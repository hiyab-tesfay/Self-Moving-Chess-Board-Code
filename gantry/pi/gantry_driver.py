#!/usr/bin/env python3
"""The Pi's half of the gantry: turns a chess move into commands for the board over USB serial.

What lives where:
  * The board (gantry/firmware) knows millimetres, the magnet, the servo arm and, since firmware
    0.2, where the squares are: `CARRY e2 e4` picks the piece up on e2 and puts it down on e4,
    with the arm, the magnet and the dwells sequenced on the board. It never hears about chess.
  * This file knows which piece is where (the job) and finds every path with the real piece
    widths (board_paths.py), because only the Pi has the camera and the position. It sends one
    CARRY per piece carried, with the via-points the path needs, and waits for each to finish.

Jobs are the JSON that the website's game server and carry_plan.py produce, one per chess move:

    {"id": 7, "label": "e7e5", "before": [...], "pieces": {...}, "expect": [...], "steps": [
        {"op": "carry", "piece": "p", "from": "e7", "to": "e5", "path": [[4, 6], [4, 4]]}]}

Path points are in squares: a1 is (0, 0), h8 is (7, 7), x.5 are the lanes between squares,
and captured pieces park in the lanes beside the board at x = -1 (White's) and 8 (Black's),
with the row an index down the lane. Slots are named "w:0", "w:1", ... and "b:0", ...

When the job lists "before" (the squares and slots occupied as it starts) and "pieces", every
carry's path is found here by board_paths.py so that the carried piece touches nothing, and a
blocking piece is moved aside and put back when there is no other way (extra CARRY lines, in
order). Without "before", the paths in the job are used as they are.

Two protocols:
  * "duen" (default): firmware 0.2. On connect the driver sends $B= with the board geometry from
    the config, then one `CARRY from to [via ...]` per carry; via-points are places in squares
    ("5.5,0.5"), or slot names.
  * "grbl" (config "protocol": "grbl", or --gcode): plain G-code, planned entirely here, for a
    real GRBL board or firmware 0.1.

Usage (the website runs the first form through CHESS_MOTION_COMMAND):
    gantry_driver.py < job.json             perform a job; exit 0 when the pieces have stopped
    gantry_driver.py --move e2e4            one plain carry
    gantry_driver.py --game e2e4 e7e5 ...   play a whole game's carries from the start position
    gantry_driver.py --reset --fen "..."    carry every piece home from a position (or after --game)
    gantry_driver.py --home                 home the gantry
    gantry_driver.py --unlock               $X after a person has checked the board, then home
    gantry_driver.py --goto 100 50          move the empty magnet to X=100 Y=50 mm
    gantry_driver.py --status               ask the board where it is
    gantry_driver.py --calibrate            steer the magnet under a1 and h1; saves the squares to the config
    gantry_driver.py --dry-run < job.json   print the commands and the prediction, send nothing
Add --port sim to drive the pretend board in gantry/firmware/host instead of real hardware,
--gcode to speak G-code to a GRBL board.

Safety on this side: a job is refused while the board reports an alarm, a hold or Host:lost
(someone has to look at the pieces and run --unlock); a job with more than 40 carries is a
planning error; after any fault the magnet is released, the arm lowered and the motion stopped.
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
sys.path.insert(0, str(HERE))
import board_paths  # noqa: E402

DEFAULT_CONFIG = HERE / "gantry_config.json"
VIRTUAL_GANTRY = HERE.parent / "firmware" / "host" / "virtual_gantry"
FILES = "abcdefgh"
PIECE_NAMES = {"p": "pawn", "n": "knight", "b": "bishop", "r": "rook", "q": "queen", "k": "king"}

MAX_CARRIES_PER_JOB = 40     # more than this in one job is a planning error, not a chess move
MAX_LINE_CHARS = 150         # the firmware's command buffer (protocol.h: 160, minus "CARRY ")
MAX_VIA_POINTS = 30          # the firmware's sequence holds 48 steps; a carry is 10 of them plus its via-points


class GantryError(Exception):
    """Something the person has to know about, in plain words."""


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
        """A path point in square units, lanes (x.5) included. In a parking column, py is a slot index down the lane."""
        x = self._interp(self.columns, px, "column")
        if px <= -1 or px >= 8:
            row = int(py)
            if row != py or row < 0 or row >= len(self.park_rows):
                raise GantryError(f"parking row {py:g} is not configured")
            return x, self.park_rows[row]
        return x, self._interp(self.rows, py, "row")

    def place_mm(self, place: str) -> tuple[float, float]:
        """A firmware place: a square, a slot, or "x,y" in squares."""
        if "," in place:
            x, y = (float(v) for v in place.split(","))
            return self.point_mm(x, y)
        return self.square_mm(place)

    def geometry(self, config: dict):
        """The same numbers for board_paths."""
        sizes = {str(k).lower(): float(v) for k, v in (config.get("piece_sizes_mm") or board_paths.DEFAULT_PIECE_SIZES_MM).items()}
        return board_paths.Geometry(
            self.square, self.a1, {c: x for c, x in self.columns.items() if c < 0 or c > 7},
            [self.park_rows[i] for i in sorted(self.park_rows)],
            float(config.get("piece_mm", 27.0)), float(config.get("clearance_mm", 3.0)),
            float(config.get("lane_clearance_mm", 1.0)), sizes)

    def setup_line(self) -> str:
        """The $B= line that gives the firmware this board: square, a1, and the docking lanes when configured."""
        parts = [self.square, self.a1[0], self.a1[1]]
        if -1 in self.columns and 8 in self.columns:
            rows = [self.park_rows[i] for i in sorted(self.park_rows)]
            pitch = rows[1] - rows[0] if len(rows) > 1 else self.square
            parts += [self.columns[-1], self.columns[8], rows[0], pitch]
        return "$B=" + ",".join(f"{v:g}" for v in parts)

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


def place_name(point) -> str:
    """A path point in square units as the firmware writes places: a square, a slot, or x,y in squares."""
    x, y = float(point[0]), float(point[1])
    if x <= -1 or x >= 8:
        if x != int(x) or y != int(y):
            raise GantryError(f"point {point} is not a docking slot")
        if int(x) == -1:
            return f"w:{int(y)}"
        if int(x) == 8:
            return f"b:{int(y)}"
        raise GantryError(f"the board has no second docking lane (column {int(x)})")
    if x == int(x) and y == int(y) and 0 <= x <= 7 and 0 <= y <= 7:
        return f"{FILES[int(x)]}{int(y) + 1}"
    return f"{x:.1f},{y:.1f}"


def piece_words(symbol: str | None) -> str:
    if not symbol or symbol == "?":
        return "the piece"
    return f"the {'white' if symbol.isupper() else 'black'} {PIECE_NAMES.get(symbol.lower(), 'piece')}"


def describe_step(step: dict) -> str:
    """One carry in plain words, for the log and the screen."""
    who = piece_words(step.get("piece"))
    if ":" in step["to"]:
        text = f"Taking {who} off {step['from']} to slot {step['to']}"
    elif ":" in step["from"]:
        text = f"Bringing {who} from slot {step['from']} to {step['to']}"
    else:
        text = f"Moving {who} from {step['from']} to {step['to']}"
    if step.get("reason") in ("make room", "put back"):
        text += f" ({step['reason']})"
    return text


# ----------------------------------------------------------------------------- the plan for a job

def route(job: dict, board: BoardMap, config: dict) -> list[dict]:
    """The job's steps with every carry's path found afresh, and room made where a piece cannot pass.

    Needs the job's "before": the squares and slots occupied when the job starts. Without it
    the paths in the job are used as they are.
    """
    if "before" not in job:
        return list(job.get("steps", []))
    world = board_paths.Board(board.geometry(config), job["before"], pieces=job.get("pieces"))
    steps: list[dict] = []
    for step in job.get("steps", []):
        if step.get("op") != "carry":
            steps.append(step)
            continue
        if step.get("piece") and step["from"] in world.occupied:
            world.pieces.setdefault(step["from"], step["piece"])
        try:
            carries = world.plan(step["from"], step["to"])
        except board_paths.NoPath as exc:
            raise GantryError(str(exc)) from None
        for carry in carries:
            piece = step.get("piece", "?") if carry.reason == "move" else world.pieces.get(carry.piece_from, "?")
            steps.append({"op": "carry", "piece": piece, "from": carry.piece_from, "to": carry.piece_to, "path": carry.path, "reason": carry.reason})
            world.apply(carry)
    return steps


def carry_limit(config: dict) -> int:
    return int(config.get("max_carries_per_job", MAX_CARRIES_PER_JOB))


def check_carry_count(count: int, config: dict, label: str) -> None:
    if count > carry_limit(config):
        raise GantryError(f"job {label} would carry {count} pieces; more than {carry_limit(config)} in one job is a planning error, not a chess move")


def checked_points(step: dict, board: BoardMap) -> list[tuple[float, float]]:
    """The carry's path in mm, every point inside the travel."""
    path = step.get("path") or []
    if len(path) < 2:
        raise GantryError(f"carry {step.get('from')}->{step.get('to')} has no path")
    points = [board.point_mm(float(px), float(py)) for px, py in path]
    for (x, y), p in zip(points, path):
        board.check(x, y, f"point {p} of {step['from']}->{step['to']}")
    return points


