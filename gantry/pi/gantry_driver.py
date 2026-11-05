#!/usr/bin/env python3
"""The Pi's half of the gantry: turns a chess move into G-code for the board over USB serial.

What lives where:
  * The board (gantry/firmware) only knows millimetres: move here, magnet on, magnet off, home.
    It never hears about chess.
  * This file knows where the squares are (gantry_config.json) and turns a job, which lists
    piece carries in square units, into those millimetre commands. It never times a step.

Jobs are the JSON that the website's game server produces, one per chess move:

    {"id": 7, "label": "e7e5", "expect": [...], "steps": [
        {"op": "carry", "piece": "p", "from": "e7", "to": "e5", "path": [[4, 6], [4, 4]]}]}

Path points are in squares: a1 is (0, 0), h8 is (7, 7), x.5 are the lanes between squares,
and captured pieces park at x = -1, -2 (White's) and 8, 9 (Black's), rows 0 to 7. Slots are
named "w:0" to "w:15" and "b:0" to "b:15". For a carry: go to the first point, magnet on,
follow the rest of the points, magnet off.

Usage (the website runs the first form through CHESS_MOTION_COMMAND):
    gantry_driver.py < job.json             perform a job; exit 0 when the pieces have stopped
    gantry_driver.py --move e2e4            one plain carry
    gantry_driver.py --home                 home the gantry
    gantry_driver.py --goto 100 50          move the empty magnet to X=100 Y=50 mm
    gantry_driver.py --status               ask the board where it is
    gantry_driver.py --calibrate            steer the magnet under a1 and h1; saves the squares to the config
    gantry_driver.py --dry-run < job.json   print the G-code and the predicted steps, send nothing
Add --port sim to drive the pretend board in gantry/firmware/host instead of real hardware.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "gantry_config.json"
VIRTUAL_GANTRY = HERE.parent / "firmware" / "host" / "virtual_gantry"
STATUS = re.compile(r"<(?P<state>[A-Za-z]+)(?::(?P<reason>[^|>]*))?\|MPos:(?P<x>-?[\d.]+),(?P<y>-?[\d.]+)(?:\|Mag:(?P<mag>[01]))?(?:\|Homed:(?P<homed>[01]))?")
FILES = "abcdefgh"


class GantryError(Exception):
    pass


# ----------------------------------------------------------------------------- squares to millimetres

class BoardMap:
    """Where every square, lane and parking slot is, in mm from the homed corner."""

    def __init__(self, config: dict):
        self.square = float(config["square_mm"])
        self.a1 = tuple(float(v) for v in config["a1_center_mm"])
        park = config.get("park_columns_mm") or {}
        self.columns = {i: self.a1[0] + i * self.square for i in range(8)}
        for i, x in enumerate(park.get("w", [])):
            self.columns[-1 - i] = float(x)
        for i, x in enumerate(park.get("b", [])):
            self.columns[8 + i] = float(x)
        rows = config.get("park_rows_mm")
        self.rows = {i: self.a1[1] + i * self.square for i in range(8)}
        self.park_rows = {i: float(y) for i, y in enumerate(rows)} if rows else self.rows
        self.travel = tuple(float(v) for v in config["travel_mm"])

    def square_mm(self, name: str) -> tuple[float, float]:
        """A square like "e4" or a slot like "w:3"."""
        if ":" in name:
            color, index = name[0], int(name[2:])
            column = -1 - index // 8 if color == "w" else 8 + index // 8
            if column not in self.columns:
                raise GantryError(f"no parking column configured for {name}")
            return self.columns[column], self.park_rows[index % 8]
        return self.point_mm(FILES.index(name[0]), int(name[1]) - 1)

    def point_mm(self, px: float, py: float) -> tuple[float, float]:
        """A path point in square units, lanes (x.5) included."""
        return self._interp(self.columns, px, "column"), self._interp(self.rows, py, "row", self.park_rows)

    def _interp(self, table: dict, value: float, what: str, outer: dict | None = None) -> float:
        """Whole numbers are configured centres; x.5 lies halfway between its neighbours, or half a square past the last one."""
        lo, hi = math.floor(value), math.ceil(value)
        known = {**(outer or {}), **table}
        if lo == hi:
            if lo not in known:
                raise GantryError(f"{what} {value:g} is not configured")
            return known[lo]
        if lo in known and hi in known:
            return known[lo] + (known[hi] - known[lo]) * (value - lo)
        if hi in known:
            return known[hi] - self.square / 2
        if lo in known:
            return known[lo] + self.square / 2
        raise GantryError(f"{what} {value:g} is not configured")

    def check(self, x: float, y: float, what: str) -> None:
        if x < 0 or y < 0 or x > self.travel[0] or y > self.travel[1]:
            raise GantryError(f"{what} is at X={x:.1f} Y={y:.1f} mm, outside the gantry's travel of {self.travel[0]:.0f} x {self.travel[1]:.0f} mm")


# ----------------------------------------------------------------------------- the plan for a job

def plan(job: dict, board: BoardMap, config: dict) -> list[tuple[str, str]]:
    """G-code lines for a job, each with a note for the log. Fails before anything moves if a point is out of reach."""
    travel_feed = float(config["travel_speed_mm_s"]) * 60
    carry_feed = float(config["carry_speed_mm_s"]) * 60
    lines: list[tuple[str, str]] = []
    for step in job.get("steps", []):
        if step.get("op") == "note":
            lines.append(("# " + step.get("message", ""), "note"))
            continue
        if step.get("op") != "carry":
            raise GantryError(f"unknown step {step.get('op')!r}")
        path = step.get("path") or []
        if len(path) < 2:
            raise GantryError(f"carry {step.get('from')}->{step.get('to')} has no path")
        points = [board.point_mm(float(px), float(py)) for px, py in path]
        for (x, y), p in zip(points, path):
            board.check(x, y, f"point {p} of {step['from']}->{step['to']}")
        label = f"{step.get('piece', '?')} {step['from']}->{step['to']}"
        x0, y0 = points[0]
        lines.append((f"G1 X{x0:.3f} Y{y0:.3f} F{travel_feed:.0f}", f"go to {step['from']} for {label}"))
        lines.append(("M3", f"grab {label}"))
        lines.append((f"G4 P{float(config['grab_settle_s']):.3f}", "let the magnet take hold"))
        for (x, y) in points[1:]:
            lines.append((f"G1 X{x:.3f} Y{y:.3f} F{carry_feed:.0f}", f"carry {label}"))
        lines.append(("M5", f"release {label}"))
        lines.append((f"G4 P{float(config['release_settle_s']):.3f}", "let go cleanly"))
    return lines


def predict(lines: list[tuple[str, str]], config: dict, steps_per_mm: float = 40.0, accel: float = 800.0, start=(0.0, 0.0)) -> dict:
    """Steps and seconds the board will need, from the same trapezoid the firmware uses."""
    x, y = start
    total_s, motor_steps = 0.0, [0, 0]
    for text, _ in lines:
        if text.startswith("G1") or text.startswith("G0"):
            words = dict(re.findall(r"([XYF])(-?[\d.]+)", text))
            tx, ty = float(words.get("X", x)), float(words.get("Y", y))
            speed = float(words.get("F", config["travel_speed_mm_s"] * 60)) / 60
            d = math.hypot(tx - x, ty - y)
            ramp = speed * speed / (2 * accel)
            total_s += 2 * math.sqrt(d / accel) if 2 * ramp >= d else 2 * speed / accel + (d - 2 * ramp) / speed
            motor_steps[0] += round(abs(tx - x) * steps_per_mm)
            motor_steps[1] += round(abs(ty - y) * steps_per_mm)
            x, y = tx, ty
        elif text.startswith("G4"):
            total_s += float(text.split("P")[1])
    return {"seconds": round(total_s, 2), "steps": motor_steps, "end_mm": (round(x, 3), round(y, 3))}


# ----------------------------------------------------------------------------- talking to the board

class Link:
    """A line-based connection to the board: real serial, or the pretend board as a subprocess.

    A background thread reads lines into a queue, so a reply is never missed while this side
    is busy, and reading with a timeout is simple.
    """

    def __init__(self, port: str, baud: int, log=logging.getLogger("gantry")):
        self.log = log
        self.traces: list[str] = []
        self.process, self.serial = None, None
        self.lines: queue.Queue = queue.Queue()
        if port == "sim":
            exe = os.environ.get("GANTRY_VIRTUAL", str(VIRTUAL_GANTRY))
            if not Path(exe).exists():
                raise GantryError(f"build the pretend board first: make -C {VIRTUAL_GANTRY.parent} virtual_gantry")
            speed = os.environ.get("GANTRY_SIM_SPEED", "50")
            self.process = subprocess.Popen([exe, "--speed", speed], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
            source = self.process.stdout
        else:
            import serial  # pyserial, only needed with real hardware
            self.serial = serial.Serial(port, baud, timeout=0.2)
            source = self.serial
            time.sleep(2.0)  # an Arduino or ESP32 resets when the port opens
        self.reader = threading.Thread(target=self._pump, args=(source,), name="gantry-reader", daemon=True)
        self.reader.start()

    def _pump(self, source) -> None:
        buf = b""
        try:
            while True:
                if self.process:
                    line = source.readline()
                    if line == "":
                        self.lines.put(None)
                        return
                    self.lines.put(line.rstrip("\r\n"))
                else:
                    chunk = source.readline()
                    if not chunk:
                        continue
                    buf += chunk
                    if buf.endswith(b"\n"):
                        self.lines.put(buf.decode(errors="replace").rstrip("\r\n"))
                        buf = b""
        except (OSError, ValueError):
            self.lines.put(None)

    def write(self, text: str) -> None:
        if self.process:
            self.process.stdin.write(text + "\n")
            self.process.stdin.flush()
        else:
            self.serial.write((text + "\n").encode())

    def read(self, timeout: float) -> str | None:
        """One line, or None when nothing arrives within the timeout."""
        try:
            line = self.lines.get(timeout=max(timeout, 0.001))
        except queue.Empty:
            return None
        if line is None:
            raise GantryError("the connection to the board closed")
        return line

    def close(self) -> None:
        if self.process:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.reader.join(timeout=2)
            self.process.stdout.close()
        elif self.serial:
            self.serial.close()


class Gantry:
    def __init__(self, link: Link, log=logging.getLogger("gantry")):
        self.link, self.log = link, log
        self.drain(1.0)

    def drain(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = self.link.read(0.1)
            if line is None:
                continue
            self.note(line)

    def note(self, line: str) -> None:
        if line.startswith("#"):
            self.link.traces.append(line)
        self.log.info("BOARD %s", line)

    def command(self, text: str, timeout: float = 5.0) -> None:
        """Send one line and wait for its ok. Raises on error:."""
        self.log.info("SEND  %s", text)
        self.link.write(text)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.link.read(max(0.0, deadline - time.monotonic()))
            if line is None:
                break
            if line == "ok":
                return
            if line.startswith("error:"):
                raise GantryError(f"the board refused {text!r}: {line}")
            self.note(line)
        raise GantryError(f"no answer from the board to {text!r} within {timeout:.0f}s")

    def status(self) -> dict:
        self.link.write("?")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            line = self.link.read(max(0.0, deadline - time.monotonic()))
            if line is None:
                break
            found = STATUS.match(line)
            if found:
                return {"state": found["state"], "reason": found["reason"] or "", "x": float(found["x"]), "y": float(found["y"]),
                        "magnet": found["mag"] == "1", "homed": found["homed"] == "1", "raw": line}
            self.note(line)
        raise GantryError("the board did not report its status")

    def wait_idle(self, timeout: float, what: str) -> dict:
        """Poll until the board is idle. Alarm or hold means the move did not finish."""
        deadline = time.monotonic() + timeout
        while True:
            state = self.status()
            if state["state"] == "Idle":
                return state
            if state["state"] in ("Alarm", "Hold"):
                raise GantryError(f"the board stopped during {what}: {state['raw']}")
            if time.monotonic() > deadline:
                raise GantryError(f"{what} did not finish within {timeout:.0f}s: {state['raw']}")
            time.sleep(0.05)

    def home(self) -> None:
        self.command("$H")
        self.wait_idle(120, "homing")

    def ensure_homed(self) -> None:
        state = self.status()
        if state["state"] == "Alarm" or not state["homed"]:
            self.home()

    def perform(self, lines: list[tuple[str, str]], config: dict) -> None:
        """Run planned G-code, waiting for each move and dwell to finish before the next."""
        for text, note in lines:
            if text.startswith("#"):
                self.log.info("NOTE  %s", text[2:])
                continue
            self.command(text)
            if text.startswith(("G0", "G1")):
                words = dict(re.findall(r"([XYF])(-?[\d.]+)", text))
                speed = float(words.get("F", config["travel_speed_mm_s"] * 60)) / 60
                self.wait_idle(10 + (config["travel_mm"][0] + config["travel_mm"][1]) / max(speed, 1) * 3, note)
            elif text.startswith("G4"):
                self.wait_idle(float(text.split("P")[1]) + 5, note)


# ----------------------------------------------------------------------------- entry points

def load_config(path: Path) -> dict:
    with open(path) as handle:
        config = json.load(handle)
    config.setdefault("travel_speed_mm_s", 120.0)
    config.setdefault("carry_speed_mm_s", 60.0)
    config.setdefault("grab_settle_s", 0.3)
    config.setdefault("release_settle_s", 0.2)
    config.setdefault("home_on_start", True)
    return config


def simple_move_job(uci: str, piece: str = "?") -> dict:
    """A one-carry job for --move, straight line, no captures (the game server plans those)."""
    src, dst = uci[:2], uci[2:4]
    fx, fy = FILES.index(src[0]), int(src[1]) - 1
    tx, ty = FILES.index(dst[0]), int(dst[1]) - 1
    return {"id": 0, "label": uci, "expect": [], "steps": [{"op": "carry", "piece": piece, "from": src, "to": dst, "path": [[fx, fy], [tx, ty]]}]}


def run_job(job: dict, config: dict, link: Link, log=logging.getLogger("gantry")) -> dict:
    board = BoardMap(config)
    lines = plan(job, board, config)   # raises before anything moves if a point is unreachable
    gantry = Gantry(link, log)
    if config.get("home_on_start", True):
        gantry.ensure_homed()
    log.info("JOB %s: %d commands", job.get("label", "?"), len(lines))
    gantry.perform(lines, config)
    return gantry.status()


def calibrate(gantry: Gantry, config: dict, config_path: Path, read=input, write=print) -> dict:
    """Measure the board at the frame: steer the magnet under a1, then h1, and save the result.

    Moves are typed as x+10, y-0.5 and so on (millimetres); 'ok' records the spot. a1 gives
    the origin of the squares, and a1 to h1 is seven squares, which gives their size.
    """
    board = BoardMap(config)
    gantry.ensure_homed()
    pos = [0.0, 0.0]

    def goto(x: float, y: float) -> None:
        board.check(x, y, "that move")
        gantry.command(f"G0 X{x:.3f} Y{y:.3f}")
        gantry.wait_idle(60, "the jog")
        pos[0], pos[1] = x, y

    def steer(prompt: str) -> tuple[float, float]:
        write(prompt)
        write("  Type moves like x+10, y-0.5 or x-100 (mm), and ok when the magnet is centred.")
        while True:
            try:
                line = read("> ").strip().lower().replace(" ", "")
            except EOFError:
                raise GantryError("calibration cancelled") from None
            if line in ("ok", "done"):
                return pos[0], pos[1]
            found = re.fullmatch(r"([xy])([+-][\d.]+)", line)
            if not found:
                write("  x+10 moves 10 mm along X, y-0.5 moves half a mm back along Y, ok records this spot")
                continue
            delta = float(found.group(2))
            x, y = (pos[0] + delta, pos[1]) if found.group(1) == "x" else (pos[0], pos[1] + delta)
            try:
                goto(x, y)
            except GantryError as exc:
                write(f"  {exc}")
                continue
            write(f"  at X={pos[0]:.1f} Y={pos[1]:.1f}")

    a1 = steer("Step 1 of 2: put the magnet under the centre of a1.")
    h1 = steer("Step 2 of 2: now under the centre of h1, on the same rank.")
    square = (h1[0] - a1[0]) / 7
    if square < 10:
        raise GantryError(f"h1 at X={h1[0]:.1f} is not seven squares to the right of a1 at X={a1[0]:.1f}")
    config["a1_center_mm"] = [round(a1[0], 2), round(a1[1], 2)]
    config["square_mm"] = round(square, 2)
    Path(config_path).write_text(json.dumps(config, indent=2) + "\n")
    write(f"Saved to {config_path}: a1 centre ({a1[0]:.1f}, {a1[1]:.1f}) mm, squares {square:.2f} mm.")
    far = BoardMap(config).square_mm("h8")
    write(f"h8 works out at ({far[0]:.1f}, {far[1]:.1f}) mm; the travel is {board.travel[0]:.0f} x {board.travel[1]:.0f}.")
    return config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", help="serial port, or 'sim' for the pretend board (default: from the config)")
    parser.add_argument("--move", metavar="UCI", help="carry one piece, e.g. e2e4")
    parser.add_argument("--home", action="store_true")
    parser.add_argument("--goto", nargs=2, type=float, metavar=("X", "Y"), help="move the empty magnet to X Y mm")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--calibrate", action="store_true", help="steer the magnet under a1 and h1, then save the square positions to the config")
    parser.add_argument("--dry-run", action="store_true", help="print the G-code and the prediction; send nothing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    log = logging.getLogger("gantry")

    try:
        config = load_config(args.config)
        port = args.port or config.get("port", "/dev/ttyUSB0")
        if args.move:
            job = simple_move_job(args.move)
        elif args.home or args.goto or args.status or args.calibrate:
            job = None
        else:
            raw = sys.stdin.read()
            if not raw.strip():
                parser.error("give a job on standard input, or --move/--home/--goto/--status")
            job = json.loads(raw)

        if args.dry_run:
            board = BoardMap(config)
            lines = plan(job, board, config) if job else []
            for text, note in lines:
                print(f"{text:<40} ; {note}")
            guess = predict(lines, config)
            print(f"; predicted: {guess['seconds']} s, motor steps X={guess['steps'][0]} Y={guess['steps'][1]}, ends at X={guess['end_mm'][0]} Y={guess['end_mm'][1]} mm")
            return 0

        link = Link(port, int(config.get("baud", 115200)), log)
        try:
            if job is not None:
                state = run_job(job, config, link, log)
            else:
                gantry = Gantry(link, log)
                if args.calibrate:
                    calibrate(gantry, config, args.config)
                if args.home:
                    gantry.home()
                if args.goto:
                    BoardMap(config).check(args.goto[0], args.goto[1], "--goto")
                    gantry.ensure_homed()
                    gantry.command(f"G0 X{args.goto[0]:.3f} Y{args.goto[1]:.3f}")
                    gantry.wait_idle(60, "--goto")
                state = gantry.status()
            print(json.dumps({"ok": True, "state": state["state"], "x_mm": state["x"], "y_mm": state["y"], "magnet": state["magnet"], "homed": state["homed"]}))
            return 0
        finally:
            link.close()
    except GantryError as exc:
        log.error("GANTRY %s", exc)
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except (OSError, ValueError, KeyError) as exc:
        log.error("GANTRY %s", exc)
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
