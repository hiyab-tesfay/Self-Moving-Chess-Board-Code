#!/usr/bin/env python3
"""The DUEN gantry console: a page on this computer that drives the bench gantry over USB.

    python3 gantry/console/console_server.py                  # then open http://127.0.0.1:8788
    python3 gantry/console/console_server.py --port sim       # the pretend board: nothing real moves

Make a chess move on the page and the gantry plays it on the real board. Captured pieces are
carried into a docking lane beside the board: white pieces to the left lane ("w:0".."w:11"),
black ones to the right ("b:0".."b:11"), each to the other when its own is full or out of the
magnet's reach. Castling moves the king then the rook, and a promotion brings a docked piece of the
new kind out when there is one. Free mode carries any piece to any empty square or slot. Undo carries the
last move back; Reset board carries every piece home.

The board is Sketch 5's (gantry_config.json, board_layout.py): the console works in the
millimetres the firmware counts, which is the sketch less the magnet's home. On connecting it
tells the board its steps/mm ($100, from 41 mm per motor turn) and its travel ($130, $131), so
every millimetre on the page is a real one. "Check the board" visits a1, h1, h8, a8 and the
docking lanes; "a1 is here" moves the whole sketch onto where the magnet really is.

Every carry is sent as plain motion commands (G1, M7, M3, M5, M8), not the firmware's CARRY,
so the speeds set on the page are the ones used. It needs firmware 0.4 or later (speeds and
acceleration set at runtime, $110/$111/$120); an older board is refused on Connect. The stall checks (one motor turn,
distance check, speed ladder, motors-off push test, restart counter) are on the page too.

Only one exchange with the board runs at a time (a lock); the Stop button writes "!" at once,
outside that lock, so it is never stuck behind a move.
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chess

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "pi"))
import board_layout as bl  # noqa: E402
import board_paths as bp  # noqa: E402
import carry_plan as cp  # noqa: E402
import gantry_driver as gd  # noqa: E402

SETTINGS_FILE = HERE / "console_settings.json"   # this computer's choices; not committed
PAGE = HERE / "console.html"

# Real millimetres and seconds: since firmware 0.7 the board counts real mm ($100 from mm_per_turn in the config).
DEFAULTS = {
    "travel_speed": 40.0,     # mm/s, arm down, nothing carried
    "carry_speed": 25.0,      # mm/s with a piece on the magnet
    "accel": 400.0,           # mm/s^2, sent to the board as $120
    "x_speed_cap": 120.0,     # mm/s the X axis itself may reach (the speed ladder finds it); a diagonal slows
    "y_speed_cap": 120.0,     # down so neither axis goes past its own cap
    "servo_settle": 3.0,      # s for the arm to come up or go down (0.8.0: a slow 2.9 s sweep, like the bench sketch)
    "grab_settle": 0.3,       # s for the magnet to take hold
    "release_settle": 0.25,   # s for the piece to settle after the magnet lets go
    "magnet_budget": 0.0,     # s per hold before the piece is set down and picked up again; 0 = never (a permanent
                              # magnet lifted by the servo has no coil to rest). Firmware that cuts the magnet ($G,
                              # 15 s up to 0.7.1) is kept 3 s under its limit whatever this says: see hold_limit()
    "overshoot": 5.0,         # mm past the last point and back: the carried piece trails the magnet (MuJoCo and
                              # gantry/sim/path_sim.py: 5-6 mm), so without this it stops that far short of the square
    "auto_run": True,         # run a move on the gantry as soon as it is made on the page
}
LIMITS = {"travel_speed": (2, 120), "carry_speed": (2, 120), "accel": (50, 800),
          "x_speed_cap": (2, 1000), "y_speed_cap": (2, 1000),
          "servo_settle": (0, 3), "grab_settle": (0, 3), "release_settle": (0, 3), "magnet_budget": (0, 14),
          "overshoot": (0, 15)}
FIRMWARE_DIR = HERE.parent / "firmware" / "gantry_firmware"   # the sketch the Flash button uploads
FQBN = "esp32:esp32:esp32"     # ESP32 Dev Module
UPLOAD_SPEED = 230400          # 921600 gave serial noise on the bench board (2026-11-06); 230400 works
CEILING_SPEED, CEILING_ACCEL = 120.0, 800.0   # the firmware's hard limits (MAX_SPEED_MM_S, MAX_ACCEL_MM_S2)
POLL_LEG_S = 0.15             # status-poll latency added to every leg when checking the magnet time
LADDER_SPEEDS = [20, 30, 40, 50, 60, 80, 100, 120]
TURN_SPEED = 12.0             # mm/s: one motor turn (41 mm) in about 3.5 s, slow enough to count


class Busy(Exception):
    pass


class Stopped(Exception):
    pass


class CameraFailed(gd.GantryError):
    """The camera could not find the check piece or token (camera_locate.LocateError), or it did something the
    camera check cannot use; the message is plain words for the page."""


# Camera check (Console.camera_check): the four corners, a8 first (next to home), then the centre. The camera only
# measures: it never moves the gantry to correct anything and never changes the config or the Tune answers.
CAL_SQUARES = ["a8", "h8", "h1", "a1", "d5", "e4"]
CAL_APPROACH_MM = 2.0          # every stop ends with this last leg, always from the same side, so the piece's drag
CAL_APPROACH_DIR = (math.sqrt(0.5), -math.sqrt(0.5))   # lag is the same each time (from +x,-y: clear of rank 8's edge)
CAL_VERIFY_TOL_MM = 1.5        # every square landing this close to its centre: the board is calibrated
CAL_CAPTURE_MM = 3.0           # the flat token followed the magnet's test move to within this: it is held
CAL_CAPTURE_PIECE_MM = 4.0     # the same for a chess piece (its top is seen from an angle: more parallax noise)
CAL_TEST_MOVE_MM = 10.0


class Refused(gd.GantryError):
    """The board answered error:<code>."""

    def __init__(self, text: str, line: str):
        self.line = line
        try:
            self.code = int(line.split()[0].split(":")[1])
        except (IndexError, ValueError):
            self.code = 0
        words = line.partition(" ")[2] or line
        super().__init__(f"the board refused {text!r}: {words}")


def trapezoid_s(d: float, v: float, a: float) -> float:
    """Seconds for a d mm move from rest to rest at top speed v and acceleration a."""
    if d <= 1e-9:
        return 0.0
    if d < v * v / a:
        return 2 * math.sqrt(d / a)
    return d / v + v / a


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        settings.update({k: saved[k] for k in DEFAULTS if k in saved})
    except (OSError, ValueError):
        pass
    return settings


def chess_from_layout_move(game: chess.Board, layout: cp.Layout, src: str, dst: str) -> chess.Board:
    """The game position after a free carry src -> dst (squares or slots), side to move kept."""
    new = game.copy(stack=False)
    piece = new.piece_at(chess.parse_square(src)) if ":" not in src else chess.Piece.from_symbol(layout.pieces[src])
    if ":" not in src:
        new.remove_piece_at(chess.parse_square(src))
    if ":" not in dst and piece:
        new.set_piece_at(chess.parse_square(dst), piece)
    new.ep_square = None
    new.castling_rights = new.clean_castling_rights()
    return new


class Console:
    def __init__(self, config: dict, sim_speed: float = 1.0, config_path: Path | None = None):
        self.config = config
        self.config_path = config_path
        self.map = gd.BoardMap(config)
        self.geometry = self.map.geometry(config)
        self.settings = load_settings()
        self.sim_speed = sim_speed
        self.lock = threading.RLock()     # the console's own state
        self.io = threading.Lock()        # one exchange with the board at a time
        self.wio = threading.Lock()       # one write at a time (Stop writes outside self.io)
        self.link: gd.Link | None = None
        self.port: str | None = None
        self.connecting = False
        self.firmware = ""
        self.too_old = ""                 # why the last Connect refused the board's firmware (before 0.4), for the page
        self.board_settings: dict[str, float] = {}
        self.status: dict | None = None
        self.restarts = 0                 # banners seen while connected: the ESP32 reset itself (supply dip)
        self.position_lost = False
        self.memory: gd.PositionMemory | None = None   # the remembered position of the board last connected
        self.task: dict | None = None
        self.waiting: dict | None = None
        self.stop_flag = threading.Event()
        self.game = chess.Board()
        self.bot = None
        self.layout = self.new_layout()
        self.history: list[dict] = []
        self.jog_queue: list[tuple[float, float]] = []
        self.preview: dict | None = None
        self.pending: dict | None = None  # a job that stopped part-way
        self.tuning: dict | None = None   # the square the Tune panel last sent the magnet to
        self.sim_nudges: dict | None = None   # Tune answers made on the pretend board: never in self.config or the file
        self.sim_start: list | None = None    # the pretend board's own starting position (Set start here)
        self.make_locator = None              # a callable returning the camera locator; None: camera_locate's own
        self.locator = None                   # the last one made (its last_photo is served at /api/camera-photo)
        self.camera_settle: float | None = None   # s to wait before each photo; None: grab_settle, at least 0.5 s
        self.camera_cal: dict = {"state": "idle", "round": 0, "squares": [], "results": [], "message": ""}
        self.tests: dict = {"distance": None, "ladder": None}
        self.log: deque = deque(maxlen=500)
        self.log_id = 0
        threading.Thread(target=self._poll, name="console-poll", daemon=True).start()

    # ------------------------------------------------------------------ log

    def say(self, kind: str, text: str) -> None:
        with self.lock:
            self.log_id += 1
            self.log.append({"id": self.log_id, "t": time.strftime("%H:%M:%S"), "kind": kind, "text": text})
        logging.getLogger("console").info("%-5s %s", kind, text)

    # ------------------------------------------------------------------ the board link

    def _write(self, text: str) -> None:
        link = self.link
        if link is None:
            raise gd.GantryError("not connected to the board")
        try:
            with self.wio:
                link.write(text)
        except (OSError, ValueError) as exc:   # unplugged, or the pretend board has gone
            self._lost(exc)
            raise gd.GantryError("the connection to the board closed") from None

    def _board_line(self, line: str) -> None:
        if line.startswith("[duen-gantry") and "ready" in line and not self.connecting:
            self.restarts += 1
            self.position_lost = True
            self.say("error", "The board restarted by itself (its supply dipped, or the USB link reset it). "
                              "Its position is lost: Disconnect and Connect again to put back the remembered "
                              "position, or home it again. " + line)
            return
        self.say("board", line)

    def _set_status(self, st: dict) -> None:
        with self.lock:
            self.status = st

    def _read(self, link: gd.Link, timeout: float) -> str | None:
        """One line from the board; a closed link is noticed here, whoever was reading."""
        try:
            return link.read(timeout)
        except gd.GantryError as exc:
            self._lost(exc)
            raise

    def exchange(self, text: str, timeout: float = 5.0) -> list[str]:
        """Send one line; return the lines the board printed before its ok. Raises Refused on error:."""
        link = self.link
        if link is None:
            raise gd.GantryError("not connected to the board")
        with self.io:
            if text != "?":
                self.say("send", text)
            self._write(text)
            deadline = time.monotonic() + timeout
            extra: list[str] = []
            while time.monotonic() < deadline:
                line = self._read(link, max(0.0, deadline - time.monotonic()))
                if line is None:
                    break
                if line == "ok":
                    return extra
                if line.startswith("error:"):
                    self.say("error", f"{text} -> {line}")
                    raise Refused(text, line)
                st = gd.parse_status(line)
                if st:
                    self._set_status(st)
                    continue
                self._board_line(line)
                extra.append(line)
        raise gd.GantryError(f"no answer from the board to {text!r} within {timeout:.0f} s")

    def query_status(self) -> dict:
        link = self.link
        if link is None:
            raise gd.GantryError("not connected to the board")
        with self.io:
            self._write("?")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                line = self._read(link, max(0.0, deadline - time.monotonic()))
                if line is None:
                    break
                st = gd.parse_status(line)
                if st:
                    self._set_status(st)
                    return st
                if line == "ok" or line.startswith("error:"):
                    continue          # the answer to a Stop written outside this exchange
                self._board_line(line)
        raise gd.GantryError("the board did not report its status")

    def _poll(self) -> None:
        while True:
            time.sleep(0.3)
            if self.link is None or self.connecting:
                continue
            try:
                self.query_status()
            except gd.GantryError:
                pass                # a lost link is handled in _read; a slow answer is tried again
            except Exception:   # never let the poller die
                logging.exception("poll")

    def _lost(self, exc: Exception) -> None:
        link = self.link
        if link is None:
            return
        self.link, self.status = None, None
        self.position_lost = True
        self.say("error", f"The USB link to the board is gone ({exc}). Check the cable and the board's power, then Connect again.")
        try:
            link.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ connecting

    def connect(self, port: str) -> None:
        if self.link is not None:
            raise Busy("already connected; Disconnect first")
        self.start("Connecting", self._connect, port)

    def _connect(self, port: str) -> None:
        self.connecting = True
        self.too_old = ""
        try:
            if port == "sim":
                os.environ["GANTRY_SIM_SPEED"] = str(self.sim_speed)
            keep = bool(self.config.get("keep_esp32_running", False))
            self.say("info", f"Opening {port}" + (" (DTR/RTS held low: the ESP32 should keep running)" if keep and port != "sim"
                                                  else " (an ESP32 restarts when its port opens, about 2 s)"))
            link = gd.Link(port, int(self.config.get("baud", 115200)), log=logging.getLogger("link"), keep_running=keep)
            link.flip_y = gd.y_flip_mm(self.config)   # home beside a8: the link mirrors Y (gantry_driver.flip_line)
            link.swap_xy = gd.swap_xy_flag(self.config)
            self.link, self.port = link, port
            self.memory = link.memory
            for line in link.boot_lines:
                self.say("board", line)
            if link.restarted is not None:
                self.say("info", "The board restarted when its port opened." if link.restarted
                         else "The board kept running when its port opened.")
            self._use_sim_nudges(bool(getattr(link, "pretend", False)))   # the pretend board tunes a copy
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                line = link.read(0.1)
                if line:
                    self.say("board", line)
            info = self.exchange("$I")
            self.firmware = next((line.strip("[]") for line in info if line.startswith("[")), "unknown")
            self.board_settings = {}
            for line in self.exchange("$$"):
                key, _, rest = line.partition("=")
                try:
                    self.board_settings[key] = float(rest.split()[0].split(",")[0])
                except (ValueError, IndexError):
                    pass
            if "$111" not in self.board_settings:   # before 0.4: speeds and acceleration fixed, the console cannot drive it
                self.too_old = (f"The board runs {self.firmware}, too old for this console (it needs duen-gantry 0.4 or later). "
                                f"Pick its port and press Flash to put {self.firmware_source_version()} on it.")
                raise gd.GantryError(self.too_old)
            self.position_lost = False
            self.push_motion()
            self.say("info", f"Connected to {self.firmware} on {port}")
            self.push_tuning()
            self.restore_position()
            self.query_status()
        except Exception:
            if self.link is not None:
                try:
                    self.link.close()
                except Exception:
                    pass
            self.link, self.port = None, None
            self._use_sim_nudges(False)
            raise
        finally:
            self.connecting = False

    def restore_position(self) -> str:
        """After the link is configured: a restarted board is put back at its remembered spot ($HM, G92) when the
        file can be trusted; a board that kept running keeps its own count (gantry_driver.restore_position)."""
        if self.link is None:
            return "none"
        if self.config.get("manual_home_on_connect", False):
            square = self.config.get("home_square")
            where = f"beneath the centre of {square}" if square else "at the configured home spot"
            self.position_unsure("manual home required after connecting")
            self.say("warn", f"Place the magnet {where}, then press Set home here. No position was restored automatically.")
            return "manual"
        if self.memory is None:
            return "none"
        outcome, text = gd.restore_position(self.memory, self.config, self.exchange, self.query_status,
                                            logging.getLogger("console"), self.link.steps_per_mm)
        if outcome in ("restored", "kept"):
            self.position_lost = False
            self.say("info", text)
            if outcome == "kept" and self.link.doubt is None:
                data = self.memory.load() or {}
                if data.get("trusted") is False:
                    self.say("info", f"(The remembered position had been marked unsure: {data.get('reason')}. "
                                     "The board's own count is used and remembered again.)")
        else:
            self.say("warn", text)
        return outcome

    def position_unsure(self, reason: str) -> None:
        """The position may be wrong (a stall, a count that could not be put back): home before the next move, and
        do not put this spot back after a restart."""
        self.position_lost = True
        link = self.link
        if link is not None:
            link._untrust(reason)
        elif self.memory is not None:
            self.memory.untrust(reason)

    def forget_position(self) -> dict:
        """The page's Forget position: the remembered spot is not put back after the next restart (home then)."""
        memory = self.memory or gd.PositionMemory.for_port(self.port or str(self.config.get("port", "")))
        if memory is None:
            raise gd.GantryError("The pretend board keeps no remembered position.")
        reason = "forgotten on the console page"
        if self.link is not None and self.link.memory is memory:
            self.link._untrust(reason)
        else:
            memory.untrust(reason)
        self.memory = memory
        self.say("info", "Forgot the remembered position: after the board next restarts, home it once more.")
        return memory.describe()

    def position_memory(self) -> dict:
        if self.memory is None:
            return {"remembered": False, "saved": None, "reason": "", "text": "position not remembered"}
        return self.memory.describe()

    # ------------------------------------------------------------------ flashing the ESP32 from the page

    @staticmethod
    def firmware_source_version() -> str:
        try:
            m = re.search(r'#define\s+FIRMWARE_VERSION\s+"([^"]+)"', (FIRMWARE_DIR / "config.h").read_text())
            return m.group(1) if m else "unknown"
        except OSError:
            return "missing"

    def flash(self, port: str = "") -> None:
        """Compile firmware/gantry_firmware and upload it to the ESP32 on `port` (the connected one by default),
        then connect again. Only ever started by the person pressing Flash on the page and confirming."""
        port = self.port if self.link is not None else port.strip()
        if not port or port == "sim":
            raise gd.GantryError("Pick the ESP32's USB port (not sim) first.")
        if shutil.which("arduino-cli") is None:
            raise gd.GantryError("arduino-cli is not installed on this Mac (brew install arduino-cli).")
        self.start(f"Flash {self.firmware_source_version()}", self._flash, port)

    def _run_tool(self, args: list[str], what: str) -> None:
        self.progress(what)
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last = ""
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            last = line
            # esptool's "Writing at 0x... (n %)" lines: show as progress, log only every so often
            m = re.search(r"\((\d+) ?%\)", line)
            if m:
                with self.lock:
                    if self.task:
                        self.task["progress"] = f"{what}: {m.group(1)} %"
                if int(m.group(1)) % 25:
                    continue
            self.say("board", line[:300])
        if proc.wait() != 0:
            raise gd.GantryError(f"{what} failed: {last or 'see the log'}")

    def _flash(self, port: str) -> None:
        version = self.firmware_source_version()
        self.connecting = True          # the poller leaves the port alone
        try:
            link, self.link, self.status = self.link, None, None
            if link is not None:
                try:
                    link.write("M8")      # arm down before the board restarts into the bootloader
                except Exception:
                    pass
                link.close()
                self.say("info", f"Disconnected from {port} to flash it")
            self.position_lost = True
            arduino = shutil.which("arduino-cli")
            self._run_tool([arduino, "compile", "--fqbn", FQBN, str(FIRMWARE_DIR)], f"Compiling {version}")
            self._run_tool([arduino, "upload", "--fqbn", FQBN, "--board-options", f"UploadSpeed={UPLOAD_SPEED}",
                            "-p", port, str(FIRMWARE_DIR)], f"Uploading {version} to {port}")
            self.say("info", f"Flashed {version} onto {port}. Connecting again…")
        finally:
            self.connecting = False
        time.sleep(1.5)
        self._connect(port)
        if version not in self.firmware:
            self.say("warn", f"The board answers {self.firmware!r}, not {version!r}: the upload may not have taken.")
        else:
            self.say("info", f"The board runs {self.firmware}. Home it before moving (Motors off, push, Set home here).")

    def disconnect(self) -> None:
        if self.task:
            raise Busy(f"busy: {self.task['name']}; Stop first")
        link, self.link, self.status = self.link, None, None
        if self.sim_nudges is not None:
            self._use_sim_nudges(False)
        if link:
            link.close()
            self.say("info", "Disconnected")

    def push_motion(self) -> None:
        """Steps/mm ($100) and travel ($130, $131) from the config, and the step pulse ($0) when the config sets one.
        A new $100 makes the board forget its position (home again); firmware before 0.7 has no $130/$131."""
        lines = bl.motion_lines(self.config)
        if self.config.get("step_pulse_us"):
            lines.append(f"$0={int(self.config['step_pulse_us'])}")
        for line in lines:
            key, value = line.split("=")
            if abs(self.board_settings.get(key, -1.0) - float(value)) < 1e-4:
                continue
            try:
                self.exchange(line)
            except Refused as exc:
                if key in ("$130", "$131", "$0"):
                    self.say("warn", f"This firmware keeps its own {key} ({exc}); flash 0.7 for {line}")
                    continue
                raise
            self.board_settings[key] = float(value)
            if key == "$100":
                self.position_lost = True
        servo = bl.servo_line(self.config)
        if servo:
            try:
                self.exchange(servo)
                self.say("info", f"Arm angles from the config: {servo} (up, down, settle ms)")
            except Refused as exc:
                self.say("warn", f"The board refused {servo} ({exc}): the arm keeps its current angles")
                self.say("warn", f"Steps per mm set to {float(value):.4f} ({bl.mm_per_turn(self.config)[0]:g} mm per motor turn): "
                                 "the board forgot where it is. A remembered position is put back next when it can be; "
                                 "otherwise push the carriage into the home corner and Set home here.")

    def new_layout(self, board: chess.Board | None = None) -> cp.Layout:
        """The gantry never takes a piece off the board: a capture is a `remove` step that a person does by hand
        (on the Pi the camera warning asks for it), then the capturing piece is carried onto the emptied square.
        The docking lanes stay unused."""
        return cp.Layout(board, captures="remove")

    def save_config(self, new: dict, why: str, write: bool = True) -> None:
        """Write gantry_config.json (the old one kept once as .before-console.json) and use it from now on.
        write=False: use it, but leave the file alone (tuning on the pretend board)."""
        if write and self.config_path is not None:
            backup = self.config_path.with_suffix(".before-console.json")
            if not backup.exists() and self.config_path.exists():
                backup.write_text(self.config_path.read_text())
            self.config_path.write_text(json.dumps(new, indent=2) + "\n")
        with self.lock:
            self.config = new
            self._rebuild_map()
            self.layout.dock_lanes = tuple(bl.usable_lanes(new))
            self.layout.dock_slots = bl.slot_count(new)
        self.say("info", f"Saved the board config: {why}" if write else f"Pretend board, not saved to the file: {why}")

    def _rebuild_map(self) -> None:
        """The map the gantry moves by: the config, with the pretend board's own Tune answers in place of the
        real ones while it is connected."""
        config = self.config if self.sim_nudges is None else dict(self.config, square_nudges_mm=self.sim_nudges)
        self.map = gd.BoardMap(config)
        self.geometry = self.map.geometry(self.config)

    def _use_sim_nudges(self, on: bool) -> None:
        with self.lock:
            self.sim_nudges = dict(self.config.get("square_nudges_mm") or {}) if on else None
            self.sim_start = list(self.config["start_mm"]) if on and self.config.get("start_mm") else None
            self.tuning = None
            self._rebuild_map()

    def push_tuning(self) -> None:
        """Give the board the page's travel and carry speed limits ($110, $111) and acceleration ($120); move_to raises
        a limit when one move needs more."""
        for key, value in (("$110", self.settings["travel_speed"]), ("$111", self.settings["carry_speed"]),
                           ("$120", self.settings["accel"])):
            self.set_board(key, value, self.exchange)

    def set_board(self, key: str, value: float, send=None) -> None:
        """$110/$111/$120 to `value` (rounded up, within 1 and the ceiling) unless it is already there."""
        ceiling = CEILING_ACCEL if key == "$120" else CEILING_SPEED
        value = max(1.0, min(ceiling, math.ceil(value * 100) / 100 + (0.01 if key != "$120" else 0)))
        if abs(self.board_settings.get(key, -1) - value) > max(0.005, 0.005 * value):
            (send or self.cmd)(f"{key}={value:.2f}")
            self.board_settings[key] = value

    # ------------------------------------------------------------------ tasks

    def start(self, name: str, fn, *args) -> None:
        with self.lock:
            if self.task:
                raise Busy(f"busy: {self.task['name']}")
            self.stop_flag.clear()
            self.task = {"name": name, "progress": "", "started": time.time()}

        link = self.link

        def run() -> None:
            try:
                # a task's moves write "moving" once, and the spot once it is Idle afterwards (not at every stop)
                with (link.batch() if link is not None else contextlib.nullcontext()):
                    fn(*args)
            except Stopped:
                self.say("warn", f"{name}: stopped")
            except gd.GantryError as exc:
                self.say("error", f"{name}: {exc}")
                self._safe()
            except Exception as exc:   # a bug here must not leave the magnet on
                self.say("error", f"{name}: {exc!r}")
                logging.error(traceback.format_exc())
                self._safe()
            finally:
                with self.lock:
                    self.task, self.waiting = None, None

        threading.Thread(target=run, name=f"task-{name}", daemon=True).start()

    def progress(self, text: str) -> None:
        with self.lock:
            if self.task:
                self.task["progress"] = text
        self.say("step", text)

    def _safe(self) -> None:
        """Magnet off, arm down. Never raises."""
        for text in ("M5", "M8"):
            try:
                self.exchange(text, 1.0)
            except Exception:
                return

    def stop(self) -> None:
        self.stop_flag.set()
        if self.link is not None:
            try:
                self._write("!")
            except Exception:
                pass
        with self.lock:
            if self.waiting:
                self.waiting["event"].set()
        self.say("warn", "STOP: motion stopped, magnet off, arm down. Press Unlock to go on.")

    def check_stop(self) -> None:
        if self.stop_flag.is_set():
            raise Stopped()

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def cmd(self, text: str, timeout: float = 5.0) -> list[str]:
        self.check_stop()
        return self.exchange(text, timeout)

    def wait_idle(self, timeout: float, what: str) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            self.check_stop()
            st = self.query_status()
            if st["state"] == "Idle":
                return st
            if st["state"] == "Alarm":
                raise gd.GantryError(f"the board stopped during {what}: {st['reason'] or 'alarm'}")
            if st["state"] == "Hold":
                if self.stop_flag.is_set():
                    raise Stopped()
                raise gd.GantryError(f"the board was stopped (hold) during {what}")
            if time.monotonic() > deadline:
                self._write("!")
                raise gd.GantryError(f"{what} did not finish within {timeout:.0f} s; the board was stopped")
            time.sleep(0.04)

    def wait_person(self, prompt: str) -> None:
        event = threading.Event()
        with self.lock:
            self.waiting = {"prompt": prompt, "event": event}
        self.say("wait", prompt)
        while not event.wait(0.2):
            self.check_stop()
        self.check_stop()
        with self.lock:
            self.waiting = None

    def done(self) -> None:
        with self.lock:
            if not self.waiting:
                raise Busy("nothing is waiting for you")
            self.waiting["event"].set()

    def require(self, homed: bool = True) -> dict:
        if self.link is None:
            raise gd.GantryError("Connect to the board first.")
        st = self.query_status()
        if st["host_lost"]:
            raise gd.GantryError("The board lost contact with this computer during a move and made itself safe. Press Unlock.")
        if st["state"] == "Hold":
            raise gd.GantryError("The board is stopped (hold). Press Unlock.")
        if st["state"] == "Alarm" and st["reason"] not in ("", "not homed"):
            raise gd.GantryError(f"The board is in alarm: {st['reason']}. Check it, then Unlock and home.")
        if homed and (not st["homed"] or self.position_lost):
            square = self.config.get("home_square")
            where = f"beneath the centre of {square}" if square else "at the configured home spot"
            raise gd.GantryError(f"Not homed. Place the magnet {where} and press Set home here.")
        if st["state"] not in ("Idle", "Alarm"):
            raise gd.GantryError(f"The board is busy ({st['state']}).")
        return st

    # ------------------------------------------------------------------ motion in board mm (real mm since firmware 0.7)

    def accel_real(self) -> float:
        return self.settings["accel"]

    def carry_speed_real(self) -> float:
        return self.settings["carry_speed"]

    def step_rate_caps(self) -> tuple[float, float]:
        """Firmware 0.5+: the fastest each axis can go in mm/s, from the step pulse ($0 caps the step rate at
        1e6 / (2 x pulse)) and steps/mm. Infinite on older firmware."""
        pulse, steps_mm = self.board_settings.get("$0", 0.0), self.board_settings.get("$100", 40.0)
        if pulse <= 0 or steps_mm <= 0:
            return math.inf, math.inf
        cap = 1e6 / (2 * pulse) / steps_mm
        return cap, cap

    def leg_speed(self, a, b, speed: float, capped: bool = True) -> float:
        """The speed along a -> b (mm) that keeps each axis under its own cap, and under what the step pulse
        lets the firmware do (that one always: the board slows down by itself, so the plan must know)."""
        d = math.dist(a, b)
        if d < 1e-9:
            return speed
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        fx, fy = self.step_rate_caps()
        cx = min(fx, self.settings["x_speed_cap"]) if capped else fx
        cy = min(fy, self.settings["y_speed_cap"]) if capped else fy
        if dx > 1e-9:
            speed = min(speed, cx * d / dx)
        if dy > 1e-9:
            speed = min(speed, cy * d / dy)
        return speed

    def here(self) -> tuple[float, float]:
        st = self.status or {"x": 0.0, "y": 0.0}
        return st["x"], st["y"]

    def move_to(self, x: float, y: float, speed: float, what: str, carrying: bool = False, capped: bool = True) -> None:
        """G1 to a point in mm at a speed along the move, each axis under its cap; wait until there. The board's own
        speed limit ($110, or $111 with the arm up) is raised first when this move needs more."""
        self.map.check(x, y, what)
        here = self.here()
        v = self.leg_speed(here, (x, y), speed, capped)
        d = math.dist(here, (x, y))
        if v > self.board_settings.get("$110", 0):
            self.set_board("$110", v)
        if carrying and v > self.board_settings.get("$111", 0):
            self.set_board("$111", v)
        if d > 1e-9:
            self.set_board("$120", self.settings["accel"])
        self.cmd(f"G1 X{x:.3f} Y{y:.3f} F{v * 60:.1f}")
        self.wait_idle(5 + 3 * trapezoid_s(d, v, self.accel_real()), what)

    def arm_down(self) -> None:
        if (self.status or {}).get("servo") == "up":
            self.cmd("M8")
            self.sleep(self.settings["servo_settle"])

    def grab(self) -> None:
        self.cmd("M7")
        self.sleep(self.settings["servo_settle"])
        for attempt in range(3):
            try:
                self.cmd("M3")
                break
            except Refused as exc:
                if exc.code == 9 and attempt < 2:   # the magnet's minimum off time since the last M5
                    self.sleep(0.6)
                    continue
                raise
        self.sleep(self.settings["grab_settle"])

    def release(self) -> None:
        self.cmd("M5")                               # the firmware lowers the arm with every M5
        self.sleep(self.settings["release_settle"])
        self.cmd("M8")
        self.sleep(self.settings["servo_settle"])

    def hold_limit(self) -> float | None:
        """Seconds a piece may be held in one go, or None: no limit. This build lifts a permanent magnet with the
        servo, so nothing needs a rest, and firmware 0.7.2 says so ($G=0). Firmware that switches the magnet off
        after $G ms (15000 up to 0.7.1, or a board connected without $G) is kept 3 s under that; magnet_budget,
        when above 0, is a limit of its own."""
        caps = []
        if self.board_settings:   # connected: what this board enforces
            fw_ms = self.board_settings.get("$G", 15000.0)
            if fw_ms > 0:
                caps.append(fw_ms / 1000.0 - 3.0)
        if self.settings["magnet_budget"] > 0:
            caps.append(self.settings["magnet_budget"])
        return min(caps) if caps else None

    def holds(self, points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
        """The carry legs grouped so a piece is never held longer than hold_limit(): a group ends with a rest
        (the piece set down where it is, on its clear path, and picked up again). A leg longer than the
        limit is cut into equal parts first. No limit: one group, no rests."""
        limit = self.hold_limit()
        if limit is None:
            return [list(points)]
        accel = self.accel_real()
        budget = limit - self.settings["grab_settle"]
        pts = [points[0]]
        for b in points[1:]:
            a = pts[-1]
            d = math.dist(a, b)
            speed = self.leg_speed(a, b, self.carry_speed_real())
            parts = 1
            while trapezoid_s(d / parts, speed, accel) + POLL_LEG_S > budget * 0.9:
                parts += 1
            for k in range(1, parts + 1):
                pts.append((a[0] + (b[0] - a[0]) * k / parts, a[1] + (b[1] - a[1]) * k / parts))
        groups, current, used = [], [pts[0]], 0.0
        for a, b in zip(pts, pts[1:]):
            leg = trapezoid_s(math.dist(a, b), self.leg_speed(a, b, self.carry_speed_real()), accel) + POLL_LEG_S
            if used + leg > budget and len(current) > 1:
                groups.append(current)
                current, used = [a], 0.0
            current.append(b)
            used += leg
        groups.append(current)
        return groups

    def carry(self, step: dict) -> None:
        points = [self.map.point_mm(float(px), float(py)) for px, py in step["path"]]
        for (x, y), p in zip(points, step["path"]):
            self.map.check(x, y, f"point {p} of {step['from']}->{step['to']}")
        label = gd.describe_step(step)
        self.arm_down()
        self.move_to(*points[0], self.settings["travel_speed"], f"going to {step['from']}")
        groups = self.holds(points)
        past = self.overshoot_point(points)
        if past:
            groups[-1] += [past, points[-1]]   # well under a second more (hold_limit keeps 3 s spare)
        for i, group in enumerate(groups):
            if i:
                self.say("info", "Resting the magnet: setting the piece down on its path and picking it up again")
            self.grab()
            for x, y in group[1:]:
                self.move_to(x, y, self.carry_speed_real(), label, carrying=True)
            self.release()

    def overshoot_point(self, points: list[tuple[float, float]]) -> tuple[float, float] | None:
        """Where the magnet goes past the last point, along the last leg, so the trailing piece is pulled onto the
        square; None when the setting is 0 or that point is outside the travel or the magnet's reach."""
        d = self.settings.get("overshoot", 0.0)
        (ax, ay), (bx, by) = points[-2], points[-1]
        leg = math.dist((ax, ay), (bx, by))
        if d <= 0 or leg < 1e-6:
            return None
        x, y = bx + (bx - ax) / leg * d, by + (by - ay) / leg * d
        if not (0 <= x <= self.map.travel[0] and 0 <= y <= self.map.travel[1]) or not self.geometry.reachable(x, y):
            return None
        return x, y

    # ------------------------------------------------------------------ planning

    def best_slot(self, layout: cp.Layout, square: str) -> str | None:
        """The free docking slot with the shortest clear way from `square`, in the taken piece's own lane first (white
        left, black right), then the other; None when none has a clear way (the planner then takes the first free slot
        and the path finder makes room)."""
        world = bp.Board(self.geometry, set(layout.pieces), pieces=dict(layout.pieces))
        free = layout.free_slots(layout.pieces.get(square))
        if not free:
            return None
        own = [s for s in free if s[0] == free[0][0]]
        return world.best_slot(square, own) or world.best_slot(square, [s for s in free if s not in own])

    def make_preview(self, kind: str, label: str, steps: list[dict], before: cp.Layout, after: cp.Layout,
                     game_after: chess.Board | None, extra: dict | None = None) -> dict:
        job = cp.job_for_steps(before, after, label, steps)
        job["any_order"] = kind == "reset"          # a blocked piece goes home later in the reset instead of failing
        routed = gd.route(job, self.map, self.config)
        carries = [s for s in routed if s["op"] == "carry"]
        gd.check_carry_count(len(carries), self.config, label)
        shown, seconds, rests = [], 0.0, 0
        here = self.here() if self.status else (0.0, 0.0)
        travel, carry_v, accel = self.settings["travel_speed"], self.carry_speed_real(), self.accel_real()
        overhead = 2 * self.settings["servo_settle"] + self.settings["grab_settle"] + self.settings["release_settle"] + 0.3
        for step in routed:
            item = {"op": step["op"], "text": self._step_text(step)}
            if step["op"] == "carry":
                pts = [self.map.point_mm(float(px), float(py)) for px, py in step["path"]]
                for (x, y), p in zip(pts, step["path"]):
                    self.map.check(x, y, f"point {p} of {step['from']}->{step['to']}")
                item.update(piece=step.get("piece"), frm=step["from"], to=step["to"], reason=step.get("reason", "move"),
                            points=[[round(x, 2), round(y, 2)] for x, y in pts])
                seconds += trapezoid_s(math.dist(here, pts[0]), self.leg_speed(here, pts[0], travel), accel) + overhead
                seconds += sum(trapezoid_s(math.dist(a, b), self.leg_speed(a, b, carry_v), accel) + POLL_LEG_S
                               for a, b in zip(pts, pts[1:]))
                groups = self.holds(pts)
                rests += len(groups) - 1
                seconds += (len(groups) - 1) * overhead
                if self.overshoot_point(pts):
                    seconds += 2 * (trapezoid_s(self.settings["overshoot"], carry_v, accel) + POLL_LEG_S)
                here = pts[-1]
            elif step["op"] == "remove":
                item.update(piece=step.get("piece"), square=step["square"])
            shown.append(item)
        preview = {"kind": kind, "label": label, "steps": shown, "routed": routed, "carries": len(carries),
                   "seconds": round(seconds, 1), "rests": rests, "before": before, "after": after,
                   "game_after": game_after, "layout_at": dict(self.layout.pieces), "made": time.time()}
        preview.update(extra or {})
        return preview

    @staticmethod
    def _step_text(step: dict) -> str:
        if step["op"] == "note":
            return step.get("message", "")
        if step["op"] == "remove":
            return f"Take {gd.piece_words(step.get('piece'))} off {step['square']} by hand (the gantry never removes pieces)"
        if step["op"] == "place":
            return f"Put {gd.piece_words(step.get('piece'))} back on {step['square']} by hand"
        text = gd.describe_step(step)
        if step.get("reason") == "capture":
            text = f"Capture: {text[0].lower()}{text[1:]}"
        return text

    def plan_move(self, src: str, dst: str, promotion: str | None, mode: str, *, bot_move: bool = False) -> dict:
        with self.lock:
            if self.task and not bot_move:
                raise Busy(f"busy: {self.task['name']}")
            if bot_move and (self.bot is None or not self.bot.enabled or self.game.turn != chess.BLACK or mode != "chess"):
                raise gd.GantryError("Only the bot can request a Black chess move.")
            if self.bot is not None and not bot_move:
                if mode == "chess":
                    if not self.bot.enabled:
                        raise gd.GantryError("Resume the Black bot before recording White's move.")
                    if self.game.turn != chess.WHITE:
                        raise gd.GantryError("Black is controlled by the bot. Use Play Black reply.")
                    self.require(homed=True)
                elif self.bot.enabled:
                    raise gd.GantryError("Pause the bot before using Free carry.")
            if self.pending:
                raise Busy("a move stopped part-way: choose Retry, Pieces as before, or Finished by hand first")
            before = self.layout.copy()
            after = self.layout.copy()
            if mode == "free":
                if src not in after.pieces:
                    raise gd.GantryError(f"nothing on {src}")
                if dst in after.pieces:
                    raise gd.GantryError(f"{dst} is not empty")
                game_after = chess_from_layout_move(self.game, after, src, dst)
                steps = [after.carry(src, dst)]
                label = f"free {src}-{dst}"
                extra = {"san": label}
            else:
                if ":" in src or ":" in dst:
                    raise gd.GantryError("chess moves are between squares; use Free mode for the docking lane")
                uci = src + dst + (promotion or "")
                move = chess.Move.from_uci(uci)
                if move not in self.game.legal_moves and not promotion:
                    queen = chess.Move.from_uci(uci + "q")
                    if queen in self.game.legal_moves:
                        move = queen
                if move not in self.game.legal_moves:
                    raise gd.GantryError(f"{src}-{dst} is not a legal move here")
                if src not in after.pieces:
                    raise gd.GantryError(f"the console has no piece on {src} (the board layout and the game disagree; "
                                         "use Free mode or New game)")
                manual_black = self.bot is None and self.config.get("manual_black_moves", False) and self.game.turn == chess.BLACK
                manual_white = self.bot is not None and self.game.turn == chess.WHITE
                if manual_black or manual_white:
                    after.apply_human(self.game, move)
                    steps = []
                else:
                    taken = cp.removal(self.game, move)
                    slot = self.best_slot(after, taken[0]) if taken else None
                    steps = after.plan_computer(self.game, move, slot)
                game_after = self.game.copy()
                san = self.game.san(move)
                game_after.push(move)
                label = move.uci()
                extra = {"san": san, "uci": move.uci(), "manual_only": bool(manual_black or manual_white)}
            try:
                preview = self.make_preview("free" if mode == "free" else "move", label, steps, before, after, game_after, extra)
            except gd.GantryError as exc:
                if mode == "free":
                    raise
                preview = self.fallback_preview(move, label, steps, before, game_after, extra, exc)
            preview.setdefault("job_steps", steps)
            self.preview = preview
            return preview

    def fallback_preview(self, move: chess.Move, label: str, steps: list[dict], before: cp.Layout,
                         game_after: chess.Board, extra: dict, exc: Exception) -> dict:
        """No clear way for the plan as made (a crowded corner): the promotion without the docked spare, then the
        captured piece taken off by hand too (cp.FALLBACKS)."""
        docked = next((s for s in steps if s.get("op") == "carry" and s.get("reason") == "capture"), None)
        spare = next((s for s in steps if s.get("op") == "carry" and s.get("reason") == "promotion" and ":" in s["from"]), None)
        for options in cp.FALLBACKS[1:]:
            if options.get("by_hand") and not docked:
                continue
            if not options.get("by_hand") and not spare:
                continue
            after = before.copy()
            tried = after.plan_computer(self.game, move, **options)
            try:
                preview = self.make_preview("move", label, tried, before, after, game_after, dict(extra))
            except gd.GantryError:
                continue
            why = []
            if spare:
                why.append(f"the docked piece on {spare['from']} has no clear way out, so the pawn goes to {move.uci()[2:4]}: swap it by hand")
            if options.get("by_hand"):
                why.append(f"no clear way from {docked['from']} to the docking lane: take that piece off by hand")
            preview["warning"] = f"{'; '.join(why)} ({exc})."
            preview["warning"] = preview["warning"][0].upper() + preview["warning"][1:]
            preview["job_steps"] = tried
            return preview
        raise exc

    def plan_undo(self) -> dict:
        with self.lock:
            if self.task:
                raise Busy(f"busy: {self.task['name']}")
            if not self.history:
                raise gd.GantryError("nothing to undo")
            last = self.history[-1]
            before, after = self.layout.copy(), self.layout.copy()
            if last.get("manual_only", False):
                after = last["layout_before"].copy()
                preview = self.make_preview("undo", f"undo {last['label']}", [], before, after, last["game_before"],
                                            {"san": f"undo {last['san']}", "manual_only": True})
                preview["job_steps"] = []
                self.preview = preview
                return preview
            steps: list[dict] = []
            for step in reversed(last["job_steps"]):
                if step["op"] == "carry":
                    steps.append(after.carry(step["to"], step["from"]))
                elif step["op"] == "remove":
                    after.pieces[step["square"]] = step["piece"]
                    steps.append({"op": "place", "piece": step["piece"], "square": step["square"]})
            preview = self.make_preview("undo", f"undo {last['label']}", steps, before, after, last["game_before"],
                                        {"san": f"undo {last['san']}"})
            preview["job_steps"] = steps
            self.preview = preview
            return preview

    def plan_reset(self) -> dict:
        with self.lock:
            if self.task:
                raise Busy(f"busy: {self.task['name']}")
            before, after = self.layout.copy(), self.layout.copy()
            steps = after.plan_reset()
            preview = self.make_preview("reset", "reset", steps, before, after, chess.Board(), {"san": "reset"})
            preview["job_steps"] = steps
            self.preview = preview
            return preview

    def cancel_preview(self) -> None:
        with self.lock:
            self.preview = None

    # ------------------------------------------------------------------ running a planned job

    def run_preview(self, by_hand: bool = False) -> None:
        with self.lock:
            preview = self.preview
            if not preview:
                raise gd.GantryError("nothing planned")
            if preview["layout_at"] != self.layout.pieces:
                self.preview = None
                raise gd.GantryError("the board changed since this plan; make the move again")
            if by_hand or preview.get("manual_only", False):
                self._commit(preview)
                self.say("info", f"{preview['san']}: played without the gantry (you moved the pieces)")
                return
            if self.link is None:
                raise gd.GantryError("Connect to the board first (or use I moved it by hand)")
            if self.task:
                raise Busy(f"busy: {self.task['name']}")
        self.require(homed=True)          # answer "not homed" or "press Unlock" on the page at once
        self.start(f"Playing {preview['san']}", self._run_job, preview, 0)

    def _run_job(self, preview: dict, first: int) -> None:
        routed = preview["routed"]
        try:
            self.require(homed=True)
        except BaseException:
            if first:                     # a retry that could not start: still stopped part-way
                with self.lock:
                    self.pending = {"preview": preview, "next": first, "total": len(routed)}
            raise
        with self.lock:
            self.pending = None
        notes = []
        try:
            for i in range(first, len(routed)):
                step = routed[i]
                self.progress(f"{i + 1}/{len(routed)}: {self._step_text(step)}")
                if step["op"] == "note":
                    notes.append(step.get("message", ""))
                    continue
                if step["op"] in ("remove", "place"):
                    self.wait_person(self._step_text(step) + ", then press Done.")
                    with self.lock:
                        if step["op"] == "remove":
                            self.layout.pieces.pop(step["square"], None)
                        else:
                            self.layout.pieces[step["square"]] = step["piece"]
                    continue
                self.carry(step)
                with self.lock:
                    self.layout.pieces[step["to"]] = self.layout.pieces.pop(step["from"])
                    preview["next"] = i + 1
            if notes:
                self.wait_person("Do these by hand, then press Done: " + " ".join(notes))
        except BaseException:
            with self.lock:
                done = preview.get("next", first)
                self.pending = {"preview": preview, "next": done, "total": len(routed)}
                self.preview = None
            self.say("warn", f"{preview['san']} stopped after {done} of {len(routed)} steps. The page shows what to do next.")
            raise
        with self.lock:
            self._commit(preview)
        self.say("info", f"{preview['san']}: done")

    def _commit(self, preview: dict) -> None:
        kind = preview["kind"]
        self.layout = preview["after"].copy()
        if kind == "move" or kind == "free":
            self.history.append({"label": preview["label"], "san": preview["san"], "job_steps": preview["job_steps"],
                                 "game_before": self.game.copy(), "layout_before": preview["before"].copy(),
                                 "manual_only": preview.get("manual_only", False)})
            self.game = preview["game_after"]
        elif kind == "undo":
            self.history.pop()
            self.game = preview["game_after"]
        elif kind == "reset":
            self.layout = self.new_layout()
            self.game = chess.Board()
            self.history = []
        self.preview, self.pending = None, None

    def resolve(self, action: str) -> None:
        with self.lock:
            pending = self.pending
            if not pending:
                raise gd.GantryError("nothing stopped part-way")
            preview = pending["preview"]
            if action == "retry":
                if self.task:
                    raise Busy(f"busy: {self.task['name']}")
                self.require(homed=True)
                self.pending = None
                self.preview = None
                self.start(f"Finishing {preview['san']}", self._run_job, preview, pending["next"])
                return
            if action == "before":
                self.layout = preview["before"].copy()
                self.say("info", f"{preview['san']}: you put the pieces back as before the move")
            elif action == "after":
                self._commit(preview)
                self.say("info", f"{preview['san']}: you finished it by hand")
                return
            else:
                raise gd.GantryError(f"unknown choice {action!r}")
            self.pending, self.preview = None, None

    def new_game(self) -> None:
        with self.lock:
            if self.task:
                raise Busy(f"busy: {self.task['name']}")
            self.game, self.layout, self.history = chess.Board(), self.new_layout(), []
            self.preview, self.pending = None, None
        self.say("info", "New game: the pieces are in the starting position")

    # ------------------------------------------------------------------ manual control

    def unlock(self) -> None:
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        self.exchange("$X")
        self.query_status()

    def motors_off(self) -> None:
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        self.exchange("M18")
        self.say("info", "Motors off: the carriage can be pushed by hand. Any move switches them on again.")

    def home_manual(self) -> None:
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        self.exchange("$HM")
        spot = gd.home_spot_mm(self.config)          # the person homes at a fixed spot off the corner (shared with the screen)
        if spot:
            hx, hy = self.home_xy()
            self.exchange(f"G92 X{hx:.3f} Y{hy:.3f}")  # Link.write mirrors Y beside a8
        self.position_lost = False
        self.query_status()
        corner = "beside a8" if self.link is not None and self.link.flip_y is not None else "the bottom-left corner, beside a1"
        square = self.config.get("home_square")
        where = f"the centre of {square}" if square else (f"the home spot, {spot[0]:g} mm and {spot[1]:g} mm off the corner {corner}" if spot else f"the corner {corner}")
        self.say("info", f"Home set: this spot is home ({where})")
        if self.start_mm() and not square:
            self.start("Go to start", self._go_start)

    def home_bump(self) -> None:
        self.start("Bump homing", self._home_bump)

    def _home_bump(self) -> None:
        self.cmd("$H")
        self.wait_idle(120, "homing")
        self.position_lost = False
        self.say("info", "Homed by bumping into the bottom-left corner")
        if self.start_mm():
            self._go_start()

    def return_home(self) -> None:
        self.start("Return home", self._return_home)

    def home_xy(self) -> tuple[float, float]:
        """Home as this side counts: 0,0, or 0,travel when home is beside a8 (the link mirrors Y)."""
        return gd.home_xy(self.config)

    def _return_home(self) -> None:
        """Back to home from wherever the board's own step count says the carriage is, in one straight line
        (both motors at once: the shortest way), with the magnet off and the arm down so nothing is dragged."""
        st = self.require(homed=True)
        if st.get("magnet"):
            self.cmd("M5")
        self.arm_down()
        hx, hy = self.here()
        tx, ty = self.home_xy()
        dx, dy = abs(hx - tx), abs(hy - ty)
        if dx < 0.01 and dy < 0.01:
            self.say("info", "Already home.")
            return
        per_mm = self.board_settings.get("$100")
        steps = f" ({dx * per_mm:.0f} and {dy * per_mm:.0f} motor steps)" if per_mm else ""
        self.say("info", f"Returning home: {dx:.1f} mm along X and {dy:.1f} mm along Y{steps}, in one straight line.")
        self.move_to(tx, ty, self.settings["travel_speed"], "returning home")
        self.say("info", "Home.")

    def jog(self, dx: float, dy: float) -> None:
        with self.lock:
            if self.task and self.task["name"] == "Jog":     # clicked again while it moves: do it next
                self.jog_queue.append((dx, dy))
                return
        self.start("Jog", self._jog, dx, dy)

    def manual_speed(self) -> float:
        """Jog and Go to leave the arm where the user put it. Up, the board only allows the carry speed ($111),
        and a raised magnet may be holding a piece, so the move goes at that speed."""
        up = (self.query_status() or {}).get("servo") == "up"
        return self.settings["carry_speed"] if up else self.settings["travel_speed"]

    def _jog(self, dx: float, dy: float) -> None:
        st = self.require(homed=False)
        speed = self.manual_speed()   # the arm stays as it is: up stays up
        todo = [(dx, dy)]
        while todo:
            dx, dy = todo.pop(0)
            hx, hy = self.here()
            if not st["homed"]:
                if dx < 0 or dy < 0:
                    raise gd.GantryError("Not homed: the board only jogs in +X and +Y until it is homed")
                if st["state"] == "Alarm":
                    self.cmd("$X")
                    st = self.query_status()
            tx, ty = hx + dx, hy + dy
            # Home reads a step's fraction past the edge (Y 420.08 with home beside a8): within 1 mm, the edge itself.
            if -1.0 < tx < 0.0: tx = 0.0
            if -1.0 < ty < 0.0: ty = 0.0
            if self.map.travel[0] < tx < self.map.travel[0] + 1.0: tx = self.map.travel[0]
            if self.map.travel[1] < ty < self.map.travel[1] + 1.0: ty = self.map.travel[1]
            self.move_to(tx, ty, speed, "jog")
            with self.lock:
                todo += self.jog_queue
                self.jog_queue.clear()
                if not todo and self.task:
                    self.task["name"] = "Jog (finishing)"   # a click from now on starts a new jog

    def goto(self, place: str) -> None:
        x, y = self.map.place_mm(place.strip().lower() if ":" not in place else place.strip())
        self.start(f"Go to {place}", self._goto, x, y, place)

    def _goto(self, x: float, y: float, place: str) -> None:
        self.require(homed=True)
        self.move_to(x, y, self.manual_speed(), f"going to {place}")   # the arm stays as it is

    def servo(self, up: bool) -> None:
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        self.exchange("M7" if up else "M8")

    def magnet(self, on: bool) -> None:
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        self.exchange("M3" if on else "M5")

    def raw(self, line: str) -> list[str]:
        line = line.strip()
        if line == "!":
            self.stop()
            return []
        if line == "?":   # answered with a status line, never an ok: exchange would wait 10 s for one
            return [self.query_status().get("raw", "")]
        if self.task:
            raise Busy(f"busy: {self.task['name']} (Stop first)")
        return self.exchange(line, 10.0)

    def save_settings(self, values: dict) -> None:
        with self.lock:
            if self.task:
                raise Busy(f"busy: {self.task['name']}")
            new = dict(self.settings)
            for key, value in values.items():
                if key not in DEFAULTS:
                    continue
                if key == "auto_run":
                    new[key] = bool(value)
                    continue
                lo, hi = LIMITS[key]
                number = float(value)
                if not lo <= number <= hi:
                    raise gd.GantryError(f"{key} must be between {lo:g} and {hi:g}")
                new[key] = number
            self.settings = new
            SETTINGS_FILE.write_text(json.dumps(new, indent=2) + "\n")
        if self.link is not None:
            self.push_tuning()
        self.say("info", "Settings saved")

    # ------------------------------------------------------------------ stall checks

    def one_turn(self, axis: str, direction: int) -> None:
        if axis not in ("x", "y") or direction not in (1, -1):
            raise gd.GantryError("choose X or Y, forward or back")
        self.start(f"One turn {axis.upper()}{'+' if direction > 0 else '-'}", self._one_turn, axis, direction)

    def _one_turn(self, axis: str, direction: int) -> None:
        """Sends exactly one motor turn of steps on one axis (200 full steps x the config's microsteps): the pulley
        turns once and the carriage moves mm_per_turn (41 mm on the bench). The person measures how far it went.
        + is away from home (X toward the h-file, Y toward rank 8), - is toward home.

        No room check and no home needed: the person is standing at the gantry running it by hand. The board's own
        soft limits (0..$130 / $131) would refuse a turn that crosses them, so near an edge the spot is relabelled
        (G92) for the turn and the true count put back after it."""
        st = self.require(homed=False)   # the arm stays as it is: a turn at 12 mm/s is under the carry speed
        if st["state"] == "Alarm":
            self.cmd("$X")
            st = self.query_status()
        i = 0 if axis == "x" else 1
        shown = direction                # what the page asked for: + away from home, - toward it
        if axis == "y" and self.link is not None and self.link.flip_y is not None:
            direction = -direction       # home beside a8: away from home is -Y as this side counts
        steps = bl.steps_per_turn(self.config)
        steps_mm = self.board_settings.get("$100", bl.steps_per_mm(self.config))
        counted = steps / steps_mm
        expected = bl.mm_per_turn(self.config)[i]
        AX = axis.upper()
        here = st["x"] if axis == "x" else st["y"]
        limit = self.board_settings.get("$130" if axis == "x" else "$131", self.map.travel[i])
        if counted > limit:
            raise gd.GantryError(f"One turn is {counted:.0f} mm as the board counts ($100={steps_mm:g}), more than its whole "
                                 f"{limit:.0f} mm: reconnect so the console sets $100 first.")
        start = here
        if not -0.001 <= here + direction * counted <= limit + 0.001:      # the board would refuse
            if st["homed"]:
                # Homed, the count is true: a turn past the end can only drive the carriage into the frame, and the
                # count put back afterwards would then be wrong (it counts steps, not how far the carriage got).
                toward = "home" if shown < 0 else "the far end"
                raise gd.GantryError(f"No room for one turn {AX}{'+' if shown > 0 else '-'}: the carriage is "
                                     f"{abs(here - (0.0 if here + direction * counted < 0 else limit)):.0f} mm from "
                                     f"{toward}, one turn is {counted:.0f} mm. Jog or turn the other way first.")
            start = 0.0 if direction > 0 else counted                      # not homed: relabel the spot for the turn
        with self.lock:
            self.tests["turn"] = {"axis": axis, "direction": shown, "steps": round(steps), "counted": round(counted, 3),
                                  "expected": expected, "state": "running", "turns": None, "measured": None}
        self.progress(f"One turn on {AX}{'+' if shown > 0 else '-'}: {steps:.0f} steps. Measure how far the carriage goes.")
        if start != here:
            self.cmd(f"G92 {AX}{start:.3f}")
        try:
            self.cmd(f"G1 {AX}{start + direction * counted:.3f} F{TURN_SPEED * 60:.1f}")
            self.wait_idle(10 + 3 * counted / TURN_SPEED, "the one-turn test")
        finally:
            if start != here:                                              # put the true count back, even after Stop
                try:
                    now = self.query_status()
                    moved = (now["x"] if axis == "x" else now["y"]) - start
                    self.exchange(f"G92 {AX}{here + moved:.3f}", 2.0)
                except Exception:
                    self.position_unsure("the one-turn test could not put the count back")
        with self.lock:
            self.tests["turn"]["state"] = "measure"
        self.say("info", f"One turn on {AX}: sent {steps:.0f} steps ({counted:.2f} mm as the board counts). It should move "
                         f"{expected:g} mm. Enter how far it really moved.")

    def turn_result(self, turns: float | None = None, measured: float | None = None) -> float:
        """mm per turn from what the person saw: millimetres the carriage moved for one turn's steps (or turns of the
        pulley, which only says whether the microstep setting is right)."""
        with self.lock:
            test = self.tests.get("turn")
            if not test or test["state"] not in ("measure", "done"):
                raise gd.GantryError("run One turn first")
            axis = test["axis"].upper()
            expected = test["expected"]
            if measured:
                if not 0.5 <= measured <= 2000:
                    raise gd.GantryError("enter how far it moved in mm")
                mm = measured
            elif turns:
                if not 0.01 <= turns <= 50:
                    raise gd.GantryError("enter the turns you saw (for example 1, 8 or 0.125)")
                mm = expected * turns
            else:
                raise gd.GantryError("enter how far it moved in mm")
            ratio = mm / expected
            if abs(ratio - 1) < 0.01:
                hint = f"Matches: {axis} moves {mm:g} mm per turn, as the config says. Nothing to change."
            elif abs(ratio - 8) < 0.5 or abs(ratio - 0.125) < 0.02 or abs(ratio - 2) < 0.1 or abs(ratio - 0.5) < 0.03:
                hint = (f"A factor of {ratio:g}: the {axis} driver's microstep pins (M0-M2) are not what the config's "
                        f"\"microsteps\" ({self.config.get('microsteps', 1)}) says. Fix the pins or the config, not mm per turn.")
            else:
                hint = (f"{axis} moves {mm:g} mm per turn, not {expected:g}. Use it so every move is right; measure over a "
                        "longer run (Distance check, 300 mm) for a better number.")
            test.update(turns=turns, measured=measured, ratio=round(ratio, 4), mm_per_turn=round(mm, 3), state="done", hint=hint)
        self.say("info", f"One turn on {axis}: {mm:g} mm. {hint}")
        return mm

    def use_mm_per_turn(self, axis: str, mm: float) -> None:
        """Save mm per turn for one axis in the config. X's sets the board's steps/mm ($100), so the board forgets its
        position (home again); Y's is kept for the record and absorbed by the board's measured squares."""
        if axis not in ("x", "y") or not 5 <= mm <= 200:
            raise gd.GantryError("choose X or Y and 5 to 200 mm per turn")
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        x, y = bl.mm_per_turn(self.config)
        new = dict(self.config, mm_per_turn=[round(mm, 3), y] if axis == "x" else [x, round(mm, 3)])
        self.save_config(new, f"{axis.upper()} moves {mm:g} mm per motor turn")
        if self.link is not None:
            self.push_motion()

    def distance_check(self, axis: str, mm: float) -> None:
        if axis not in ("x", "y") or not 1 <= mm <= 400:
            raise gd.GantryError("choose X or Y and 1 to 400 mm")
        self.start(f"Distance check {axis.upper()}", self._distance, axis, mm)

    def _distance(self, axis: str, mm: float) -> None:
        st = self.require(homed=False)
        self.arm_down()
        if st["state"] == "Alarm":
            self.cmd("$X")
            st = self.query_status()
        bx, by = st["x"], st["y"]
        target = f"X{bx + mm:.3f} Y{by:.3f}" if axis == "x" else f"X{bx:.3f} Y{by + mm:.3f}"
        with self.lock:
            self.tests["distance"] = {"axis": axis, "mm": mm, "state": "running", "measured": None}
        self.progress(f"Moving {axis.upper()} by {mm:g} mm as the board counts it (10 mm/s). Measure how far it really went.")
        self.cmd(f"G1 {target} F600")
        self.wait_idle(10 + 3 * mm / 10, "the distance check")
        with self.lock:
            self.tests["distance"]["state"] = "measure"
        self.say("info", f"Distance check: the board counted {mm:g} mm on {axis.upper()}. Measure the real distance and enter it.")

    def distance_result(self, measured: float) -> float:
        """mm per turn for that axis from a longer run: the config's value x measured / counted."""
        with self.lock:
            test = self.tests.get("distance")
            if not test:
                raise gd.GantryError("run the distance check first")
            if not 0.5 <= measured <= 2000:
                raise gd.GantryError("enter the measured distance in mm")
            i = 0 if test["axis"] == "x" else 1
            ratio = measured / test["mm"]
            mm = bl.mm_per_turn(self.config)[i] * ratio
            if abs(ratio - 1) < 0.003:
                hint = "Matches to 0.3 %: nothing to change."
            elif abs(ratio - 8) < 0.8 or abs(ratio - 0.125) < 0.02:
                hint = "About 8x: the driver's microstep pins and the config's microsteps disagree. Fix those, not mm per turn."
            else:
                hint = f"{ratio:.4f} of what the board counted: use {mm:.3f} mm per turn for {test['axis'].upper()}."
            test.update(measured=measured, ratio=round(ratio, 5), mm_per_turn=round(mm, 3), state="done", hint=hint)
        self.say("info", f"Distance check: {measured:g} mm real for {test['mm']:g} counted. {hint}")
        return mm

    def ladder(self, axis: str, distance: float, speeds: list[float]) -> None:
        if axis not in ("x", "y"):
            raise gd.GantryError("choose X or Y")
        speeds = sorted({float(v) for v in speeds if 1 <= float(v) <= CEILING_SPEED})
        if not speeds:
            raise gd.GantryError("no speeds to try")
        self.start(f"Speed ladder {axis.upper()}", self._ladder, axis, float(distance), speeds)

    def _ladder(self, axis: str, distance: float, speeds: list[float]) -> None:
        st = self.require(homed=True)
        self.arm_down()
        hx, hy = st["x"], st["y"]
        out = (hx + distance, hy) if axis == "x" else (hx, hy + distance)
        self.map.check(*out, "the far end of the speed ladder")
        test = {"axis": axis, "distance": distance, "speeds": speeds, "at": None, "ok": [], "stalled": None, "done": False}
        with self.lock:
            self.tests["ladder"] = test
        try:                           # move_to raises $110 as the ladder climbs; put it back after
            for v in speeds:
                test["at"] = v
                self.progress(f"Speed ladder {axis.upper()}: {v:g} mm/s out and back ({distance:g} mm). Press It stalled if it buzzes or skips.")
                self.move_to(*out, v, f"ladder {v:g} mm/s out", capped=False)
                self.sleep(0.4)
                self.move_to(hx, hy, v, f"ladder {v:g} mm/s back", capped=False)
                self.sleep(0.8)
                test["ok"].append(v)
            test["done"] = True
            test["at"] = None
            self.say("info", f"Speed ladder {axis.upper()}: every speed up to {speeds[-1]:g} mm/s ran without a stall being reported")
        finally:
            try:
                self.push_tuning()
            except Exception:
                pass

    def stalled(self) -> dict:
        with self.lock:
            test = self.tests.get("ladder")
            if not test or test.get("done") or test.get("at") is None:
                raise gd.GantryError("no speed ladder is running")
            test["stalled"] = test["at"]
            good = test["ok"][-1] if test["ok"] else None
            test["recommend"] = {f"{test['axis']}_speed_cap": round(0.7 * good) if good else None,
                                 "carry_speed": round(min(0.6 * good, self.settings["carry_speed"])) if good else None}
        self.stop()
        self.position_unsure("a stall was reported")
        self.say("warn", f"Stall reported at {test['stalled']:g} mm/s on {test['axis'].upper()}. The carriage may have slipped: "
                         "push it into the bottom-left corner and Set home here before the next move.")
        return test

    # ------------------------------------------------------------------ the board itself

    def check_board(self) -> None:
        self.start("Check the board", self._check_board)

    def _check_board(self) -> None:
        """The magnet visits a1, h1, h8, a8 and the first and last slot of each docking lane in use, and waits at each
        for the person to look: is it under the centre? Nothing is carried."""
        self.require(homed=True)
        self.arm_down()
        places = ["a1", "h1", "h8", "a8"]
        for side in bl.usable_lanes(self.config):
            n = len(self.map.park_rows)
            places += [f"{side}:0", f"{side}:{n - 1}"]
        for place in places:
            x, y = self.map.place_mm(place)
            self.move_to(x, y, self.settings["travel_speed"], f"going to {place}")
            sx, sy = bl.to_sketch(self.config, x, y)
            self.wait_person(f"Is the magnet under the centre of {place}? (it is at {x:.1f}, {y:.1f} as the board counts, "
                             f"{sx:.1f}, {sy:.1f} on the sketch). If every square is off by the same amount, press Done, "
                             f"jog onto a1's centre and press 'a1 is here'.")
        self.move_to(*self.map.place_mm("a1"), self.settings["travel_speed"], "back to a1")
        self.say("info", "Board check done: " + ", ".join(places))

    def a1_here(self, square: str = "a1") -> list[str]:
        """The magnet sits on `square`'s real centre (a1 unless named): move the whole board so that square is here
        (magnet_home_mm changes). Home stays the corner; this says where the board really sits from it."""
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        square = (square or "a1").strip().lower()
        if square not in self.map_squares():
            raise gd.GantryError(f"{square!r} is not a square (a1..h8)")
        st = self.require(homed=True)
        new, report = bl.shift_to(self.config, self.map.square_mm(square), (st["x"], st["y"]))
        report[0] = f"{square} is here: " + report[0]
        new["calibrated_on_pi"] = True                     # deploy_pi.sh keeps this config instead of the Mac's
        self.save_config(new, report[0])
        for line in report[1:]:
            self.say("warn" if "out of reach" in line or "left of" in line or "past" in line else "info", line)
        return report

    # ------------------------------------------------------------------ tuning: nudge squares onto their real centres

    def tune_goto(self, square: str) -> None:
        """Send the empty magnet to where the board now thinks `square` is, and remember it as the square being tuned."""
        square = square.strip().lower()
        if square not in self.map_squares():
            raise gd.GantryError(f"{square!r} is not a square (a1..h8)")
        x, y = self.map.square_mm(square)
        self.start(f"Go to {square} (tune)", self._goto, x, y, square)   # raises Busy first if something runs
        self.tuning = {"square": square, "target": [round(x, 2), round(y, 2)]}

    @staticmethod
    def map_squares() -> list[str]:
        return [f + str(r) for f in gd.FILES for r in range(1, 9)]

    def tune_mark(self, square: str | None = None) -> dict:
        """The magnet sits on `square`'s real centre (nudged there, or it was right already): keep that answer and
        refit. Saved in gantry_config.json at once."""
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        square = (square or (self.tuning or {}).get("square") or "").strip().lower()
        if square not in self.map_squares():
            raise gd.GantryError("Pick a square first: click it in Tune mode so the magnet goes there.")
        st = self.require(homed=True)
        here = st["x"], st["y"]
        base =self.map.layout_mm(square)
        before = self.map.square_mm(square)
        nudge = [round(here[0] - base[0], 2), round(here[1] - base[1], 2)]
        far = math.dist(here, before)
        if far > 40:
            raise gd.GantryError(f"The magnet is {far:.0f} mm from where {square} is now: that is more than a square away. "
                                 f"Click {square} again, nudge from there, then press Right place.")
        answers = {**self._answers(), square: nudge}
        moved = f"moved {here[0] - before[0]:+.1f}, {here[1] - before[1]:+.1f} mm" if far > 0.05 else "confirmed where it was"
        self._save_tuning(answers, f"{square} is at {here[0]:.1f}, {here[1]:.1f} ({moved}); "
                                   f"{len(answers)} square(s) tuned")
        self.tuning = {"square": square, "target": [round(here[0], 2), round(here[1], 2)]}
        return self.map.nudge.summary()

    def tune_forget(self, square: str) -> dict:
        square = square.strip().lower()
        kept = self._answers()
        if kept.pop(square, None) is None:
            raise gd.GantryError(f"{square} has no tuning answer")
        self._save_tuning(kept, f"forgot {square}'s tuning")
        return self.map.nudge.summary()

    def tune_clear(self) -> None:
        if not self._answers():
            return
        self._save_tuning({}, "cleared every tuning answer: the squares are back on the sketch")
        self.tuning = None

    def _answers(self) -> dict:
        """The Tune answers in use: the pretend board's own copy while it is connected, else the config's."""
        return dict(self.sim_nudges if self.sim_nudges is not None else self.config.get("square_nudges_mm") or {})

    def _save_tuning(self, answers: dict, why: str) -> None:
        if self.sim_nudges is not None:     # the pretend board: a copy only, gone when it disconnects
            with self.lock:
                self.sim_nudges = answers
                self._rebuild_map()
            self.say("info", f"Pretend board, not saved to the file: {why}")
            return
        new = dict(self.config)
        if answers:
            new["square_nudges_mm"] = answers
        else:
            new.pop("square_nudges_mm", None)
        self.save_config(new, why)

    def tune_view(self) -> dict:
        nudge = self.map.nudge
        squares = {}
        for name in self.map_squares():
            bx, by = self.map.layout_mm(name)
            x, y = self.map.square_mm(name)
            squares[name] = {"at": [round(x, 2), round(y, 2)], "moved": [round(x - bx, 2), round(y - by, 2)]}
        slots = {}
        for side in bl.usable_lanes(self.config):
            for i in range(len(self.map.park_rows)):
                try:
                    x, y = self.map.square_mm(f"{side}:{i}")
                except gd.GantryError:
                    continue
                slots[f"{side}:{i}"] = [round(x, 2), round(y, 2)]
        return {"square": (self.tuning or {}).get("square"), "target": (self.tuning or {}).get("target"), "slots": slots,
                "answers": {k: [round(float(a), 2), round(float(b), 2)] for k, (a, b) in self._answers().items()},
                "fit": nudge.summary(), "squares": squares}

    # ------------------------------------------------------------------ camera check
    #
    # The camera only checks: it never moves the gantry to correct anything and never changes the config or the
    # Tune answers. The person clears the board (the camera photographs it empty), then puts one chess piece anywhere
    # on it (camera_cal_target "piece"; with "token" the printed ArUco token is dropped on the empty board instead).
    # The camera finds it and the magnet picks it up (going under where the camera saw it: that is a pick-up, not a
    # correction), then carries it to where the gantry believes each listed square's centre is. The camera measures
    # how far the piece landed from the real centre (the drag lag cancelled by arriving from both sides) and the
    # page shows it. Fixing what it finds is the person's job: Tune or "this square's centre is here".

    def start_mm(self) -> list | None:
        """Where the gantry rests after homing (set by hand with Set start here), or None."""
        start = self.sim_start if self.sim_nudges is not None else self.config.get("start_mm")
        return [float(start[0]), float(start[1])] if start else None

    def go_start(self) -> None:
        if not self.start_mm():
            raise gd.GantryError("No starting position yet: jog the magnet onto a8's centre and press Set start here.")
        self.start("Go to start", self._go_start)

    def _go_start(self) -> None:
        """The empty magnet (arm down, magnet off) to the starting position, at the travel speed."""
        x, y = self.start_mm()
        st = self.require(homed=True)
        if st.get("magnet"):
            self.cmd("M5")
        self.arm_down()
        self.move_to(x, y, self.settings["travel_speed"], "going to the starting position")
        self.say("info", f"At the starting position ({self.config.get('start_square', 'a8')}'s centre, "
                         f"{x:.1f}, {y:.1f}).")

    def set_start_here(self, square: str = "a8") -> list[float]:
        """Where the magnet is now becomes the starting position (the person has jogged it onto a8's centre)."""
        if self.task:
            raise Busy(f"busy: {self.task['name']}")
        self.require(homed=True)
        x, y = self.here()
        start = [round(x, 2), round(y, 2)]
        if self.sim_nudges is not None:          # the pretend board: a copy only, like its Tune answers
            self.sim_start = start
            self.say("info", f"Pretend board, not saved to the file: the starting position is {square}'s centre, "
                             f"{start[0]:.1f}, {start[1]:.1f}")
        else:
            self.save_config(dict(self.config, start_square=square, start_mm=start),
                             f"set by hand: the starting position is {square}'s centre, {start[0]:.1f}, {start[1]:.1f}")
        return start

    def camera_photo(self) -> bytes | None:
        path = getattr(self.locator, "last_photo", None)
        try:
            return Path(path).read_bytes() if path else None
        except OSError:
            return None

    def camera_cal_view(self) -> dict:
        with self.lock:
            view = json.loads(json.dumps(self.camera_cal))
        path = getattr(self.locator, "last_photo", None)
        view["photo"] = bool(path) and Path(path).is_file()
        view["start_mm"] = self.start_mm()
        return view

    def _cal_update(self, **values) -> None:
        with self.lock:
            self.camera_cal.update(values)

    def _cal_result(self, row: dict) -> None:
        with self.lock:
            self.camera_cal["results"].append(row)

    def _cal_target(self) -> str:
        """What the magnet drags during the camera check: "piece" (a chess piece) or "token" (printed)."""
        return "token" if str(self.config.get("camera_cal_target", "piece")).strip().lower() == "token" else "piece"

    def _cal_thing(self) -> str:
        return "token" if self._cal_target() == "token" else "piece"

    def _new_locator(self):
        if self.make_locator is not None:
            return self.make_locator()
        import inspect
        import camera_locate     # here, not at the top: OpenCV is only needed for this
        wanted = {"target": self._cal_target(),
                  "piece_height_mm": float(self.config.get("camera_cal_piece_height_mm", 45.0))}
        if self.config.get("camera_hfov_deg") is not None:
            wanted["hfov_deg"] = float(self.config["camera_hfov_deg"])
        try:
            params = inspect.signature(camera_locate.CameraLocator).parameters
        except (TypeError, ValueError):
            params = None
        if params is not None and not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            dropped = [k for k in wanted if k not in params]
            wanted = {k: v for k, v in wanted.items() if k in params}
            if "target" in dropped and self._cal_target() == "piece":
                self.say("warn", "This camera_locate.py only knows the printed token, not a chess piece: update it.")
        try:
            return camera_locate.CameraLocator(square_mm=(self.map.square, self.map.square_y), **wanted)
        except TypeError:        # an older camera_locate without these keywords
            return camera_locate.CameraLocator(square_mm=(self.map.square, self.map.square_y))

    def camera_check(self, squares: list[str] | None = None) -> None:
        names = [str(s).strip().lower() for s in (squares or CAL_SQUARES)]
        bad = [s for s in names if s not in self.map_squares()]
        if bad:
            raise gd.GantryError(f"not a square (a1..h8): {', '.join(bad)}")
        if len(set(names)) != len(names):
            raise gd.GantryError("a square is listed twice")
        with self.lock:
            if self.task:
                raise Busy(f"busy: {self.task['name']}")
            before = self.camera_cal
            self.camera_cal = {"state": "running", "round": 0, "squares": names, "results": [],
                               "message": "Starting: clear the board."}
            try:
                self.start("Camera check", self._camera_check, names)
            except Busy:
                self.camera_cal = before
                raise

    camera_calibrate = camera_check          # the old name, kept for callers that still use it

    def _locate(self) -> tuple[float, float]:
        """The piece's (or token's) centre on the board (mm from a1's centre, +x toward h, +y toward rank 8)."""
        self.check_stop()
        settle = self.camera_settle
        self.sleep(max(self.settings["grab_settle"], 0.5) if settle is None else settle)
        try:
            found = self.locator.locate()
        except (Stopped, gd.GantryError):
            raise
        except Exception as exc:     # camera_locate.LocateError says what is wrong in plain words
            words = str(exc) if type(exc).__name__ == "LocateError" else f"the camera failed: {exc!r}"
            raise CameraFailed(words or f"the camera did not find the {self._cal_thing()}") from None
        self.check_stop()
        x, y = (float(v) for v in found["board_mm"])
        return x, y

    def _ideal(self, square: str) -> tuple[float, float]:
        """Where a square's centre is on the board, in the camera's millimetres (a1's centre is 0, 0)."""
        return gd.FILES.index(square[0]) * self.map.square, (int(square[1]) - 1) * self.map.square_y

    def _inside(self, x: float, y: float) -> tuple[float, float]:
        return min(max(x, 0.0), self.map.travel[0]), min(max(y, 0.0), self.map.travel[1])

    def _arrive(self, x: float, y: float, side: int = 1, what: str = "camera check") -> tuple[float, float]:
        """Drag the piece to (x, y), the last leg CAL_APPROACH_MM long from one fixed side (+1) or the opposite
        one (-1), so the piece trails the magnet the same way every time. Returns where the magnet is."""
        ax, ay = CAL_APPROACH_DIR
        x, y = self._inside(x, y)
        before = self._inside(x + side * CAL_APPROACH_MM * ax, y + side * CAL_APPROACH_MM * ay)
        speed = self.carry_speed_real()
        self.move_to(*before, speed, what, carrying=True)
        self.move_to(x, y, speed, what, carrying=True)
        return self.here()

    def _both_sides(self, x: float, y: float, square: str) -> tuple[float, float]:
        """The piece's board position with the magnet at (x, y), the drag lag taken out: the average of arriving
        from one side and from the other."""
        self._arrive(x, y, 1, f"checking {square}")
        first = self._locate()
        self._arrive(x, y, -1, f"checking {square}")
        second = self._locate()
        return (first[0] + second[0]) / 2, (first[1] + second[1]) / 2

    def _pick_up(self) -> None:
        """The piece is somewhere on the board: put the magnet under it and check it follows a small move."""
        thing = self._cal_thing()
        tol = CAL_CAPTURE_MM if thing == "token" else CAL_CAPTURE_PIECE_MM
        for attempt in (1, 2):
            px, py = self._locate()
            gx, gy = self._inside(*self.map.point_mm(px / self.map.square, py / self.map.square_y))
            self.progress(f"The camera sees the {thing} at {px:.1f}, {py:.1f} mm from a1's centre: "
                          f"picking it up at {gx:.1f}, {gy:.1f}")
            self.arm_down()
            self.move_to(gx, gy, self.settings["travel_speed"], f"going under the {thing}")
            self.grab()
            first = self._locate()
            cx, cy = self.map.travel[0] / 2, self.map.travel[1] / 2      # the test move: toward the middle
            d = CAL_TEST_MOVE_MM * math.sqrt(0.5)
            move = (d if gx < cx else -d, d if gy < cy else -d)
            self.move_to(*self._inside(gx + move[0], gy + move[1]), self.carry_speed_real(), "testing the hold",
                         carrying=True)
            hx, hy = self.here()
            moved = (hx - gx, hy - gy)
            second = self._locate()
            off = math.dist((second[0] - first[0], second[1] - first[1]), moved)
            if off <= tol:
                self.say("info", f"The magnet holds the {thing} (it followed a {math.hypot(*moved):.0f} mm move "
                                 f"to {off:.1f} mm).")
                return
            self.say("warn", f"The {thing} did not follow the magnet ({off:.1f} mm off its move); "
                             + ("trying again." if attempt == 1 else "giving up."))
            self.release()
        raise gd.GantryError(f"The magnet could not pick up the {thing}. "
                             + ("Put it flat on a square" if thing == "token" else "Stand it upright in a square")
                             + ", away from the edge, and start the camera check again.")

    def _measure(self, square: str) -> float:
        """Carry the piece to where the gantry believes `square`'s centre is (no corrections) and record how far
        the camera sees it from the real centre. Returns that distance in mm."""
        thing = self._cal_thing()
        self.progress(f"Checking {square}: carrying the {thing} to its believed centre")
        x, y = self.map.square_mm(square)
        px, py = self._both_sides(x, y, square)
        ix, iy = self._ideal(square)
        dx, dy = ix - px, iy - py
        d = math.hypot(dx, dy)
        self._cal_result({"kind": "verify", "square": square, "error_mm": [round(dx, 2), round(dy, 2)],
                          "distance_mm": round(d, 2), "ok": d <= CAL_VERIFY_TOL_MM})
        self.say("info" if d <= CAL_VERIFY_TOL_MM else "warn",
                 f"{square}: the {thing} lands {d:.2f} mm off the centre (dx {dx:+.1f}, dy {dy:+.1f})")
        return d

    def _camera_check(self, squares: list[str]) -> None:
        try:
            st = self.require(homed=True)
            if st.get("magnet"):
                self.cmd("M5")
            self.arm_down()
            self.locator = self._new_locator()
            thing = self._cal_thing()
            if thing == "token":
                self._cal_update(message="Waiting for you: clear the board and drop the token on it.")
                self.wait_person("Clear the board and drop the calibration token anywhere on it. Press Done.")
            else:
                self._cal_update(message="Waiting for you: clear the board completely.")
                self.wait_person("Clear the board completely (nothing on it, hands out of view). Press Done.")
                capture = getattr(self.locator, "capture_reference", None)
                if callable(capture):
                    self._cal_update(message="Photographing the empty board.")
                    self.check_stop()
                    try:
                        capture()
                    except (Stopped, gd.GantryError):
                        raise
                    except Exception as exc:
                        words = str(exc) if type(exc).__name__ == "LocateError" else f"the camera failed: {exc!r}"
                        raise CameraFailed(words or "the camera could not photograph the empty board") from None
                self._cal_update(message="Waiting for you: put one chess piece on the board.")
                self.wait_person("Put one chess piece (a pawn or a rook) anywhere on the board. Press Done.")
            self._cal_update(message=f"Picking the {thing} up.")
            self._pick_up()
            worst = (None, -1.0)
            for square in squares:
                self._cal_update(message=f"Checking {square}.")
                d = self._measure(square)
                if d > worst[1]:
                    worst = (square, d)
            self.release()                   # the piece stays on the last square
            square, d = worst
            if d <= CAL_VERIFY_TOL_MM:
                advice = f"Within {CAL_VERIFY_TOL_MM:g} mm everywhere: the board is calibrated."
            else:
                advice = (f"Off by up to {d:.1f} mm: fix it with Tune (nudge by eye, Right place) or "
                          "'this square's centre is here', then check again.")
            words = (f"Check done: the worst square is {square}, {d:.1f} mm off. {advice} "
                     f"The {thing} is on {squares[-1]}.")
            self._cal_update(state="done", message=words)
            self.say("info" if d <= CAL_VERIFY_TOL_MM else "warn", words)
        except Stopped:
            self._cal_update(state="failed", message="Stopped.")
            raise
        except Exception as exc:
            self._cal_update(state="failed", message=str(exc) or repr(exc))
            raise
        finally:
            self._safe()                     # magnet off, arm down: never raises
            close = getattr(self.locator, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ the page's view

    def view(self, since: int) -> dict:
        with self.lock:
            st = self.status
            status = None
            if st:
                status = {k: st[k] for k in ("state", "reason", "magnet", "homed", "servo", "host_lost", "prog")}
                status.update(x=round(st["x"], 2), y=round(st["y"], 2), raw=st["raw"])
            legal: dict[str, list[str]] = {}
            for move in self.game.legal_moves:
                legal.setdefault(chess.square_name(move.from_square), []).append(chess.square_name(move.to_square))
            preview = None
            if self.preview:
                preview = {k: self.preview[k] for k in ("kind", "label", "san", "steps", "carries", "seconds", "rests")}
                preview["warning"] = self.preview.get("warning")
            pending = None
            if self.pending:
                p = self.pending
                pending = {"san": p["preview"]["san"], "next": p["next"], "total": p["total"]}
            outcome = self.game.outcome()
            return {
                "connected": self.link is not None, "port": self.port, "firmware": self.firmware, "too_old": self.too_old,
                "firmware_source": self.firmware_source_version(),
                "board_settings": self.board_settings, "status": status, "position_lost": self.position_lost,
                "position_memory": self.position_memory(),
                "restarts": self.restarts,
                "task": {"name": self.task["name"], "progress": self.task["progress"]} if self.task else None,
                "waiting": self.waiting["prompt"] if self.waiting else None,
                "settings": self.settings, "preview": preview, "pending": pending, "tests": self.tests,
                "game": {"fen": self.game.fen(), "turn": "white" if self.game.turn else "black",
                         "moves": [h["san"] for h in self.history], "legal": legal, "check": self.game.is_check(),
                         "over": outcome.result() if outcome else None, "valid": self.game.is_valid()},
                "layout": self.layout.pieces,
                "tune": self.tune_view(),
                "geometry": {"square": self.map.square, "square_y": self.map.square_y, "a1": self.map.a1, "travel": self.map.travel,
                             "home_beside": "a8" if gd.y_flip_mm(self.config) is not None else "a1",
                             "home_square": self.config.get("home_square"), "home_xy": self.home_xy(),
                             "swap_xy": gd.swap_xy_flag(self.config),
                             "manual_home_on_connect": bool(self.config.get("manual_home_on_connect", False)),
                             "columns": {str(k): v for k, v in self.map.columns.items() if k < 0 or k > 7},
                             "park_rows": [self.map.park_rows[i] for i in sorted(self.map.park_rows)],
                             "reach": self.geometry.reach_mm, "lanes": bl.usable_lanes(self.config),
                             "home": bl.magnet_home(self.config), "problems": bl.check(self.config),
                             "mm_per_turn": bl.mm_per_turn(self.config), "steps_per_mm": round(bl.steps_per_mm(self.config), 5),
                             "start_mm": self.start_mm()},
                "camera_cal": self.camera_cal_view(),
                "bot": self.bot.view() if self.bot is not None else None,
                "log": [e for e in self.log if e["id"] > since], "log_id": self.log_id,
            }


def serial_ports() -> list[str]:
    found = []
    for pattern in ("/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*", "/dev/cu.usbmodem*",
                    "/dev/ttyUSB*", "/dev/ttyACM*"):
        found += sorted(glob.glob(pattern))
    return found + ["sim"]


class Handler(BaseHTTPRequestHandler):
    console: Console = None   # set in main

    def log_message(self, fmt, *args):   # keep the terminal for the console's own log
        pass

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            since = int(parse_qs(url.query).get("since", ["0"])[0] or 0)
            self._json(200, self.console.view(since))
        elif url.path == "/api/ports":
            self._json(200, {"ports": serial_ports()})
        elif url.path == "/api/camera-photo":
            photo = self.console.camera_photo()
            if photo is None:
                self._json(404, {"error": "no camera photo yet"})
            else:
                self._send(200, photo, "image/jpeg")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
            self._json(403, {"error": "this console only answers its own page"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            self._json(400, {"error": "bad JSON"})
            return
        c = self.console
        routes = {
            "/api/bot-config": lambda b: c.bot.configure(b) if c.bot is not None else {"error": "Bot mode is not running"},
            "/api/bot-reply": lambda b: c.bot.reply() if c.bot is not None else None,
            "/api/connect": lambda b: c.connect(str(b.get("port") or "")),
            "/api/disconnect": lambda b: c.disconnect(),
            "/api/stop": lambda b: c.stop(),
            "/api/unlock": lambda b: c.unlock(),
            "/api/motors-off": lambda b: c.motors_off(),
            "/api/home": lambda b: c.home_bump() if b.get("mode") == "bump" else c.home_manual(),
            "/api/jog": lambda b: c.jog(float(b.get("dx", 0)), float(b.get("dy", 0))),
            "/api/goto": lambda b: c.goto(str(b.get("place", ""))),
            "/api/return-home": lambda b: c.return_home(),
            "/api/servo": lambda b: c.servo(bool(b.get("up"))),
            "/api/magnet": lambda b: c.magnet(bool(b.get("on"))),
            "/api/command": lambda b: {"lines": c.raw(str(b.get("line", "")))},
            "/api/settings": lambda b: c.save_settings(b),
            "/api/plan": lambda b: self._plan(b),
            "/api/run": lambda b: c.run_preview(bool(b.get("by_hand"))),
            "/api/cancel": lambda b: c.cancel_preview(),
            "/api/undo": lambda b: self._auto(c.plan_undo()),
            "/api/reset-board": lambda b: self._auto(c.plan_reset(), False),
            "/api/new-game": lambda b: c.new_game(),
            "/api/resolve": lambda b: c.resolve(str(b.get("action"))),
            "/api/done": lambda b: c.done(),
            "/api/test/distance": lambda b: c.distance_check(str(b.get("axis", "x")).lower(), float(b.get("mm", 20))),
            "/api/test/distance-result": lambda b: {"mm_per_turn": c.distance_result(float(b.get("measured", 0)))},
            "/api/test/turn": lambda b: c.one_turn(str(b.get("axis", "x")).lower(), int(b.get("direction", 1))),
            "/api/test/turn-result": lambda b: {"mm_per_turn": c.turn_result(float(b.get("turns") or 0) or None,
                                                                             float(b.get("measured") or 0) or None)},
            "/api/mm-per-turn": lambda b: c.use_mm_per_turn(str(b.get("axis", "")).lower(), float(b.get("mm", 0))),
            "/api/check-board": lambda b: c.check_board(),
            "/api/a1-here": lambda b: {"report": c.a1_here(b.get("square") or "a1")},
            "/api/tune/goto": lambda b: c.tune_goto(str(b.get("square", ""))),
            "/api/tune/mark": lambda b: {"fit": c.tune_mark(b.get("square"))},
            "/api/tune/forget": lambda b: {"fit": c.tune_forget(str(b.get("square", "")))},
            "/api/tune/clear": lambda b: c.tune_clear(),
            "/api/flash": lambda b: c.flash(str(b.get("port", ""))),
            "/api/test/ladder": lambda b: c.ladder(str(b.get("axis", "x")).lower(), float(b.get("distance", 100)),
                                                   b.get("speeds") or LADDER_SPEEDS),
            "/api/test/stalled": lambda b: c.stalled(),
            "/api/camera-check": lambda b: c.camera_check(b.get("squares") or None),
            "/api/camera-calibrate": lambda b: c.camera_check(b.get("squares") or None),   # the old name
            "/api/set-start-here": lambda b: {"start_mm": c.set_start_here()},
            "/api/go-start": lambda b: c.go_start(),
            "/api/forget-position": lambda b: c.forget_position(),
        }
        route = routes.get(url.path)
        if not route:
            self._json(404, {"error": "not found"})
            return
        try:
            result = route(body)
            self._json(200, result if isinstance(result, dict) else {"ok": True})
        except (Busy, gd.GantryError, ValueError, KeyError) as exc:
            message = str(exc).strip("'\"") or exc.__class__.__name__
            c.say("error", message)
            self._json(409, {"error": message})
        except Exception as exc:
            logging.error(traceback.format_exc())
            self._json(500, {"error": repr(exc)})

    def _plan(self, b: dict) -> dict:
        c = self.console
        preview = c.plan_move(str(b.get("from", "")).strip(), str(b.get("to", "")).strip(),
                              (str(b["promotion"]).lower() if b.get("promotion") else None), str(b.get("mode", "chess")))
        return self._auto(preview)

    def _auto(self, preview: dict, allowed: bool = True) -> dict:
        c = self.console
        ran = False
        if preview.get("manual_only", False):
            c.run_preview(by_hand=True)
            ran = True
            if c.bot is not None and preview["kind"] == "move":
                c.bot.reply()
        elif allowed and c.settings["auto_run"] and c.link is not None:
            c.run_preview()
            ran = True
        return {"ran": ran, "san": preview["san"], "carries": preview["carries"], "seconds": preview["seconds"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help="connect at start: a serial port such as /dev/cu.usbserial-0001, or sim")
    parser.add_argument("--http", type=int, default=8788, help="the page's port on this computer (default 8788)")
    parser.add_argument("--config", type=Path, default=gd.DEFAULT_CONFIG)
    parser.add_argument("--sim-speed", type=float, default=1.0, help="how much faster than real the pretend board runs")
    parser.add_argument("--bot-black", action="store_true", help="Human White by hand; local Stockfish and gantry play Black")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("link").setLevel(logging.WARNING)
    console = Console(gd.load_config(args.config), args.sim_speed, args.config)
    if args.bot_black:
        from bot_black import BlackBot
        console.bot = BlackBot(console)
    Handler.console = console
    server = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)
    server.daemon_threads = True
    print(f"DUEN gantry console: open http://127.0.0.1:{args.http}  (Ctrl+C to quit)", flush=True)
    if args.port:
        console.connect(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if console.bot is not None:
            console.bot.close()
        console.stop() if console.task else None
        if console.link is not None:
            try:
                console.exchange("M5", 1.0)
                console.exchange("M8", 1.0)
            except Exception:
                pass
            link = console.link          # the poller may have dropped a lost link meanwhile
            if link is not None:
                link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