def plan(job: dict, board: BoardMap, config: dict) -> list[tuple[str, str]]:
    """G-code lines for a job (GRBL boards, firmware 0.1), each with a note for the log. Fails before anything moves if a point is out of reach."""
    travel_feed = float(config["travel_speed_mm_s"]) * 60
    carry_feed = float(config["carry_speed_mm_s"]) * 60
    lines: list[tuple[str, str]] = []
    carries = 0
    for step in route(job, board, config):
        if step.get("op") == "note":
            lines.append(("# " + step.get("message", ""), "note"))
            continue
        if step.get("op") != "carry":
            raise GantryError(f"unknown step {step.get('op')!r}")
        points = checked_points(step, board)
        label = f"{step.get('piece', '?')} {step['from']}->{step['to']}"
        if step.get("reason") in ("make room", "put back"):
            label += f" ({step['reason']})"
        x0, y0 = points[0]
        lines.append((f"G1 X{x0:.3f} Y{y0:.3f} F{travel_feed:.0f}", f"go to {step['from']} for {label}"))
        lines.append(("M3", f"grab {label}"))
        lines.append((f"G4 P{float(config['grab_settle_s']):.3f}", "let the magnet take hold"))
        for (x, y) in points[1:]:
            lines.append((f"G1 X{x:.3f} Y{y:.3f} F{carry_feed:.0f}", f"carry {label}"))
        lines.append(("M5", f"release {label}"))
        lines.append((f"G4 P{float(config['release_settle_s']):.3f}", "let go cleanly"))
        carries += 1
    check_carry_count(carries, config, job.get("label", "?"))
    return lines


