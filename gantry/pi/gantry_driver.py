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

    {"id": 7, "label": "e4d5", "before": [...], "pieces": {...}, "expect": [...], "steps": [
        {"op": "remove", "piece": "p", "square": "d5"},
        {"op": "carry", "piece": "P", "from": "e4", "to": "d5", "path": [[4, 3], [3, 4]]}]}

A "remove" step is a captured piece that a person takes off the board; the camera checks the
square is empty before the job is run. The driver sends nothing for it (it is the caller's job to
have waited) and plans the carries that follow as if the square were already empty. A "note"
step is a message for the person. A "carry" is one piece carried.

Path points are in squares: a1 is (0, 0), h8 is (7, 7), x.5 are the lanes between squares. The
docking lanes beside the board at x = -1 (White's, the left) and 8 (Black's, the right), with the
row an index down the lane and slots named "w:0", "w:1", ... and "b:0", ..., are understood here
and in the firmware; the bench console docks captured pieces there (dock mode), the touch screen
and the website have a person take them off.

Millimetres are the magnet's own (board_layout.py): 0,0 is the magnet's centre at home, and the
board, the lanes and the travel in gantry_config.json are what the console's board calibration
measured by steering the magnet there. Before each job the driver tells the board its steps/mm
($100, from mm_per_turn: 41 mm per turn on the bench) and its travel ($130, $131).

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