def plan_carry(job: dict, board: BoardMap, config: dict) -> list[tuple[str, str]]:
    """CARRY lines for firmware 0.2, one per piece carried, each with a note in plain words.

    The path comes from route() (board_paths, real piece widths); its inner points become the
    via-points, written as the firmware reads places: slot names, or x,y in squares. Every point
    is checked against the travel first, so nothing is sent that the board would refuse half-way.
    """
    lines: list[tuple[str, str]] = []
    carries = 0
    for step in route(job, board, config):
        if step.get("op") == "note":
            lines.append(("# " + step.get("message", ""), "note"))
            continue
        if step.get("op") != "carry":
            raise GantryError(f"unknown step {step.get('op')!r}")
        checked_points(step, board)
        vias = [place_name(p) for p in step["path"][1:-1]]
        if len(vias) > MAX_VIA_POINTS:
            raise GantryError(f"the way from {step['from']} to {step['to']} has {len(vias)} turns; the board takes at most {MAX_VIA_POINTS}")
        line = " ".join(["CARRY", step["from"], step["to"], *vias])
        if len(line) > MAX_LINE_CHARS:
            raise GantryError(f"the way from {step['from']} to {step['to']} does not fit in one command ({len(line)} characters)")
        lines.append((line, describe_step(step)))
        carries += 1
    check_carry_count(carries, config, job.get("label", "?"))
    return lines


def carry_motions(line: str, board: BoardMap, config: dict) -> list[tuple]:
    """What the board will do for one CARRY line: ("move", x, y, mm/s) and ("dwell", seconds), in order."""
    places = line.split()[1:]
    if len(places) < 2:
        raise GantryError(f"not a carry: {line!r}")
    src, dst, vias = places[0], places[1], places[2:]
    travel, carry = float(config["travel_speed_mm_s"]), float(config["carry_speed_mm_s"])
    servo = float(config.get("servo_settle_s", 0.3)) if config.get("servo", True) else 0.0
    motions: list[tuple] = [("move", *board.place_mm(src), travel)]
    if servo:
        motions.append(("dwell", servo))
    motions.append(("dwell", float(config["grab_settle_s"])))
    for place in [*vias, dst]:
        motions.append(("move", *board.place_mm(place), carry))
    motions.append(("dwell", float(config["release_settle_s"])))
    if servo:
        motions.append(("dwell", servo))
    return motions


def predict(lines: list[tuple[str, str]], config: dict, steps_per_mm: float = 40.0, accel: float = 800.0, start=(0.0, 0.0), board: BoardMap | None = None) -> dict:
    """Steps and seconds the board will need, from the same trapezoid the firmware uses. Reads G-code and CARRY lines."""
    x, y = start
    total_s, motor_steps = 0.0, [0, 0]

    def move(tx: float, ty: float, speed: float) -> None:
        nonlocal x, y, total_s
        d = math.hypot(tx - x, ty - y)
        ramp = speed * speed / (2 * accel)
        total_s += 2 * math.sqrt(d / accel) if 2 * ramp >= d else 2 * speed / accel + (d - 2 * ramp) / speed
        motor_steps[0] += round(abs(tx - x) * steps_per_mm)
        motor_steps[1] += round(abs(ty - y) * steps_per_mm)
        x, y = tx, ty

    for text, _ in lines:
        if text.startswith("G1") or text.startswith("G0"):
            words = dict(re.findall(r"([XYF])(-?[\d.]+)", text))
            move(float(words.get("X", x)), float(words.get("Y", y)), float(words.get("F", config["travel_speed_mm_s"] * 60)) / 60)
        elif text.startswith("G4"):
            total_s += float(text.split("P")[1])
        elif text.startswith("CARRY"):
            for motion in carry_motions(text, board or BoardMap(config), config):
                if motion[0] == "move":
                    move(motion[1], motion[2], motion[3])
                else:
                    total_s += motion[1]
    return {"seconds": round(total_s, 2), "steps": motor_steps, "end_mm": (round(x, 3), round(y, 3))}


def split_job(job: dict, board: BoardMap, config: dict, max_carries: int | None = None) -> list[dict]:
    """A job with too many carries (a reset after a long game) as several jobs within the limit.

    The whole job is routed once, so make-room carries are counted; each part is then a plain
    job of already-routed steps (no "before", so the driver uses its paths as they are), with
    "expect" worked out from where the pieces will be after it.
    """
    limit = carry_limit(config) if max_carries is None else max_carries
    steps = route(job, board, config)
    if sum(1 for s in steps if s.get("op") == "carry") <= limit:
        return [job]
    chunks: list[list[dict]] = [[]]
    count = 0
    for step in steps:
        if step.get("op") == "carry":
            if count == limit:
                chunks.append([])
                count = 0
            count += 1
        chunks[-1].append(step)
    pieces = dict(job.get("pieces") or {})
    occupied = set(job.get("before") or [])
    jobs = []
    for i, chunk in enumerate(chunks):
        for step in chunk:
            if step.get("op") == "carry":
                occupied.discard(step["from"])
                occupied.add(step["to"])
                pieces[step["to"]] = pieces.pop(step["from"], step.get("piece", "?"))
        jobs.append({"id": job.get("id", 0), "label": f"{job.get('label', '?')} ({i + 1}/{len(chunks)})", "steps": chunk,
                     "expect": sorted(n for n in occupied if ":" not in n)})
    return jobs


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
            args = [exe, "--speed", os.environ.get("GANTRY_SIM_SPEED", "50")]
            if os.environ.get("GANTRY_SIM_HOST_TIMEOUT_MS"):
                args += ["--host-timeout", os.environ["GANTRY_SIM_HOST_TIMEOUT_MS"]]
            if os.environ.get("GANTRY_SIM_NO_SERVO"):
                args.append("--no-servo")
            self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
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


def parse_status(line: str) -> dict | None:
    """<State[:reason]|MPos:x,y[,z]|Mag:0|Homed:1[|Servo:up][|Prog:k/n][|Host:lost]> as a dict, or None."""
    if not (line.startswith("<") and line.endswith(">")):
        return None
    fields = line[1:-1].split("|")
    state, _, reason = fields[0].partition(":")
    info = {"state": state, "reason": reason, "x": 0.0, "y": 0.0, "magnet": False, "homed": False,
            "servo": None, "prog": None, "host_lost": False, "raw": line}
    for field in fields[1:]:
        key, _, value = field.partition(":")
        try:
            if key == "MPos":
                parts = value.split(",")
                info["x"], info["y"] = float(parts[0]), float(parts[1])
            elif key == "Mag":
                info["magnet"] = value == "1"
            elif key == "Homed":
                info["homed"] = value == "1"
            elif key == "Servo":
                info["servo"] = value
            elif key == "Prog":
                info["prog"] = value
            elif key == "Host":
                info["host_lost"] = value == "lost"
        except (ValueError, IndexError):
            return None
    return info