Home once: every spot the board reports Idle and homed is remembered in ~/.duen-gantry/position.json
(PositionMemory; GANTRY_STATE_DIR moves it), with "moving" set before every move. A restarted ESP32 (the USB port
opening, a supply dip, the Pi rebooting) is put back there with $HM and G92 instead of being homed by hand
(restore_position), unless the power went off mid-move or something may have moved the carriage behind the count's
back (motors off, an alarm, a stall, a stop mid-move, a new steps/mm). With "keep_esp32_running" the port opens
with DTR/RTS low so the ESP32 does not restart at all. "park_at_start_after_job" ends each job at start_mm.
"""
from __future__ import annotations

import argparse
import contextlib
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
import board_layout  # noqa: E402
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
        config = board_layout.machine(config)    # the sketch's millimetres less the magnet's home: what the board counts
        self.square, self.square_y = board_layout.square_xy(config)   # along X (files) and Y (ranks)
        self.a1 = tuple(float(v) for v in config["a1_center_mm"])
        park = config.get("park_columns_mm") or {}
        self.columns = {i: self.a1[0] + i * self.square for i in range(8)}
        for i, x in enumerate(park.get("w", [])):
            self.columns[-1 - i] = float(x)
        for i, x in enumerate(park.get("b", [])):
            self.columns[8 + i] = float(x)
        rows = config.get("park_rows_mm")
        self.rows = {i: self.a1[1] + i * self.square_y for i in range(8)}
        self.park_rows = {i: float(y) for i, y in enumerate(rows)} if rows else self.rows
        self.travel = tuple(float(v) for v in config["travel_mm"])
        # The Tune panel's answers (board_layout.NudgeField): every point moves by the fitted correction there.
        self.nudge = board_layout.NudgeField({
            name: (*self.layout_mm(name), dx, dy) for name, (dx, dy) in board_layout.nudges(config).items()
            if len(name) == 2 and name[0] in FILES and name[1] in "12345678"})

    def layout_mm(self, name: str) -> tuple[float, float]:
        """A square's centre as the layout draws it, before any tuning."""
        return self.columns[FILES.index(name[0])], self.rows[int(name[1]) - 1]

    def _nudged(self, x: float, y: float) -> tuple[float, float]:
        if not self.nudge:
            return x, y
        dx, dy = self.nudge.at(x, y)
        # never past the travel: the docking columns sit on its very edges (x 0 and x 470 on Sketch 5)
        return min(max(x + dx, 0.0), self.travel[0]), min(max(y + dy, 0.0), self.travel[1])

    def square_mm(self, name: str) -> tuple[float, float]:
        """A square like "e4" or a slot like "w:3"."""
        if ":" in name:
            # Slots fill one lane at the configured row pitch (12 on the real board), then the next lane out.
            color, index = name[0], int(name[2:])
            per_lane = len(self.park_rows)
            column = -1 - index // per_lane if color == "w" else 8 + index // per_lane
            if column not in self.columns:
                raise GantryError(f"no parking column configured for {name}")
            return self._nudged(self.columns[column], self.park_rows[index % per_lane])
        return self.point_mm(FILES.index(name[0]), int(name[1]) - 1)

    def point_mm(self, px: float, py: float) -> tuple[float, float]:
        """A path point in square units, lanes (x.5) included. In a parking column, py is a slot index down the lane."""
        x = self._interp(self.columns, px, "column", self.square)
        if px <= -1 or px >= 8:
            row = int(py)
            if row != py or row < 0 or row >= len(self.park_rows):
                raise GantryError(f"parking row {py:g} is not configured")
            return self._nudged(x, self.park_rows[row])
        return self._nudged(x, self._interp(self.rows, py, "row", self.square_y))

    def units(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """A point in mm as square units (the inverse of point_mm on the board and the lanes beside it)."""
        if self.nudge:     # undo the tuning first (a few rounds: the field is nearly flat, so this settles at once)
            lx, ly = x_mm, y_mm
            for _ in range(4):
                dx, dy = self.nudge.smooth(lx, ly)
                lx, ly = x_mm - dx, y_mm - dy
            x_mm, y_mm = lx, ly
        a1x, h = self.columns[0], self.columns[7]
        if x_mm < a1x and -1 in self.columns:
            x = -1 + (x_mm - self.columns[-1]) / (a1x - self.columns[-1])
        elif x_mm > h and 8 in self.columns:
            x = 7 + (x_mm - h) / (self.columns[8] - h)
        else:
            x = (x_mm - a1x) / self.square
        return x, (y_mm - self.rows[0]) / self.square_y

    def place_mm(self, place: str) -> tuple[float, float]:
        """A firmware place: a square, a slot, or "x,y" in squares."""
        if "," in place:
            x, y = (float(v) for v in place.split(","))
            return self.point_mm(x, y)
        return self.square_mm(place)

    def geometry(self, config: dict):
        """The same numbers for board_paths (machine millimetres, like the rest of this map)."""
        sizes = {str(k).lower(): float(v) for k, v in (config.get("piece_sizes_mm") or board_paths.DEFAULT_PIECE_SIZES_MM).items()}
        lanes = board_layout.usable_lanes(config)  # asked for in docking_lanes, configured, and within reach
        slot_columns = tuple(c for c in self.columns if (c < 0 and "w" in lanes) or (c > 7 and "b" in lanes))
        reach = board_layout.reach(config)         # [x_min, y_min, x_max, y_max] of the magnet: the travel by default
        return board_paths.Geometry(
            self.square, self.a1, {c: x for c, x in self.columns.items() if c < 0 or c > 7},
            [self.park_rows[i] for i in sorted(self.park_rows)],
            float(config.get("piece_mm", 27.0)), float(config.get("clearance_mm", 3.0)),
            float(config.get("lane_clearance_mm", 1.0)), sizes,
            slot_columns=slot_columns, reach_mm=reach,
            smooth=bool(config.get("path_smoothing", False)), corner_lag_mm=float(config.get("corner_lag_mm", 0.0)),
            turn_penalty_mm=float(config.get("turn_penalty_mm", 2.0)), square_y_mm=self.square_y,
            edge_routes=bool(config.get("edge_routes", False)))

    def setup_line(self) -> str:
        """The $B= line that gives the firmware this board: square, a1, the docking lanes when configured, and the
        square size along Y when it was measured apart from X (firmware 0.7+ reads that eighth number)."""
        parts = [self.square, self.a1[0], self.a1[1]]
        two_sizes = abs(self.square_y - self.square) > 1e-3
        if (-1 in self.columns and 8 in self.columns) or two_sizes:
            rows = [self.park_rows[i] for i in sorted(self.park_rows)]
            pitch = rows[1] - rows[0] if len(rows) > 1 else self.square_y
            parts += [self.columns.get(-1, self.a1[0] - self.square), self.columns.get(8, self.columns[7] + self.square),
                      rows[0], pitch]
        if two_sizes:
            parts.append(self.square_y)
        return "$B=" + ",".join(f"{round(v, 3):g}" for v in parts)

    def _interp(self, table: dict, value: float, what: str, step: float, outer: dict | None = None) -> float:
        """Whole numbers are configured centres; between two of them the place is in proportion, and past the last one
        it goes on at the square pitch (x.5 is half a square out), the same as the firmware's place_mm."""
        lo, hi = math.floor(value), math.ceil(value)
        known = {**(outer or {}), **table}
        if lo == hi:
            if lo not in known:
                raise GantryError(f"{what} {value:g} is not configured")
            return known[lo]
        if lo in known and hi in known:
            return known[lo] + (known[hi] - known[lo]) * (value - lo)
        if hi in known:
            return known[hi] - step * (hi - value)
        if lo in known:
            return known[lo] + step * (value - lo)
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
    return f"{_num(x)},{_num(y)}"


def _num(v: float) -> str:
    """One decimal when that is exact (lanes are x.5), else three: the firmware reads any decimal."""
    return f"{v:.1f}" if abs(v * 10 - round(v * 10)) < 1e-6 else f"{v:.3f}"


def firmware_keeps_line(path) -> bool:
    """Does the firmware carry a two-point path along that straight line by itself? Only between two squares on one
    file, rank or diagonal (protocol.h route()); anything else it takes round its own corner. A move along the lane
    between two slots keeps the firmware's own way, as before."""
    (x0, y0), (x1, y1) = (float(v) for v in path[0]), (float(v) for v in path[-1])
    slot0, slot1 = x0 <= -1 or x0 >= 8, x1 <= -1 or x1 >= 8
    if slot0 and slot1:
        return True
    if slot0 or slot1:
        return False
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    return dx < 1e-9 or dy < 1e-9 or abs(dx - dy) < 1e-9


def piece_words(symbol: str | None) -> str:
    if not symbol or symbol == "?":
        return "the piece"
    return f"the {'white' if symbol.isupper() else 'black'} {PIECE_NAMES.get(symbol.lower(), 'piece')}"


def describe_step(step: dict) -> str:
    """One carry (or a removal by the person) in plain words, for the log and the screen."""
    who = piece_words(step.get("piece"))
    if step.get("op") == "remove":
        return f"The person takes {who} off {step['square']} (the caller waits for the camera before this job runs)"
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
    the paths in the job are used as they are. A "remove" step is passed through unchanged and
    empties its square for the carries after it: the person has taken that piece off (the caller
    checked with the camera before running the job), so nothing has to be carried round it.
    A job with "any_order" (a reset) may carry a blocked piece later instead of failing: every
    carry is tried in order, the blocked ones again after the others, until a pass moves nothing;
    its notes ("Put a black pawn on e7.") come after all the carries.
    """
    if "before" not in job:
        return list(job.get("steps", []))
    world = board_paths.Board(board.geometry(config), job["before"], pieces=job.get("pieces"))
    any_order = bool(job.get("any_order"))
    steps: list[dict] = []
    notes: list[dict] = []
    pending = list(job.get("steps", []))
    while pending:
        blocked: list[tuple[dict, str]] = []
        moved = False
        for step in pending:
            if step.get("op") == "remove":
                world.occupied.discard(step["square"])
                world.pieces.pop(step["square"], None)
            if step.get("op") != "carry":
                (notes if any_order and step.get("op") == "note" else steps).append(step)
                continue
            if step.get("piece") and step["from"] in world.occupied:
                world.pieces.setdefault(step["from"], step["piece"])
            try:
                carries = world.plan(step["from"], step["to"])
            except board_paths.NoPath as exc:
                if not any_order:
                    raise GantryError(str(exc)) from None
                # A reset: each piece only has to get home, so a blocked one waits for the others and is tried again.
                blocked.append((step, str(exc)))
                continue
            moved = True
            for carry in carries:
                piece = step.get("piece", "?") if carry.reason == "move" else world.pieces.get(carry.piece_from, "?")
                steps.append({"op": "carry", "piece": piece, "from": carry.piece_from, "to": carry.piece_to, "path": carry.path, "reason": carry.reason})
                world.apply(carry)
        if blocked and not moved:
            raise GantryError(blocked[0][1])
        pending = [step for step, _ in blocked]
    return steps + notes


def carry_limit(config: dict) -> int:
    return int(config.get("max_carries_per_job", MAX_CARRIES_PER_JOB))


def check_carry_count(count: int, config: dict, label: str) -> None:
    if count > carry_limit(config):
        raise GantryError(f"job {label} would carry {count} pieces; more than {carry_limit(config)} in one job is a planning error, not a chess move")


def removal_line(step: dict) -> tuple[str, str]:
    """A "remove" step as a comment line: printed by --dry-run and logged, never sent to the board.

    No motion and no command: the person has already taken the piece off, and whoever runs the
    job (the touch-screen app, the website relay) waited for the camera to confirm it first.
    """
    return (f"# person removes {step.get('piece', '?')} from {step['square']}", describe_step(step))


def checked_points(step: dict, board: BoardMap) -> list[tuple[float, float]]:
    """The carry's path in mm, every point inside the travel."""
    path = step.get("path") or []
    if len(path) < 2:
        raise GantryError(f"carry {step.get('from')}->{step.get('to')} has no path")
    points = [board.point_mm(float(px), float(py)) for px, py in path]
    for (x, y), p in zip(points, path):
        board.check(x, y, f"point {p} of {step['from']}->{step['to']}")
    return points


def lag_compensated(points, lag_mm: float, travel=None) -> list[tuple[float, float]]:
    """Where the MAGNET must go so a dragged piece lands on `points` (the path the piece should follow, the first one
    being where it is picked up). Returns the magnet's waypoints after the first point.

    The math: dragged along a direction u (unit vector), the piece settles lag_mm BEHIND the magnet, at
    magnet - lag*u, because friction holds it back until the magnet's pull balances it. So:
      * along a straight leg the piece trails on the same line: nothing to correct there;
      * at a path point P reached along u_in, the magnet goes to P + lag*u_in: the piece then sits exactly on P
        (without this the piece stops lag_mm short of every corner and cuts it, and stops short of the target);
      * at a corner (the next leg goes along u_out), the magnet then swings to P + lag*u_out: it stays lag_mm from
        the piece the whole time, so the piece stays on the corner while the lead turns onto the new line, and the
        next leg drags it along that line;
      * at the last point the magnet stops at P + lag*u_in, so the piece stops on the square's centre.
    Points are clamped inside the travel (0..travel): next to the frame (rank 8's centres sit on the Y limit here)
    the lead along that axis is lost, and the piece lands up to lag_mm short there."""
    import math as _math
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        return []
    if lag_mm <= 0:
        return pts[1:]

    def unit(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = _math.hypot(dx, dy)
        return (dx / d, dy / d) if d > 1e-9 else None

    def clamp(x, y):
        if travel is not None:
            x = min(max(x, 0.0), float(travel[0]))
            y = min(max(y, 0.0), float(travel[1]))
        return round(x, 3), round(y, 3)

    out: list[tuple[float, float]] = []

    def add(point):
        if not out or _math.hypot(point[0] - out[-1][0], point[1] - out[-1][1]) > 1e-3:
            out.append(point)

    u_in = None
    for i in range(1, len(pts)):
        u = unit(pts[i - 1], pts[i])
        u_in = u or u_in
        p = pts[i]
        if u_in is None:
            continue
        add(clamp(p[0] + lag_mm * u_in[0], p[1] + lag_mm * u_in[1]))
        if i < len(pts) - 1:
            u_out = unit(p, pts[i + 1])
            if u_out is not None and abs(u_out[0] - u_in[0]) + abs(u_out[1] - u_in[1]) > 1e-6:
                add(clamp(p[0] + lag_mm * u_out[0], p[1] + lag_mm * u_out[1]))
    return out


def plan(job: dict, board: BoardMap, config: dict) -> list[tuple[str, str]]:
    """G-code lines for a job (GRBL boards, firmware 0.1), each with a note for the log. Fails before anything moves if a point is out of reach.

    A "remove" step gives no G-code, only a comment line: the caller has waited for the person to
    take the piece off (and for the camera to see the square empty) before running the job.
    """
    travel_feed = float(config["travel_speed_mm_s"]) * 60
    carry_feed = float(config["carry_speed_mm_s"]) * 60
    servo = float(config.get("servo_settle_s", 3.0)) if config.get("servo", True) else 0.0
    lines: list[tuple[str, str]] = []
    carries = 0
    for step in route(job, board, config):
        if step.get("op") == "note":
            lines.append(("# " + step.get("message", ""), "note"))
            continue
        if step.get("op") == "remove":
            lines.append(removal_line(step))
            continue
        if step.get("op") != "carry":
            raise GantryError(f"unknown step {step.get('op')!r}")
        points = checked_points(step, board)
        label = f"{step.get('piece', '?')} {step['from']}->{step['to']}"
        if step.get("reason") in ("make room", "put back"):
            label += f" ({step['reason']})"
        x0, y0 = points[0]
        lines.append((f"G1 X{x0:.3f} Y{y0:.3f} F{travel_feed:.0f}", f"go to {step['from']} for {label}"))
        if servo:   # firmware 0.8 refuses M3 with the arm down
            lines.append(("M7", f"arm up for {label}"))
            lines.append((f"G4 P{servo:.3f}", "let the arm settle"))
        lines.append(("M3", describe_step(step)))   # perform() tells progress() this note: the touch screen shows it
        lines.append((f"G4 P{float(config['grab_settle_s']):.3f}", "let the magnet take hold"))
        # drag_lag_mm: the carried piece trails the magnet; lead it so the PIECE follows the path (lag_compensated).
        for (x, y) in lag_compensated(points, float(config.get("drag_lag_mm", 0.0)), board.travel):
            lines.append((f"G1 X{x:.3f} Y{y:.3f} F{carry_feed:.0f}", f"carry {label}"))
        lines.append(("M5", f"release {label}"))
        lines.append((f"G4 P{float(config['release_settle_s']):.3f}", "let go cleanly"))
        if servo:
            lines.append(("M8", f"arm down after {label}"))
            lines.append((f"G4 P{servo:.3f}", "let the arm settle"))
        carries += 1
    check_carry_count(carries, config, job.get("label", "?"))
    return lines


def plan_carry(job: dict, board: BoardMap, config: dict) -> list[tuple[str, str]]:
    """CARRY lines for firmware 0.2, one per piece carried, each with a note in plain words.

    The path comes from route() (board_paths, real piece widths); its inner points become the
    via-points, written as the firmware reads places: slot names, or x,y in squares. Every point
    is checked against the travel first, so nothing is sent that the board would refuse half-way.
    A "remove" step (a captured piece the person takes off) gives no CARRY, only a comment line;
    the caller must have waited for the removal, camera-checked, before running the job.
    """
    lines: list[tuple[str, str]] = []
    carries = 0
    for step in route(job, board, config):
        if step.get("op") == "note":
            lines.append(("# " + step.get("message", ""), "note"))
            continue
        if step.get("op") == "remove":
            lines.append(removal_line(step))
            continue
        if step.get("op") != "carry":
            raise GantryError(f"unknown step {step.get('op')!r}")
        points = checked_points(step, board)
        vias = [place_name(p) for p in step["path"][1:-1]]
        if not vias and not firmware_keeps_line(step["path"]):
            # A shortcut straight from one place to the other: the firmware would go round its own corner
            # instead, so the middle of the line goes along as a via-point and it stays on the planned line.
            (ax, ay), (bx, by) = points[0], points[-1]
            vias = [place_name(board.units((ax + bx) / 2, (ay + by) / 2))]
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
    servo = float(config.get("servo_settle_s", 3.0)) if config.get("servo", True) else 0.0
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


def predict(lines: list[tuple[str, str]], config: dict, steps_per_mm: float | None = None, accel: float | None = None, start=(0.0, 0.0), board: BoardMap | None = None) -> dict:
    """Steps and seconds the board will need, from the same trapezoid the firmware uses. Reads G-code and CARRY lines."""
    if steps_per_mm is None:
        steps_per_mm = board_layout.steps_per_mm(config)
    if accel is None:
        accel = float(config.get("accel_mm_s2", 800.0))   # the firmware's $120: 400 from 0.4 on, 800 before
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
    "expect" worked out from where the pieces will be after it. A "remove" step counts as no
    carry and empties its square for the parts after it.
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
            if step.get("op") == "remove":
                occupied.discard(step["square"])
                pieces.pop(step["square"], None)
            elif step.get("op") == "carry":
                occupied.discard(step["from"])
                occupied.add(step["to"])
                pieces[step["to"]] = pieces.pop(step["from"], step.get("piece", "?"))
        jobs.append({"id": job.get("id", 0), "label": f"{job.get('label', '?')} ({i + 1}/{len(chunks)})", "steps": chunk,
                     "expect": sorted(n for n in occupied if ":" not in n)})
    return jobs


# ----------------------------------------------------------------------------- talking to the board

PORT_LOCK_DIR = Path(os.environ.get("GANTRY_LOCK_DIR", "/tmp"))
SETTLE_S = 3.0               # keep_esp32_running: how long the link waits for a status line or the banner on opening


def _config_flag(key: str, default: bool = False) -> bool:
    """A true/false setting from DEFAULT_CONFIG, for callers that open a Link without passing it (the touch app)."""
    try:
        return bool(json.loads(Path(DEFAULT_CONFIG).read_text()).get(key, default))
    except (OSError, ValueError, AttributeError):
        return default


def open_serial(serial_module, port: str, baud: int, keep_running: bool):
    """The serial port, opened. keep_running: DTR and RTS are set low (deasserted) BEFORE opening, with no
    hardware flow control, so the ESP32 dev board's auto-reset transistors (DTR/RTS -> EN/IO0) see no edge and the
    firmware keeps running with its count. HUPCL is cleared too (Linux, macOS), so closing the port does not drop
    the lines either. Without keep_running: pyserial's defaults (the board restarts when the port opens)."""
    if not keep_running:
        return serial_module.Serial(port, baud, timeout=0.2)
    handle = serial_module.Serial()
    handle.port, handle.baudrate, handle.timeout = port, baud, 0.2
    handle.dsrdtr, handle.rtscts = False, False
    handle.dtr, handle.rts = False, False        # applied by pyserial as the port opens
    handle.open()
    try:
        import termios
        fd = handle.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass
    return handle


def lock_port(port: str):
    """One program at a time on the gantry: the touch app and the gantry terminal both come through Link.
    The lock is taken BEFORE the port opens, because opening it can restart the ESP32 (even mid-move; unless
    keep_esp32_running holds DTR/RTS low, see open_serial).
    Returns the open lock file (keep it until close); GantryError when another program has the port."""
    import fcntl
    path = PORT_LOCK_DIR / f"duen-gantry-{Path(port).name}.lock"
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip()
        handle.close()
        raise GantryError(f"The gantry on {port} is in use by another program{' (' + holder + ')' if holder else ''}. "
                          "Close it first (quit the gantry terminal, or leave the game on the touch screen).") from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid {os.getpid()}: {Path(sys.argv[0]).name}")
    handle.flush()
    return handle


def unlock_port(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()           # closing releases the flock
    except Exception:
        pass


FLIP_COMMANDS = ("G0", "G1", "G92")
_Y_WORD = re.compile(r"Y(-?\d+(?:\.\d*)?)")
_MPOS = re.compile(r"MPos:(-?[\d.]+),(-?[\d.]+)")


def y_flip_mm(config: dict) -> float | None:
    """The board's Y travel when home is beside a8 (config "home_beside": "a8"), else None.
    GANTRY_HOME_BESIDE in the environment overrides the config (the tests use it).

    Everything on the Pi (the page, the planner, the layout) counts with home beside a1 and ranks
    along +Y. With home beside a8 the real Y axis runs the other way, so the link mirrors every Y
    it sends and every Y the board reports about the middle of the travel: y on the board is
    travel - y on the Pi. Files along X are unchanged. The firmware's own CARRY cannot be
    mirrored, so a flipped board is driven with plain G-code (uses_gcode).
    """
    where = str(os.environ.get("GANTRY_HOME_BESIDE") or config.get("home_beside", "a1")).strip().lower()
    if where == "a1":
        return None
    if where != "a8":
        raise GantryError(f'home_beside is "a1" or "a8", not {where!r}')
    return board_layout.travel(config)[1]


def flip_line(text: str, flip_y: float | None) -> str:
    """A line to or from the board with its Y mirrored (see y_flip_mm); unchanged when flip_y is None."""
    if flip_y is None:
        return text
    if text.startswith("<"):
        return _MPOS.sub(lambda m: f"MPos:{m.group(1)},{flip_y - float(m.group(2)):.3f}", text, count=1)
    head = text.split(" ", 1)[0].upper()
    if head in FLIP_COMMANDS:
        return _Y_WORD.sub(lambda m: f"Y{flip_y - float(m.group(1)):.3f}", text)
    return text


_X_OR_Y_WORD = re.compile(r"([XY])(-?\d+(?:\.\d*)?)")
_TRAVEL_SETTING = re.compile(r"^\$(130|131)=")


def swap_xy_flag(config: dict | None = None) -> bool:
    """Config "swap_xy": the motor the firmware calls X moves the carriage along the ranks and its Y along the files
    (2026-11-07: asked for 3 squares along X, it went 3 along Y). GANTRY_SWAP_XY=1/0 overrides (tests)."""
    env = os.environ.get("GANTRY_SWAP_XY")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes")
    return bool(config.get("swap_xy", False)) if config is not None else _config_flag("swap_xy")


def swap_line(text: str, swap: bool) -> str:
    """A line to or from the board with X and Y exchanged (see swap_xy_flag): G0/G1/G92 words, the status's MPos and
    the travel settings $130/$131. Exchanging twice gives the line back, so the same call serves both directions."""
    if not swap:
        return text
    if text.startswith("<"):
        return _MPOS.sub(lambda m: f"MPos:{m.group(2)},{m.group(1)}", text, count=1)
    if _TRAVEL_SETTING.match(text):
        return _TRAVEL_SETTING.sub(lambda m: "$131=" if m.group(1) == "130" else "$130=", text, count=1)
    head = text.split(" ", 1)[0].upper()
    if head in FLIP_COMMANDS:
        return _X_OR_Y_WORD.sub(lambda m: ("Y" if m.group(1) == "X" else "X") + m.group(2), text)
    return text


# ----------------------------------------------------------------------------- remembering the position

STATE_FILE_NAME = "position.json"
SETTLE_MM = 0.01             # the remembered spot is only rewritten when it moved more than this (spares the SD card)
RESTORE_TOL_MM = 0.05        # after $HM + G92 the board must report the remembered spot this close
# Commands that move the carriage or change its count: the file says "moving" before any of them is sent.
MOVING_COMMANDS = ("G0", "G1", "G92", "CARRY", "GOTO", "$H", "$HM", "$T")
_SETTING_LINE = re.compile(r"^\$(100|130|131)=(-?[\d.]+)")


def state_dir() -> Path:
    """Where the remembered position lives: ~/.duen-gantry, or GANTRY_STATE_DIR."""
    return Path(os.environ.get("GANTRY_STATE_DIR") or (Path.home() / ".duen-gantry"))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class PositionMemory:
    """The carriage's last known spot, on disk, so a restarted ESP32 need not be homed by hand again.

    The frame has no switches: homing is a person pushing the carriage into the corner and sending $HM, and the
    ESP32 forgets everything when it restarts (a USB port opening, a supply dip, the Pi rebooting). Unpowered
    steppers on belts stay where they are, so when the board was standing still and nobody pushed it, the spot it
    last reported is still true: $HM there and G92 to that spot puts the count back (restore_position).

    The file (JSON, written atomically: the Pi loses power abruptly):
        x, y            mm as the Pi counts (home beside a1, i.e. before the link's Y mirror)
        steps_per_mm    $100 when it was written; a different one now means the count is meaningless
        travel          [$130, $131] (information)
        home_beside     "a1" or "a8"
        moving          true from just before a command that moves or recounts the carriage until the board
                        reports Idle again: power lost while true means the position is unknown
        trusted         false after anything that may have moved the carriage behind the count's back
                        (motors off, an alarm, a stall, a stop mid-move, a new steps/mm, Forget position)
        updated, reason when, and why (for the person)
    """

    def __init__(self, path: Path, log=logging.getLogger("gantry")):
        self.path = Path(path)
        self.log = log
        self.cache: dict | None = None
        self.failed = False
        self.cache = self.load()

    @classmethod
    def for_port(cls, port: str, log=logging.getLogger("gantry")) -> "PositionMemory | None":
        """The real board's file; the pretend board never touches it (GANTRY_SIM_STATE names a file of its own, else none)."""
        if port == "sim":
            own = os.environ.get("GANTRY_SIM_STATE")
            return cls(Path(own), log) if own else None
        return cls(state_dir() / STATE_FILE_NAME, log)

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _save(self, data: dict) -> None:
        data = dict(data, updated=_now_iso())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w") as handle:
                handle.write(json.dumps(data, indent=1) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            try:                                   # the rename itself must reach the card too
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
            self.failed = False
        except OSError as exc:                     # never let the memory stop the gantry
            if not self.failed:
                self.log.warning("could not save the position to %s: %s", self.path, exc)
            self.failed = True
        self.cache = data

    def moving(self, what: str) -> None:
        """Just before a command that moves the carriage or changes its count."""
        data = self.cache or {}
        if data.get("moving"):
            return
        self._save(dict(data, moving=True, reason=f"sent {what}"))

    def settled(self, x: float, y: float, steps_per_mm: float, travel, home_beside: str) -> bool:
        """The board reports Idle and homed here. Writes only when something changed. True when it wrote."""
        data = self.cache or {}
        same = (data.get("trusted") and not data.get("moving") and data.get("home_beside") == home_beside
                and data.get("steps_per_mm") is not None and abs(float(data["steps_per_mm"]) - steps_per_mm) < 1e-4
                and abs(float(data.get("x", 1e9)) - x) <= SETTLE_MM and abs(float(data.get("y", 1e9)) - y) <= SETTLE_MM)
        if same:
            return False
        self._save({"x": round(x, 3), "y": round(y, 3), "steps_per_mm": steps_per_mm,
                    "travel": list(travel) if travel else data.get("travel"), "home_beside": home_beside,
                    "moving": False, "trusted": True, "reason": "idle and homed"})
        return True

    def untrust(self, reason: str) -> None:
        data = self.cache
        if data is None:
            data = self.load()
        if not data or (data.get("trusted") is False and data.get("reason") == reason):
            return
        self.log.info("POSITION not remembered any more: %s", reason)
        self._save(dict(data, trusted=False, reason=reason))

    def describe(self) -> dict:
        """For a page: {"remembered": bool, "saved": time or None, "text": plain words}."""
        data = self.cache if self.cache is not None else self.load()
        if not data:
            return {"remembered": False, "saved": None, "reason": "", "text": "position not remembered"}
        saved = data.get("updated")
        if data.get("trusted") and not data.get("moving"):
            return {"remembered": True, "saved": saved, "reason": data.get("reason", ""),
                    "text": f"position remembered (saved {saved})"}
        why = "a move was under way" if data.get("moving") else (data.get("reason") or "unsure")
        return {"remembered": False, "saved": saved, "reason": why, "text": f"position not remembered ({why})"}


def home_beside_name(flip_y, swap: bool = False) -> str:
    return ("a1" if flip_y is None else "a8") + (" axes swapped" if swap else "")


def restore_position(memory: "PositionMemory | None", config: dict, command, status, log=logging.getLogger("gantry"),
                     steps_per_mm: float | None = None) -> tuple[str, str]:
    """After the link is configured ($100, $130/$131, $B): put a restarted board back where it was.

    command(text) sends one line and waits for its ok; status() asks "?" (the link mirrors Y, so both count as the
    Pi does). Returns (outcome, words for the person):
        "kept"      the board is homed already (it did not restart): its count is trusted and the file refreshed
        "restored"  it was not homed; the file was trusted and still: $HM, G92 to the remembered spot, checked
        "moving"    the power went off during a move: the position is unknown
        "none"      nothing usable remembered (no file, untrusted, other steps/mm or home corner, an alarm)
    """
    st = status()
    if st["homed"] and st["state"] != "Alarm":
        return "kept", f"The board kept its position ({st['x']:.1f}, {st['y']:.1f}): no homing needed."
    if st["state"] == "Alarm" and st["reason"] not in ("", "not homed"):
        return "none", f"The board is in alarm ({st['reason']}): check it, then home."
    if memory is None:
        return "none", "No position is remembered for this board: home once."
    data = memory.load()
    if not data:
        return "none", "No position remembered yet: home once (it is remembered from then on)."
    if data.get("moving"):
        return "moving", "The power went off during a move, so the position is unknown: home once more."
    if not data.get("trusted"):
        return "none", f"The remembered position is not trusted ({data.get('reason') or 'unsure'}): home once more."
    want_spm = float(steps_per_mm if steps_per_mm is not None else board_layout.steps_per_mm(config))
    if data.get("steps_per_mm") is None or abs(float(data["steps_per_mm"]) - want_spm) > 1e-4:
        return "none", (f"The position was remembered at {data.get('steps_per_mm')} steps/mm, the board now uses "
                        f"{want_spm:g}: home once more.")
    beside = home_beside_name(y_flip_mm(config), swap_xy_flag(config))
    if data.get("home_beside") != beside:
        return "none", f"The position was remembered with home beside {data.get('home_beside')}, now {beside}: home once more."
    x, y = float(data["x"]), float(data["y"])
    when = data.get("updated", "?")
    command("$HM")
    command(f"G92 X{x:.3f} Y{y:.3f}")
    st = status()
    if not st["homed"] or abs(st["x"] - x) > RESTORE_TOL_MM or abs(st["y"] - y) > RESTORE_TOL_MM:
        if memory is not None:
            memory.untrust("the restore did not land on the remembered spot")
        for line in (f"$100={want_spm + 0.01:.4f}", f"$100={want_spm:.4f}"):   # a new $100 makes the board forget
            try:
                command(line)
            except Exception:
                pass
        return "none", (f"Restoring the position failed (the board reports {st['x']:.2f}, {st['y']:.2f}, "
                        f"not {x:.2f}, {y:.2f}): home once more.")
    text = f"Position restored from {when}: {x:.1f}, {y:.1f} — no homing needed."
    log.info("POSITION %s", text)
    return "restored", text


# ----------------------------------------------------------------------------- the home spot

def home_spot_mm(config: dict) -> tuple[float, float] | None:
    """config "home_spot_mm": [dx, dy], how far the spot where a person homes the carriage is from the frame's true
    corner, in mm along X and along Y AWAY from that corner (both >= 0). None (absent, null or 0,0): home is the
    corner itself. home_square, when set, uses that square's calibrated centre instead of home_spot_mm.
    Raises GantryError for an invalid square or a spot outside the travel."""
    spot = config.get("home_spot_mm")
    square = str(config.get("home_square") or "").strip().lower()
    if square:
        if not re.fullmatch(r"[a-h][1-8]", square):
            raise GantryError(f"home_square must be a square from a1 to h8, not {square!r}")
        x, y = BoardMap(config).square_mm(square)
        flip = y_flip_mm(config)
        spot = (x, flip - y if flip is not None else y)
    if not spot:
        return None
    try:
        dx, dy = float(spot[0]), float(spot[1])
    except (TypeError, ValueError, IndexError):
        raise GantryError(f"home_spot_mm is [dx, dy] in mm, not {spot!r}") from None
    tx, ty = board_layout.travel(config)
    if not (0.0 <= dx <= tx and 0.0 <= dy <= ty):
        raise GantryError(f"home_spot_mm {dx:g}, {dy:g} is outside the travel of {tx:g} x {ty:g} mm")
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return None
    return dx, dy


def home_xy(config: dict) -> tuple[float, float]:
    """Where the carriage is after homing, as the Pi counts: the corner (0, 0; or 0, travel_y with home beside a8,
    since the link mirrors Y) moved by home_spot_mm away from it."""
    dx, dy = home_spot_mm(config) or (0.0, 0.0)
    flip = y_flip_mm(config)
    return dx, (flip - dy if flip is not None else dy)


def home_spot_from_here(status: dict, config: dict) -> list[float]:
    """The home_spot_mm to save when the board was homed in the true corner and the carriage has since been jogged to
    the spot where the person will home it from now on. status: Gantry.status() (as the Pi counts)."""
    if not status.get("homed"):
        raise GantryError("The board is not homed: home it in the true corner first, then jog to the new home spot.")
    flip = y_flip_mm(config)
    dx, dy = float(status["x"]), (flip - float(status["y"]) if flip is not None else float(status["y"]))
    if dx < -0.5 or dy < -0.5:
        raise GantryError(f"That spot is {dx:.1f}, {dy:.1f} mm from the corner: past the corner itself.")
    return [round(max(dx, 0.0), 2), round(max(dy, 0.0), 2)]


def set_home_here(target, config: dict) -> dict:
    """A person has put the carriage on its home spot: $HM (this is the corner), then, with home_spot_mm set, G92 so
    the count says corner + offset. target: a Link or a Gantry. G92 goes through Link.write, which mirrors Y for
    home beside a8: the board itself hears X dx Y dy. The status afterwards (Idle and homed) is what the position
    memory records. Returns that status."""
    gantry = target if isinstance(target, Gantry) else Gantry(target)
    gantry.link.flip_y = y_flip_mm(config)
    gantry.link.swap_xy = swap_xy_flag(config)
    x, y = home_xy(config)
    gantry.command("$HM")
    if home_spot_mm(config):
        gantry.command(f"G92 X{x:.3f} Y{y:.3f}")
    state = gantry.status()
    if not state["homed"] or abs(state["x"] - x) > RESTORE_TOL_MM or abs(state["y"] - y) > RESTORE_TOL_MM:
        raise GantryError(f"Homing here did not take: the board reports {state['x']:.2f}, {state['y']:.2f}, "
                          f"not {x:.2f}, {y:.2f}.")
    return state


def update_config_file(path: Path, values: dict, backup_suffix: str = ".before-touch") -> dict:
    """Set `values` in the JSON config file (everything else kept as it is), keeping the old file once as
    <name><backup_suffix> (gantry_config.json.before-touch). Written atomically. Returns the file's new contents."""
    path = Path(path)
    text = path.read_text()
    data = json.loads(text)
    backup = path.with_name(path.name + backup_suffix)
    if not backup.exists():
        backup.write_text(text)
    data.update(values)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return data


class Link:
    """A line-based connection to the board: real serial, or the pretend board as a subprocess.

    A background thread reads lines into a queue, so a reply is never missed while this side
    is busy, and reading with a timeout is simple.
    """

    def __init__(self, port: str, baud: int, log=logging.getLogger("gantry"), keep_running: bool | None = None,
                 memory="auto"):
        """keep_running (config "keep_esp32_running"; None: read it from DEFAULT_CONFIG): open the serial port with
        DTR and RTS held low, so the ESP32's auto-reset circuit is not triggered and the board keeps its count.
        Flashing (esptool / arduino-cli upload) opens the port itself and still resets the board into its
        bootloader: this does not change that.
        memory: a PositionMemory, None for none, or "auto" (PositionMemory.for_port: the pretend board never uses
        the real board's file)."""
        self.log = log
        self.traces: list[str] = []
        self.process, self.serial, self.lock_file = None, None, None
        self.lines: queue.Queue = queue.Queue()
        self.pretend = port == "sim"   # the pretend board may always be homed: nothing can hit anything
        # Home beside a8 (config "home_beside": "a8"): the travel along Y, else None. See flip_line().
        self.flip_y: float | None = None
        # Config "swap_xy": the board's X motor drives the Pi's Y and the other way round (swap_line), after the mirror.
        self.swap_xy: bool = swap_xy_flag()
        self.memory: PositionMemory | None = PositionMemory.for_port(port, log) if memory == "auto" else memory
        self.steps_per_mm: float | None = None      # $100 as last sent or reported
        self.travel: list[float | None] = [None, None]
        self.doubt: str | None = None               # why the count may be wrong; cleared by a fresh home
        self.batch_depth = 0                        # inside batch(): Idle between moves does not rewrite the file
        self.boot_lines: list[str] = []             # what the board printed while the link settled
        self.restarted: bool | None = None          # did the board print its banner on opening (it restarted)?
        if keep_running is None:
            keep_running = _config_flag("keep_esp32_running")
        self.keep_running = bool(keep_running) and port != "sim"
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
            self.lock_file = lock_port(port)      # before opening: opening the port may restart the ESP32
            try:
                self.serial = open_serial(serial, port, baud, self.keep_running)
            except Exception:
                unlock_port(self.lock_file)
                self.lock_file = None
                raise
            source = self.serial
            if not self.keep_running:
                time.sleep(2.0)  # an Arduino or ESP32 resets when the port opens
        self.reader = threading.Thread(target=self._pump, args=(source,), name="gantry-reader", daemon=True)
        self.reader.start()
        if self.keep_running:
            self._settle(SETTLE_S)

    def _settle(self, seconds: float) -> None:
        """With DTR/RTS held low the board should still be running: it answers "?" at once. If it restarted after
        all (a USB adapter that pulses the lines anyway), its banner comes within a second or two, and anything
        sent before that is lost. So: ask "?" every quarter second until a status line or the banner arrives, or
        `seconds` pass (then carry on as before: the commands that follow have their own timeouts)."""
        deadline = time.monotonic() + seconds
        next_ask = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_ask:
                try:
                    self.serial.write(b"?\n")
                except Exception:
                    return
                next_ask = time.monotonic() + 0.25
            try:
                line = self.lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                self.lines.put(None)
                return
            if line.startswith("<"):
                self.restarted = bool(self.restarted)   # it answered: still running (the caller's flip_y is not set yet)
                return
            self.boot_lines.append(line)
            if "ready" in line and line.startswith("[duen-gantry"):
                self.restarted = True
                self.log.info("the board restarted when its port opened: %s", line)
                time.sleep(0.2)            # let the rest of its boot print settle
                return

    @contextlib.contextmanager
    def batch(self):
        """A run of moves (a job, a console task): the file says "moving" from its first move to the first Idle
        after it, instead of being rewritten at every stop in between."""
        self.batch_depth += 1
        try:
            yield self
        finally:
            self.batch_depth -= 1

    def _watch_sent(self, text: str) -> None:
        """Every line sent, as the Pi counts (before the Y mirror): keep the remembered position honest."""
        memory = self.memory
        head = text.strip().split(" ", 1)[0].upper()
        found = _SETTING_LINE.match(text.strip())
        if found:
            self._learn_setting(found.group(1), float(found.group(2)), sent=True)
        if memory is None:
            return
        if head in ("$H", "$HM"):
            self.doubt = None                       # a fresh zero
        if head in MOVING_COMMANDS:
            memory.moving(head)
        elif head == "M18":
            self._untrust("the motors were switched off (M18): the carriage may have been pushed")
        elif head == "!" and (memory.cache or {}).get("moving"):
            self._untrust("stopped in the middle of a move")

    def _learn_setting(self, key: str, value: float, sent: bool = False) -> None:
        """$100/$130/$131 sent, or reported by $$. Only a SENT new steps/mm untrusts the file: a restarted board
        reports its compiled value until the Pi sends the configured one again."""
        if key == "100":
            remembered = (self.memory.cache or {}).get("steps_per_mm") if self.memory else None
            if sent and remembered is not None and abs(float(remembered) - value) > 1e-4:
                self._untrust(f"steps per mm changed ({float(remembered):g} -> {value:g})")
            self.steps_per_mm = value
        else:
            self.travel[0 if key == "130" else 1] = value

    def _untrust(self, reason: str) -> None:
        self.doubt = reason
        if self.memory is not None:
            self.memory.untrust(reason)

    def _watch_status(self, line: str) -> None:
        """Every line received, as the Pi counts (after the Y mirror)."""
        found = _SETTING_LINE.match(line)
        if found:
            self._learn_setting(found.group(1), float(found.group(2)))
            return
        if self.memory is None or not line.startswith("<"):
            return
        st = parse_status(line)
        if not st:
            return
        if st["host_lost"]:
            self._untrust("the board lost contact with the Pi and switched its motors off")
        elif st["state"] == "Alarm" and st["reason"] not in ("", "not homed"):
            self._untrust(f"alarm: {st['reason']}")
        elif st["state"] == "Hold":
            self._untrust("the board was stopped (hold) during a move")
        elif (st["state"] == "Idle" and st["homed"] and self.doubt is None and self.batch_depth == 0
              and self.steps_per_mm is not None):
            self.memory.settled(st["x"], st["y"], self.steps_per_mm,
                                self.travel if None not in self.travel else None, home_beside_name(self.flip_y, self.swap_xy))

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
        try:
            self._watch_sent(text)
        except Exception:      # the memory must never stop a command (above all "!")
            self.log.exception("position memory")
        text = swap_line(flip_line(text, self.flip_y), self.swap_xy)
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
        line = flip_line(swap_line(line, self.swap_xy), self.flip_y)
        try:
            self._watch_status(line)
        except Exception:
            self.log.exception("position memory")
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
        unlock_port(self.lock_file)
        self.lock_file = None


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


POLL_MIN_S = 0.1      # how often the Pi asks "?" once a move should be done
POLL_MAX_S = 2.0      # the longest it stays quiet: well inside the firmware's 20 s host watchdog
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

    def wait_idle(self, timeout: float, what: str, expected: float = 0.0) -> dict:
        """Poll until the board is idle. Alarm or hold means the move did not finish; a timeout stops everything.

        `expected` (seconds, from predict) keeps the Pi quiet while the board is surely still moving: it asks half
        the remaining time later, at least every POLL_MAX_S so the firmware's 20 s host watchdog never fires, and
        every POLL_MIN_S once the move should be about done. Without it: every POLL_MIN_S."""
        start = time.monotonic()
        deadline = start + timeout
        if expected and getattr(self.link, "pretend", False):
            expected /= float(os.environ.get("GANTRY_SIM_SPEED", "50") or 50)    # the pretend board runs faster
        due = start + 0.8 * expected        # a little early: the guess runs long or short by a few tenths
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
            time.sleep(min(POLL_MAX_S, max(POLL_MIN_S, (due - time.monotonic()) / 2)))

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

    def unlock(self, may_bump: bool = True) -> None:
        """After a person has checked the board: clear the alarm or hold and find the position again."""
        self.command("$X")
        self.ensure_homed(may_bump)

    def restore_position(self, config: dict) -> tuple[str, str]:
        """A restarted board put back at its remembered spot (gantry_driver.restore_position). Call it once the link
        is configured (set_motion, set_board)."""
        outcome, text = restore_position(getattr(self.link, "memory", None), config, self.command, self.status, self.log,
                                         getattr(self.link, "steps_per_mm", None))
        self.log.info("POSITION %s: %s", outcome, text)
        return outcome, text

    def ensure_homed(self, may_bump: bool = True, config: dict | None = None) -> None:
        """A real move needs a real zero. A restarted board is first put back at its remembered spot when that can be
        trusted (config given: restore_position). Otherwise, with may_bump the board finds it itself ($H: switches,
        or a slow bump into the corner on a frame without them); else a person must have homed it ($HM by hand, or
        $H) first."""
        state = self.status()
        if state["state"] == "Alarm" or not state["homed"]:
            why = ""
            if config is not None and getattr(self.link, "memory", None) is not None:
                outcome, why = self.restore_position(config)
                if outcome in ("restored", "kept"):
                    return
            if not may_bump:
                raise GantryError("The board is not homed. Push the carriage into the bottom-left corner and send $HM "
                                  "(home.sh manual), or send $H to let it bump-home, then try again."
                                  + (f" ({why})" if why else ""))
            self.home()

    def park(self, config: dict) -> bool:
        """The empty magnet (magnet off, arm down) to the starting position (config start_mm, a8's real centre), so
        the remembered spot is always a known one on the board. False when there is no starting position."""
        start = config.get("start_mm")
        if not start:
            return False
        x, y = float(start[0]), float(start[1])
        BoardMap(config).check(x, y, "the starting position")
        state = self.status()
        if state["magnet"]:
            self.command("M5")
        if state.get("servo") == "up":
            self.command("M8")
            settle = float(config.get("servo_settle_s", 3.0)) if config.get("servo", True) else 0.0
            if settle:
                self.command(f"G4 P{settle:.3f}")
                self.wait_idle(settle + 5, "the arm going down")
        if abs(state["x"] - x) < 0.01 and abs(state["y"] - y) < 0.01:
            return True
        speed = float(config.get("travel_speed_mm_s", 60.0))
        self.log.info("PARK at the starting position %.1f, %.1f", x, y)
        self.command(f"G1 X{x:.3f} Y{y:.3f} F{speed * 60:.0f}")
        self.wait_idle(10 + (config["travel_mm"][0] + config["travel_mm"][1]) / max(speed, 1) * 3, "parking at the start")
        return True

    def set_board(self, config: dict) -> None:
        """Give the firmware this board's geometry, so its idea of e4 is the Pi's."""
        self.command(BoardMap(config).setup_line())

    def set_motion(self, config: dict) -> None:
        """Firmware 0.7: steps/mm ($100, from mm_per_turn) and the travel ($130, $131) from the config. $100 to the
        value the board already has changes nothing; a different one forgets the position (home again). Older
        firmware has no $130/$131 and keeps its compiled travel: that refusal is ignored."""
        for line in board_layout.motion_lines(config):
            try:
                self.command(line)
            except GantryError as exc:
                if line.startswith("$100") or "unknown" not in str(exc):
                    raise
                self.log.info("the firmware has no %s (before 0.7): it keeps its own travel", line.split("=")[0])
        servo = board_layout.servo_line(config)
        if servo:
            try:
                self.command(servo)
            except GantryError as exc:   # the arm is up, or firmware before 0.7.2: keep going, say so
                self.log.warning("the board refused %s (%s): the arm keeps its current angles", servo, exc)

    def set_step_pulse(self, config: dict) -> None:
        """Firmware 0.5+: the STEP pulse length ($0) from config["step_pulse_us"], when set. The firmware boots with
        1500 us, which caps the step rate at 333/s: 8 mm/s at 1/8 steps, too slow for long carries under the
        magnet's time limit. A board wired for 1/8 steps wants a shorter pulse here (the shortest its wiring passes).
        Older firmware has no $0 and keeps its own 3 us; that refusal is ignored."""
        value = config.get("step_pulse_us")
        if not value:
            return
        try:
            self.command(f"$0={int(value)}")
        except GantryError as exc:
            if "unknown" not in str(exc):
                raise
            self.log.info("the firmware has no $0 (before 0.5): it keeps its own step pulse")

    def perform(self, lines: list[tuple[str, str]], config: dict, progress=None) -> None:
        """Run planned commands, waiting for each to finish before the next. progress(text) is told about every carry.

        Comment lines ("# ...": notes, and the removals a person has already made) are logged, not sent.
        """
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
                self.wait_idle(10 + 2 * guess["seconds"], note, expected=guess["seconds"])
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
    config.setdefault("travel_speed_mm_s", 60.0)
    config.setdefault("carry_speed_mm_s", 40.0)
    config.setdefault("grab_settle_s", 0.3)
    config.setdefault("release_settle_s", 0.2)
    config.setdefault("servo", True)
    config.setdefault("servo_settle_s", 3.0)
    config.setdefault("home_on_start", True)
    config.setdefault("protocol", "duen")
    return config


def uses_gcode(config: dict) -> bool:
    """Plain G-code planned here; also whenever home is beside a8, since the firmware's CARRY counts from a1."""
    return str(config.get("protocol", "duen")).lower() == "grbl" or y_flip_mm(config) is not None or swap_xy_flag(config)


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


def may_bump_home(config: dict, link) -> bool:
    """Whether the driver may send $H itself. On the real frame that is the config's call (home_on_start:
    false once a person homes it by hand, since there are no switches); the pretend board may always."""
    return bool(config.get("home_on_start", True)) or bool(getattr(link, "pretend", False))


def parks_after_job(config: dict) -> bool:
    """config "park_at_start_after_job" (default: on when a starting position, start_mm, is saved)."""
    return bool(config.get("start_mm")) and bool(config.get("park_at_start_after_job", True))


def run_job(job: dict, config: dict, link: Link, log=logging.getLogger("gantry"), progress=None, park: bool = True) -> dict:
    """Perform one job on an open link. progress(text), when given, hears about each carry as it starts.

    Order: plan (fails early), refuse if the board wants a person first, give it the board
    geometry ($B, firmware 0.2), home if the config says so, then run. Any fault leaves the
    magnet off and the arm down. A job that starts with a "remove" step runs its carries at once:
    the caller must already have waited for the person to take that piece off and for the camera
    to confirm the square is empty.
    """
    lines = plan_job(job, config)
    link.flip_y = y_flip_mm(config)
    link.swap_xy = swap_xy_flag(config)
    gantry = Gantry(link, log)
    gantry.check_safe()
    gantry.set_motion(config)            # GRBL reads $100 / $130 / $131 the same way
    if not uses_gcode(config):
        gantry.set_step_pulse(config)
        gantry.set_board(config)
    gantry.ensure_homed(may_bump_home(config, link), config)
    log.info("JOB %s: %d commands", job.get("label", "?"), len(lines))
    batch = link.batch() if hasattr(link, "batch") else contextlib.nullcontext()
    try:
        with batch:
            gantry.perform(lines, config, progress)
            if park and parks_after_job(config):
                gantry.park(config)
    except BaseException:
        gantry.make_safe(stop_first=False)
        raise
    return gantry.status()


def run_jobs(jobs: list[dict], config: dict, link: Link, log=logging.getLogger("gantry"), progress=None) -> dict:
    state = None
    for i, job in enumerate(jobs):
        state = run_job(job, config, link, log, progress, park=i == len(jobs) - 1)
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
    """Measure the board at the frame: steer the magnet under a1, h1 and h8, and save the layout.

    Moves are typed as x+10, y-0.5 and so on (millimetres); 'ok' records the spot. a1 gives the
    origin of the squares, a1 to h1 the square size along X, h1 to h8 along Y; the docking lanes
    follow 42.5 mm out from the edge files (board_layout.from_points). The console's Board
    calibration panel does the same with buttons, and can also measure the lanes and the travel.
    """
    board = BoardMap(config)
    gantry.set_motion(config)
    gantry.ensure_homed(may_bump_home(config, gantry.link), config)
    st = gantry.status()
    pos = [st["x"], st["y"]]

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

    a1 = steer("Step 1 of 3: put the magnet under the centre of a1.")
    h1 = steer("Step 2 of 3: now under the centre of h1, on the same rank.")
    h8 = steer("Step 3 of 3: now under the centre of h8.")
    try:
        new, report = board_layout.from_points(config, a1, h1, h8)
    except ValueError as exc:
        raise GantryError(str(exc)) from None
    Path(config_path).write_text(json.dumps(new, indent=2) + "\n")
    config.clear()
    config.update(new)
    write(f"Saved to {config_path}:")
    for line in report:
        write(f"  {line}")
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
    parser.add_argument("--calibrate", action="store_true", help="steer the magnet under a1, h1 and h8, then save the board's layout to the config")
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

        link = Link(port, int(config.get("baud", 115200)), log, keep_running=bool(config.get("keep_esp32_running", False)))
        link.flip_y = y_flip_mm(config)
        link.swap_xy = swap_xy_flag(config)
        try:
            if jobs is not None:
                state = run_jobs(jobs, config, link, log)
            else:
                gantry = Gantry(link, log)
                if args.goto:                     # $100/$130/$131 first, so a restarted board can be put back
                    gantry.set_motion(config)
                if args.unlock:
                    gantry.unlock(may_bump_home(config, gantry.link))
                if args.calibrate:
                    calibrate(gantry, config, args.config)
                if args.home:
                    gantry.home()
                if args.goto:
                    BoardMap(config).check(args.goto[0], args.goto[1], "--goto")
                    gantry.ensure_homed(may_bump_home(config, gantry.link), config)
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