UNLOCK_HINT = "Check the pieces, then run gantry_driver.py --unlock (it sends $X and homes)."


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
                _, _, words = line.partition(" ")
                raise GantryError(f"the board refused {text!r}: {words or line}")
            self.note(line)
        raise GantryError(f"no answer from the board to {text!r} within {timeout:.0f}s")

    def status(self) -> dict:
        self.link.write("?")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            line = self.link.read(max(0.0, deadline - time.monotonic()))
            if line is None:
                break
            found = parse_status(line)
            if found:
                return found
            self.note(line)
        raise GantryError("the board did not report its status")

    def make_safe(self, stop_first: bool) -> None:
        """Best effort after a fault: magnet off, arm down, motion stopped. Never raises."""
        order = ["!", "M5", "M8"] if stop_first else ["M5", "M8", "!"]
        for text in order:
            try:
                self.log.warning("SAFE  %s", text)
                self.link.write(text)
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    line = self.link.read(max(0.0, deadline - time.monotonic()))
                    if line is None or line == "ok" or line.startswith("error:"):
                        break
                    self.note(line)
            except Exception:   # the link may be gone; nothing more to do about it here
                return

    def wait_idle(self, timeout: float, what: str) -> dict:
        """Poll until the board is idle. Alarm or hold means the move did not finish; a timeout stops everything."""
        deadline = time.monotonic() + timeout
        while True:
            state = self.status()
            if state["state"] == "Idle":
                return state
            if state["state"] == "Alarm":
                raise GantryError(f"the board stopped during {what}: {state['reason'] or 'alarm'}. {UNLOCK_HINT}")
            if state["state"] == "Hold":
                raise GantryError(f"the board was stopped during {what}. {UNLOCK_HINT}")
            if time.monotonic() > deadline:
                self.make_safe(stop_first=True)
                raise GantryError(f"{what} did not finish within {timeout:.0f}s; the board has been stopped and the magnet released. {UNLOCK_HINT}")
            time.sleep(0.05)

    def check_safe(self) -> dict:
        """Refuse to start while the board says a person should look at it first."""
        state = self.status()
        if state["host_lost"]:
            raise GantryError(f"The board lost contact with the Pi during its last move and made itself safe. {UNLOCK_HINT}")
        if state["state"] == "Alarm" and state["reason"] not in ("", "not homed"):
            raise GantryError(f"The board stopped with an alarm: {state['reason']}. {UNLOCK_HINT}")
        if state["state"] == "Hold":
            raise GantryError(f"The board is stopped (hold). {UNLOCK_HINT}")
        return state

    def home(self) -> None:
        self.command("$H")
        self.wait_idle(120, "homing")

    def unlock(self) -> None:
        """After a person has checked the board: clear the alarm or hold and find the position again."""
        self.command("$X")
        self.home()

    def ensure_homed(self) -> None:
        state = self.status()
        if state["state"] == "Alarm" or not state["homed"]:
            self.home()

    def set_board(self, config: dict) -> None:
        """Give the firmware this board's geometry, so its idea of e4 is the Pi's."""
        self.command(BoardMap(config).setup_line())

    def perform(self, lines: list[tuple[str, str]], config: dict, progress=None) -> None:
        """Run planned commands, waiting for each to finish before the next. progress(text) is told about every carry."""
        board = BoardMap(config)
        for text, note in lines:
            if text.startswith("#"):
                self.log.info("NOTE  %s", text[2:])
                continue
            if text.startswith("CARRY"):
                guess = predict([(text, note)], config, board=board)
                self.log.info("CARRY %s (about %.1f s)", note, guess["seconds"])
                if progress:
                    progress(note)
                self.command(text)
                self.wait_idle(10 + 2 * guess["seconds"], note)
                continue
            self.command(text)
            if text.startswith(("G0", "G1")):
                words = dict(re.findall(r"([XYF])(-?[\d.]+)", text))
                speed = float(words.get("F", config["travel_speed_mm_s"] * 60)) / 60
                self.wait_idle(10 + (config["travel_mm"][0] + config["travel_mm"][1]) / max(speed, 1) * 3, note)
            elif text.startswith("G4"):
                self.wait_idle(float(text.split("P")[1]) + 5, note)
            elif text == "M3" and progress:
                progress(note)


# ----------------------------------------------------------------------------- entry points

def load_config(path: Path) -> dict:
    with open(path) as handle:
        config = json.load(handle)
    config.setdefault("travel_speed_mm_s", 120.0)
    config.setdefault("carry_speed_mm_s", 60.0)
    config.setdefault("grab_settle_s", 0.3)
    config.setdefault("release_settle_s", 0.2)
    config.setdefault("servo", True)
    config.setdefault("servo_settle_s", 0.3)
    config.setdefault("home_on_start", True)
    config.setdefault("protocol", "duen")
    return config


def uses_gcode(config: dict) -> bool:
    return str(config.get("protocol", "duen")).lower() == "grbl"


def simple_move_job(uci: str, piece: str = "?", occupied=None) -> dict:
    """A one-carry job for --move. With `occupied` (default: the starting position) the path is found by board_paths."""
    src, dst = uci[:2], uci[2:4]
    fx, fy = FILES.index(src[0]), int(src[1]) - 1
    tx, ty = FILES.index(dst[0]), int(dst[1]) - 1
    pieces = board_paths.pieces_at_start() if occupied is None else {}
    before = sorted(board_paths.occupied_at_start() | {src} if occupied is None else set(occupied) | {src})
    if piece == "?" and src in pieces:
        piece = pieces[src]
    return {"id": 0, "label": uci, "expect": [], "before": before, "pieces": pieces,
            "steps": [{"op": "carry", "piece": piece, "from": src, "to": dst, "path": [[fx, fy], [tx, ty]]}]}


def plan_job(job: dict, config: dict) -> list[tuple[str, str]]:
    """The commands for a job in the configured protocol. Raises before anything moves if a point is unreachable."""
    board = BoardMap(config)
    return plan(job, board, config) if uses_gcode(config) else plan_carry(job, board, config)


def run_job(job: dict, config: dict, link: Link, log=logging.getLogger("gantry"), progress=None) -> dict:
    """Perform one job on an open link. progress(text), when given, hears about each carry as it starts.

    Order: plan (fails early), refuse if the board wants a person first, give it the board
    geometry ($B, firmware 0.2), home if the config says so, then run. Any fault leaves the
    magnet off and the arm down.
    """
    lines = plan_job(job, config)
    gantry = Gantry(link, log)
    gantry.check_safe()
    if not uses_gcode(config):
        gantry.set_board(config)
    if config.get("home_on_start", True):
        gantry.ensure_homed()
    log.info("JOB %s: %d commands", job.get("label", "?"), len(lines))
    try:
        gantry.perform(lines, config, progress)
    except BaseException:
        gantry.make_safe(stop_first=False)
        raise
    return gantry.status()


def run_jobs(jobs: list[dict], config: dict, link: Link, log=logging.getLogger("gantry"), progress=None) -> dict:
    state = None
    for job in jobs:
        state = run_job(job, config, link, log, progress)
    return state if state is not None else Gantry(link, log).status()


def game_jobs(ucis: list[str]):
    """The jobs that play a whole game from the start position, and the layout after it (needs python-chess)."""
    import chess  # noqa: F401  (python-chess)
    import carry_plan
    board, layout = chess.Board(), carry_plan.Layout()
    jobs = []
    for i, uci in enumerate(ucis):
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            raise GantryError(f"{uci!r} is not a move") from None
        if move not in board.legal_moves:
            raise GantryError(f"{uci} is not legal after {' '.join(ucis[:i]) or 'the start'}")
        jobs.append(carry_plan.job_for_move(layout, board, move, i + 1))
        board.push(move)
    return jobs, layout


def layout_from_text(text: str):
    """A layout as "e4=P,d5=p,b:0=p" or a JSON object {"e4": "P", ...}."""
    import carry_plan
    text = text.strip()
    if text.startswith("{"):
        pieces = json.loads(text)
    else:
        pieces = {}
        for item in text.split(","):
            if not item.strip():
                continue
            name, _, symbol = item.strip().partition("=")
            if not symbol:
                raise GantryError(f"{item!r}: write square=piece, like e4=P or b:0=p")
            pieces[name.strip()] = symbol.strip()
    layout = carry_plan.Layout.__new__(carry_plan.Layout)
    layout.pieces = {str(k): str(v) for k, v in pieces.items()}
    return layout


def reset_jobs(layout, config: dict) -> list[dict]:
    import carry_plan
    return split_job(carry_plan.job_for_reset(layout), BoardMap(config), config)


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


def print_dry_run(jobs: list[dict], config: dict) -> None:
    board = BoardMap(config)
    total = 0.0
    for job in jobs:
        lines = plan_job(job, config)
        if len(jobs) > 1:
            print(f"; job {job.get('label', '?')}")
        for text, note in lines:
            print(f"{text:<40} ; {note}")
        guess = predict(lines, config, board=board)
        total += guess["seconds"]
        print(f"; predicted: {guess['seconds']} s, motor steps X={guess['steps'][0]} Y={guess['steps'][1]}, ends at X={guess['end_mm'][0]} Y={guess['end_mm'][1]} mm")
    if len(jobs) > 1:
        print(f"; {len(jobs)} jobs, about {total:.0f} s in all")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", help="serial port, or 'sim' for the pretend board (default: from the config)")
    parser.add_argument("--move", metavar="UCI", help="carry one piece, e.g. e2e4, finding a way round the other pieces")
    parser.add_argument("--occupied", metavar="SQUARES", help="for --move: the occupied squares, comma separated (default: the starting position); 'none' for an empty board")
    parser.add_argument("--game", nargs="+", metavar="UCI", help="play every move of a game from the start position, both sides, e.g. --game e2e4 e7e5 g1f3")
    parser.add_argument("--reset", action="store_true", help="carry every piece back to its home square: after --game, or from --fen / --layout")
    parser.add_argument("--fen", help="for --reset: the position on the board")
    parser.add_argument("--layout", help="for --reset: where every piece is, e4=P,d5=p,b:0=p or a JSON object")
    parser.add_argument("--home", action="store_true")
    parser.add_argument("--unlock", action="store_true", help="after a person has checked the board: $X, then home")
    parser.add_argument("--goto", nargs=2, type=float, metavar=("X", "Y"), help="move the empty magnet to X Y mm")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--calibrate", action="store_true", help="steer the magnet under a1 and h1, then save the square positions to the config")
    parser.add_argument("--gcode", action="store_true", help="speak G-code (GRBL, firmware 0.1) instead of CARRY commands")
    parser.add_argument("--dry-run", action="store_true", help="print the commands and the prediction; send nothing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    log = logging.getLogger("gantry")

    try:
        config = load_config(args.config)
        if args.gcode:
            config["protocol"] = "grbl"
        port = args.port or config.get("port", "/dev/ttyUSB0")
        jobs: list[dict] | None = None
        if args.move:
            occupied = None
            if args.occupied:
                occupied = [] if args.occupied.strip().lower() == "none" else [s.strip() for s in args.occupied.split(",") if s.strip()]
            jobs = [simple_move_job(args.move, occupied=occupied)]
        elif args.game or args.reset:
            jobs = []
            layout = None
            if args.game:
                game, layout = game_jobs(args.game)
                jobs += game
            if args.reset:
                if args.fen:
                    import chess
                    import carry_plan
                    layout = carry_plan.Layout(chess.Board(args.fen))
                elif args.layout:
                    layout = layout_from_text(args.layout)
                elif layout is None:
                    parser.error("--reset needs the position: --fen, --layout, or the moves of --game")
                jobs += reset_jobs(layout, config)
        elif args.home or args.unlock or args.goto or args.status or args.calibrate:
            jobs = None
        else:
            raw = sys.stdin.read()
            if not raw.strip():
                parser.error("give a job on standard input, or --move/--game/--reset/--home/--goto/--status")
            jobs = [json.loads(raw)]

        if args.dry_run:
            print_dry_run(jobs or [], config)
            return 0

        link = Link(port, int(config.get("baud", 115200)), log)
        try:
            if jobs is not None:
                state = run_jobs(jobs, config, link, log)
            else:
                gantry = Gantry(link, log)
                if args.unlock:
                    gantry.unlock()
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
            print(json.dumps({"ok": True, "state": state["state"], "x_mm": state["x"], "y_mm": state["y"], "magnet": state["magnet"],
                              "homed": state["homed"], "servo": state.get("servo"), "host_lost": state.get("host_lost", False)}))
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
