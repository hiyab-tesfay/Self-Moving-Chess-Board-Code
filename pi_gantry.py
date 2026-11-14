"""The gantry for the touch-screen app: the computer's moves played by the board itself.

The app (pi_touch.py) keeps playing exactly as before; when a gantry is configured it asks this
module to carry the computer's pieces instead of asking the person to, and still verifies the
board with the camera afterwards (expected_squares() says what it should see).

The gantry never takes a captured piece off the board. When the computer captures, the app asks
the person to take the piece off, waits until the camera has seen the square empty (each time the
person presses Check), and only then lets the gantry play the capturing move:

    player = GantryPlayer.from_env()          # None when no gantry is configured
    player.new_game()                          # the pieces are in the starting position
    player.human_moved(board, move)            # the person moved by hand: follow the pieces
    taken = player.removal_needed(board, move) # ("d5", "p") when the person must take a piece off first, else None
    ...                                        # the app shows it, waits, and camera-checks that the square is empty
    notes = player.play(board, move, say)      # the gantry plays: returns what the person must still do
    player.reset(say)                          # everything back home; returns the pieces the person must put back
    player.expected_squares()                  # the squares the camera should see occupied

Manual control, for the touch screen's Gantry screen (each opens the link if needed and keeps it open until
close(); every move with the magnet off and the arm down; see "manual control" below):
    player.status() / jog(dx, dy) / center() / go_to_square("e4") / set_home_here() / save_home_spot_here() /
    set_start_here() / return_home() / motors_off()   -> {x, y, homed, remembered, memory, units, ...}
    player.plan_test_move("e2e4") -> {path_units, carries, ...} (no link)   player.test_move("e2e4", carry) -> as above

Configuration, from the environment:
    CHESS_GANTRY_PORT    serial device of the ESP32 (e.g. /dev/ttyUSB0), "auto" for the first of the
                         sorted /dev/ttyUSB* then /dev/ttyACM* (none found: a warning and no
                         gantry), or "sim" for the pretend board in gantry/firmware/host.
                         Unset: no gantry, from_env() returns None.
    CHESS_GANTRY_DIR     the gantry/pi folder of the Self-Moving-Chess-Board-Code repo (branch
                         gantry). Default: the first of ~/chess-gantry/gantry/pi and
                         ~/Self-Moving-Chess-Board-Code/gantry/pi that exists.
    CHESS_GANTRY_CONFIG  gantry_config.json; default <dir>/gantry_config.json.

The planning (carry_plan.py: a capture is a removal by the person then one carry, castling is two
carries, a promotion carries the pawn and leaves a note) and the paths with real piece widths
(board_paths.py) live in that folder with the driver; this file only wires them to the app. The
docking lanes beside the board are not used. Every fault comes back as GantryError whose
text can go straight on the screen, and never leaves the magnet on: the driver releases it, lowers
the arm and stops the motion on any exception, and this module sends M5 and M8 once more.
"""
from __future__ import annotations

import glob
import logging
import os
from pathlib import Path
import sys
import threading
from manual_controls import valid_manual

import chess

AUTO_PORT_PATTERNS = ("/dev/ttyUSB*", "/dev/ttyACM*")   # CP210x/CH340 first, then CDC-ACM
DEFAULT_DIRS = (Path.home() / "chess-gantry" / "gantry" / "pi", Path.home() / "Self-Moving-Chess-Board-Code" / "gantry" / "pi")
log = logging.getLogger("pi_gantry")


class GantryError(Exception):
    """str(exc) is plain words for the screen."""


def find_gantry_dir(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_dir() else None
    for candidate in DEFAULT_DIRS:
        if candidate.is_dir():
            return candidate
    return None


def find_serial_port() -> str | None:
    """The first of the sorted /dev/ttyUSB*, then the first of the sorted /dev/ttyACM*; None when there is neither."""
    for pattern in AUTO_PORT_PATTERNS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def load_modules(directory: Path):
    """gantry_driver and carry_plan from the gantry folder (board_paths comes with the driver)."""
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    import carry_plan  # noqa: E402
    import gantry_driver  # noqa: E402
    return gantry_driver, carry_plan


class GantryPlayer:
    def __init__(self, directory: Path, port: str, config_path: Path) -> None:
        self.directory, self.port, self.config_path = Path(directory), port, Path(config_path)
        self.gd, self.cp = load_modules(self.directory)
        try:
            self.config = self.gd.load_config(self.config_path)
        except (OSError, ValueError) as exc:
            raise GantryError(f"The gantry's settings could not be read ({self.config_path}): {exc}") from None
        self.link = None
        self.manual = None          # the Gantry the manual controls use while this link is open (see _manual_gantry)
        self.restore_text = ""      # what connecting for the manual controls found out about the position
        self.save_to_file = port != "sim"   # the pretend board changes its settings in memory only, like the console
        self.layout = self.cp.Layout()
        self.manual_home_ready = not self.config.get("manual_home_on_connect", False)
        self.cancelled = threading.Event()

    # ------------------------------------------------------------------ set-up

    @classmethod
    def from_env(cls) -> "GantryPlayer | None":
        """The gantry the environment describes, or None when there is none. Never raises."""
        try:
            port = (os.environ.get("CHESS_GANTRY_PORT") or "").strip()
            if not port:
                return None
            if port.lower() == "auto":
                found = find_serial_port()
                if found is None:
                    log.warning("CHESS_GANTRY_PORT=auto but no serial device was found (%s); playing without the gantry",
                                ", ".join(AUTO_PORT_PATTERNS))
                    return None
                log.info("CHESS_GANTRY_PORT=auto: using %s", found)
                port = found
            directory = find_gantry_dir(os.environ.get("CHESS_GANTRY_DIR"))
            if directory is None:
                log.warning("CHESS_GANTRY_PORT is set but no gantry folder was found (CHESS_GANTRY_DIR, %s)", ", ".join(str(d) for d in DEFAULT_DIRS))
                return None
            config = Path(os.environ.get("CHESS_GANTRY_CONFIG") or directory / "gantry_config.json").expanduser()
            if not config.is_file():
                log.warning("gantry config %s not found", config)
                return None
            return cls(directory, port, config)
        except Exception as exc:  # a broken install must not stop the app from starting
            log.warning("gantry not available: %s", exc)
            return None

    def connect(self) -> None:
        """Open the link, tell the board where the squares are, home if the config says so. Raises GantryError."""
        if self.link is not None:
            return
        self._open_link()
        try:
            if self.config.get("manual_home_on_connect", False):
                self._manual_gantry()
                return
            gantry = self.gd.Gantry(self.link, log)
            gantry.check_safe()
            if not self.gd.uses_gcode(self.config):
                gantry.set_board(self.config)
            if self.config.get("home_on_start", True):
                gantry.ensure_homed()
        except self.gd.GantryError as exc:
            self._make_safe()
            raise GantryError(str(exc)) from None
        except Exception as exc:
            self._make_safe()
            self.close()
            raise GantryError(f"The gantry did not answer: {exc}") from None

    def _open_link(self) -> None:
        """Just the link (the port lock taken first, so a second program gets a plain refusal). Raises GantryError."""
        if self.link is not None:
            return
        try:
            flip_y = self.gd.y_flip_mm(self.config)
            self.link = self.gd.Link(self.port, int(self.config.get("baud", 115200)), log,
                                     keep_running=self.config.get("keep_esp32_running"))
            self.link.flip_y = flip_y   # home beside a8: every Y mirrored, from the first status on
            self.link.swap_xy = self.gd.swap_xy_flag(self.config)   # X and Y motors exchanged (config swap_xy)
            self.manual_home_ready = not self.config.get("manual_home_on_connect", False)
        except self.gd.GantryError as exc:
            raise GantryError(str(exc)) from None
        except Exception as exc:
            raise GantryError(f"Could not open the gantry on {self.port}: {exc}") from None

    def close(self) -> None:
        self.manual = None
        self.manual_home_ready = not self.config.get("manual_home_on_connect", False)
        if self.link is not None:
            try:
                self.link.close()
            finally:
                self.link = None

    def stop(self) -> None:
        """Stop immediately; only an explicit manual home permits another carry."""
        self.cancelled.set()
        self.manual_home_ready = False
        if self.link is not None:
            for command in ("!", "M5", "M8"):
                try:
                    self.link.write(command)
                except Exception as exc:
                    log.warning("Could not send %s while stopping: %s", command, exc)

    def unlock(self) -> None:
        """After a person has checked the pieces: clear the board's alarm or hold and home it again."""
        self.connect()
        try:
            if self.config.get("manual_home_on_connect", False):
                self.manual_home_ready = False
                self.gd.Gantry(self.link, log).command("$X")
                return
            self.gd.Gantry(self.link, log).unlock()
        except self.gd.GantryError as exc:
            raise GantryError(str(exc)) from None

    # ------------------------------------------------------------------ the game

    def new_game(self, board: chess.Board | None = None) -> None:
        self.layout = self.cp.Layout(board)

    def human_moved(self, board_before: chess.Board, move: chess.Move) -> None:
        try:
            self.layout.apply_human(board_before, move)
        except (ValueError, KeyError) as exc:
            raise GantryError(f"The pieces do not match the game ({exc}).") from None

    def removal_needed(self, board_before: chess.Board, move: chess.Move) -> tuple[str, str] | None:
        """The piece a person must take off before the gantry can play `move`: (square, symbol), or None.

        A capture names the square the taker lands on; en passant names the passed pawn's square.
        Call this before play(): show it, wait, and have the camera confirm the square is empty.
        """
        return self.cp.removal(board_before, move)

    def play(self, board_before: chess.Board, move: chess.Move, say=None) -> list[str]:
        """Carry the pieces for `move`. say(text) hears about each carry; returns the notes left for the person.

        Call removal_needed() first; have the camera confirm the square is empty; then play. This
        method does not wait for anything: the job it plans may start with a "remove" step for the
        captured piece, but the screen has already verified the removal with the camera, so the
        driver runs only the carries (one for a capture, straight onto the emptied square). The
        removed piece leaves the layout: the person keeps it.
        """
        if board_before.turn != chess.BLACK or board_before.color_at(move.from_square) != chess.BLACK:
            raise GantryError("White is moved by the human. The bot gantry only carries Black pieces.")
        trial = self.layout.copy()
        try:
            job = self.cp.job_for_move(trial, board_before, move)
        except (ValueError, KeyError) as exc:
            raise GantryError(f"The gantry cannot play {move.uci()}: {exc}") from None
        self._run([job], say)
        self.layout = trial
        return [step["message"] for step in job["steps"] if step["op"] == "note"]

    def reset(self, say=None) -> list[str]:
        """Carry every piece on the board back to a home square; returns what the person must do by hand.

        The gantry cannot bring back the pieces the person took, so the notes say "Put a black pawn
        on e7." for each such square (and "Take the white queen off e4." for a spare piece that has
        no home square). After this the layout is the full starting position, as the camera check
        expects: it only passes once the person has put those pieces back.
        """
        trial = self.layout.copy()
        job = self.cp.job_for_reset(trial)
        try:
            jobs = self.gd.split_job(job, self.gd.BoardMap(self.config), self.config)
        except self.gd.GantryError as exc:
            raise GantryError(str(exc)) from None
        self._run(jobs, say)
        self.layout = self.cp.Layout()
        return [step["message"] for step in job["steps"] if step["op"] == "note"]

    def expected_squares(self) -> set[str]:
        """The squares the camera should see occupied: after a capture the taken piece is gone, after reset() the full start."""
        return set(self.layout.on_board())

    # ------------------------------------------------------------------ manual control (the touch screen's Gantry panel)
    # Each method opens the link if it is not open yet (the port lock: one program at a time) and leaves it open:
    # a burst of jog presses must not pay for opening it every time (see _manual_gantry). close() lets it go.
    # Every move goes with the magnet off and the arm down, at the travel speed. Faults come back as GantryError.

    def status(self) -> dict:
        """{x, y, homed, state, reason, magnet, servo, remembered, memory, restore, units}: where the carriage is
        as the Pi counts (home beside a8 mirrors Y), whether the position file trusts it, and `units` (the spot in
        square units: a1's centre 0,0) for the screen's little board."""
        return self._manual(lambda gantry: self._report(gantry.status()))

    def jog(self, dx: float, dy: float) -> dict:
        """Move by dx, dy mm (as the Pi counts: +X towards the h-file, +Y towards rank 8). Homed: the target is
        kept inside the travel (a jog past the frame stops at its edge). Not homed: every direction works, so a
        person can steer the carriage onto the home spot: the spot is first called the middle of the travel (G92;
        the firmware only refuses targets outside its travel), and the jog runs at the slower unhomed speed
        (config unhomed_jog_speed_mm_s, default 15 mm/s) because nothing stops it at the frame."""
        def work(gantry):
            st = self._usable(gantry.status(), homed=False)
            if not st["homed"]:
                if st["state"] == "Alarm":     # "not homed" after a restart
                    gantry.command("$X")
                travel = self.gd.BoardMap(self.config).travel
                mid_x, mid_y = travel[0] / 2.0, travel[1] / 2.0       # the middle: the same spot with Y mirrored
                gantry.command(f"G92 X{mid_x:.3f} Y{mid_y:.3f}")
                st = self._usable(gantry.status(), homed=False)
                speed = float(self.config.get("unhomed_jog_speed_mm_s", 15.0))
                return self._move(gantry, st, st["x"] + dx, st["y"] + dy, "jog", check=False, speed=speed, keep_arm=True)
            travel = self.gd.BoardMap(self.config).travel
            tx = min(max(st["x"] + dx, 0.0), travel[0])
            ty = min(max(st["y"] + dy, 0.0), travel[1])
            if abs(tx - st["x"]) < 0.01 and abs(ty - st["y"]) < 0.01:
                raise self.gd.GantryError("The carriage is already at the edge of its travel that way.")
            return self._move(gantry, st, tx, ty, "jog", check=True, keep_arm=True)
        return self._manual(work)

    def center(self) -> dict:
        """Center gantry: the empty magnet to the starting position (start_mm, saved with Set start here), or a8's
        centre on the board map when none is saved."""
        def work(gantry):
            st = self._usable(gantry.status(), homed=True)
            start = self.config.get("start_mm")
            x, y = (float(start[0]), float(start[1])) if start else self.gd.BoardMap(self.config).square_mm("a8")
            return self._move(gantry, st, x, y, "going to the start")
        return self._manual(work)

    def go_to_square(self, square: str) -> dict:
        square = str(square).strip().lower()
        if len(square) != 2 or square[0] not in "abcdefgh" or square[1] not in "12345678":
            raise GantryError(f"{square!r} is not a square.")

        def work(gantry):
            st = self._usable(gantry.status(), homed=True)
            x, y = self.gd.BoardMap(self.config).square_mm(square)
            return self._move(gantry, st, x, y, f"going to {square}")
        return self._manual(work)

    def return_home(self) -> dict:
        """Back to the home spot (the corner, or home_spot_mm away from it) in one straight line."""
        def work(gantry):
            st = self._usable(gantry.status(), homed=True)
            x, y = self.gd.home_xy(self.config)
            return self._move(gantry, st, x, y, "returning home")
        return self._manual(work)

    def set_home_here(self) -> dict:
        """The person has put the carriage on its home spot: $HM, and G92 to the corner + home_spot_mm when set."""
        def work(gantry):
            st = gantry.status()
            if st["magnet"]:
                gantry.command("M5")
            state = self.gd.set_home_here(gantry, self.config)
            self.manual_home_ready = True
            self.cancelled.clear()
            self.restore_text = ""
            start = self.config.get("start_mm")
            if start and not self.config.get("manual_home_on_connect", False):
                st = self._usable(gantry.status(), homed=True)
                report = self._move(gantry, st, float(start[0]), float(start[1]), "going to the start")
                return dict(report, went_to_start=True, from_corner=self.from_corner(report))
            return self._report(state)
        return self._manual(work)

    def from_corner(self, st: dict) -> dict:
        """How far the carriage is from the home corner: mm along X (towards the h-file) and along Y (away from the
        corner), and the same in motor steps (steps per mm = full steps per turn x microsteps / mm per turn)."""
        flip = self.gd.y_flip_mm(self.config)
        dx = float(st["x"])
        dy = (flip - float(st["y"])) if flip is not None else float(st["y"])
        turn = self.config.get("mm_per_turn", [41.0, 41.0])
        turn = turn if isinstance(turn, (list, tuple)) else [turn, turn]
        per_turn = float(self.config.get("full_steps_per_turn", 200)) * float(self.config.get("microsteps", 1))
        return {"mm": [round(dx, 2), round(dy, 2)],
                "steps": [round(dx * per_turn / float(turn[0])), round(dy * per_turn / float(turn[1]))]}

    def save_home_spot_here(self) -> dict:
        """Homed in the true corner and jogged to where the person will home from now on: that offset becomes
        home_spot_mm in gantry_config.json. The count stays as it is (it is still measured from the corner)."""
        def work(gantry):
            st = self._usable(gantry.status(), homed=True)
            spot = self.gd.home_spot_from_here(st, self.config)
            self._save({"home_spot_mm": spot})
            return dict(self._report(st), saved={"home_spot_mm": spot})
        return self._manual(work)

    def set_start_here(self, square: str = "a8") -> dict:
        """Where the magnet is now becomes the starting position (start_mm; Center gantry and the end of every job go there)."""
        def work(gantry):
            st = self._usable(gantry.status(), homed=True)
            start = [round(st["x"], 2), round(st["y"], 2)]
            self._save({"start_mm": start, "start_square": square})
            return dict(self._report(st), saved={"start_mm": start}, from_corner=self.from_corner(st))
        return self._manual(work)

    def motors_off(self) -> dict:
        """M18: the carriage can be pushed by hand (the position file stops trusting the count)."""
        def work(gantry):
            st = gantry.status()
            if st["magnet"]:
                gantry.command("M5")
            gantry.command("M18")
            if self.config.get("manual_home_on_connect", False):
                self.manual_home_ready = False
            return self._report(gantry.status())
        return self._manual(work)

    def arm(self, up: bool) -> dict:
        """The servo arm: up (M7, the magnet against the board) or down (M8, clear of it). The magnet goes off before
        the arm comes down, so nothing is dropped half way. Waits servo_settle_s so the next move never starts with
        the arm still travelling. Works before homing too (the carriage does not move)."""
        def work(gantry):
            st = gantry.status()
            if st["state"] not in ("Idle", "Alarm", "Hold"):
                raise self.gd.GantryError(f"The board is busy ({st['state']}).")
            if st["state"] == "Alarm" and st["reason"] in ("", "not homed"):
                gantry.command("$X")      # locked after a restart: the arm may still move (the carriage does not)
            elif st["state"] in ("Alarm", "Hold"):
                raise self.gd.GantryError(f"The board is stopped ({st['reason'] or st['state']}). Check it, put the "
                                          "carriage on its home spot and tap Set home here.")
            if not up and st["magnet"]:
                gantry.command("M5")
            gantry.command("M7" if up else "M8")
            settle = float(self.config.get("servo_settle_s", 3.0)) if self.config.get("servo", True) else 0.0
            if settle:
                gantry.command(f"G4 P{settle:.3f}")
                gantry.wait_idle(settle + 5, "the arm going up" if up else "the arm going down")
            return self._report(gantry.status())
        return self._manual(work)

    # test moves (the Gantry screen's "Test move"): plan one move, trace its path with the empty magnet, or run it

    def _test_job(self, uci: str, occupied=None) -> tuple[dict, list[dict]]:
        """The one-move job and its routed steps. occupied: None (an otherwise empty board: only the moving piece),
        "start" (the standard starting position, so pieces are in the way), or an iterable of squares."""
        uci = str(uci).strip().lower()
        squares = (uci[:2], uci[2:4])
        if len(uci) != 4 or any(len(s) != 2 or s[0] not in "abcdefgh" or s[1] not in "12345678" for s in squares):
            raise GantryError(f"{uci!r} is not a move: pick two squares, like e2 then e4.")
        if squares[0] == squares[1]:
            raise GantryError("Pick two different squares.")
        if occupied == "start":
            job = self.gd.simple_move_job(uci)                              # the starting position
        else:
            job = self.gd.simple_move_job(uci, occupied=set(occupied or ()) | {squares[0]})
        try:
            steps = self.gd.route(job, self.gd.BoardMap(self.config), self.config)
        except self.gd.GantryError as exc:
            raise GantryError(f"No way for {squares[0]} → {squares[1]}: {exc}") from None
        except Exception as exc:   # board_paths.NoPath and friends
            raise GantryError(f"No way for {squares[0]} → {squares[1]}: {exc}") from None
        return job, steps

    def plan_test_move(self, uci: str, occupied=None) -> dict:
        """The path one move would take, without the gantry (pure planning, no link):
        {uci, path_units: every carry's points in square units (a1's centre 0,0) one after the other, paths: one
        list per carry, carries, notes}. Raises GantryError in plain words (not a square, no way through)."""
        job, steps = self._test_job(uci, occupied)
        carries = [step for step in steps if step.get("op") == "carry"]
        if not carries:
            raise GantryError(f"Nothing to carry for {uci}.")
        paths = [[[float(px), float(py)] for px, py in step["path"]] for step in carries]
        notes = [self.gd.describe_step(step) for step in carries]
        notes += [step.get("message", "") for step in steps if step.get("op") == "note"]
        return {"uci": job["label"], "path_units": [p for path in paths for p in path], "paths": paths,
                "reasons": [step.get("reason", "move") for step in carries], "carries": len(carries), "notes": notes}

    def test_move(self, uci: str, carry: bool, occupied=None, say=None) -> dict:
        """Homed only. carry=False (Trace path): the empty magnet (off, arm down) visits every point of the planned
        path at the travel speed; nothing is picked up and it stays on the target square. carry=True (Run move): the
        piece really is carried, exactly as in a game (the driver's run_job on the open link: arm up, magnet on,
        carry, release, arm down, then park at start_mm when that is saved). Ends with the magnet off and the arm
        down, also on any fault. Returns the status report plus uci and carry."""
        job, steps = self._test_job(uci, occupied)

        def work(gantry):
            st = self._usable(gantry.status(), homed=True)
            if carry:
                self.gd.run_job(job, self.config, self.link, log, progress=say)
                report = self._report(gantry.status())
            else:
                board = self.gd.BoardMap(self.config)
                carries = [step for step in steps if step.get("op") == "carry"]
                for step in carries:     # checked first: nothing moves when a point is out of reach
                    self.gd.checked_points(step, board)
                report = self._report(st)
                for step in carries:
                    for px, py in step["path"]:
                        x, y = board.point_mm(float(px), float(py))
                        report = self._move(gantry, report, x, y, f"tracing {step['from']} → {step['to']}")
            return dict(report, uci=job["label"], carry=bool(carry))
        return self._manual(work)

    def square_size_mm(self) -> tuple[float, float]:
        """One square along X (files) and along Y (ranks), mm: the jog pad's "1 square"."""
        board = self.gd.BoardMap(self.config)
        return board.square, board.square_y

    def manual_command(self, value: dict) -> dict:
        """Explicit maintenance commands. Never called by camera/game move handling."""
        if not valid_manual(value):
            raise GantryError("Invalid manual gantry command.")
        op = value["op"]
        if op == "disconnect":
            self.close()
            return {"connected": False, "homed": False, "state": "Disconnected", "response": "Disconnected"}
        if op in ("connect", "status"):
            return dict(self.status(), connected=True)
        if op == "home":
            return self.set_home_here()
        if op == "motors_off":
            return self.motors_off()
        if op in ("arm_up", "arm_down"):
            return self.arm(op == "arm_up")
        if op == "jog":
            sx, sy = self.square_size_mm() if value["step"] == "square" else (value["step"], value["step"])
            return self.jog(value["dx"] * sx, value["dy"] * sy)
        if op == "return_home":
            return self.return_home()
        if op == "start":
            return self.center()
        if op == "save_start":
            return self.set_start_here()
        if op in ("trace", "carry"):
            src, dst = value["uci"][:2], value["uci"][2:]
            if src not in self.layout.pieces or dst in self.layout.pieces:
                raise GantryError("Manual carry needs a piece on FROM and an empty TO square.")
            result = self.test_move(value["uci"], op == "carry", occupied=set(self.layout.pieces))
            if op == "carry":
                self.layout.pieces[dst] = self.layout.pieces.pop(src)
            return result

        def work(gantry):
            if op == "settings":
                self._save({key: setting for key, setting in value.items() if key != "op"})
                gantry.set_motion(self.config)
                return dict(self._report(gantry.status()), response="Motion settings saved.")
            if op == "goto":
                st = self._usable(gantry.status(), homed=True)
                x, y = self.gd.BoardMap(self.config).place_mm(value["place"])
                return self._move(gantry, st, x, y, "manual Go to")
            if op == "forget":
                self.manual_home_ready = False
                return dict(self._report(gantry.status()), response="Position forgotten. Set home beneath e2 again.")
            line = {"unlock": "$X", "bump_home": "$H", "magnet_on": "M3", "magnet_off": "M5"}.get(op, value.get("line", ""))
            if line == "?":
                return self._report(gantry.status())
            if line in ("!", "~"):
                self.link.write(line)
                self.manual_home_ready = False
                return self._report(gantry.status())
            moving = line.startswith(("G0", "G1", "G2", "G3")) and not line.startswith("G92")
            if moving:
                self._usable(gantry.status(), homed=True)
            if line.startswith(("$H", "G92", "$100", "$101", "$102", "$RST", "M18")):
                self.manual_home_ready = False
            notes = []
            original_note = gantry.note
            def capture_note(text):
                notes.append(text)
                original_note(text)
            gantry.note = capture_note
            try:
                gantry.command(line)
                if moving or line.startswith(("$H", "G4")):
                    gantry.wait_idle(120, "manual command")
            finally:
                gantry.note = original_note
            return dict(self._report(gantry.status()), response="\n".join(notes) or f"Sent {line}")
        return self._manual(work)

    # manual control, inside

    def _manual_gantry(self):
        """The open link, configured once for the manual controls: Y mirrored for home beside a8, steps/mm and travel
        sent, the board geometry ($B) for the duen protocol, and a restarted board put back at its remembered spot.

        Opening costs about 1.5 s on the real board (with keep_esp32_running the ESP32 does not restart: the link
        waits for its first status line, then Gantry listens a second for anything it printed, then three settings
        go out); without keep_esp32_running it also restarts the ESP32 (2 s, and the count is lost). So the link
        stays open across presses until close()."""
        self._open_link()   # no check_safe here: a board in alarm or hold must still reach Set home here
        if self.manual is None or self.manual.link is not self.link:
            gantry = self.gd.Gantry(self.link, log)
            self.link.flip_y = self.gd.y_flip_mm(self.config)
            self.link.swap_xy = self.gd.swap_xy_flag(self.config)
            gantry.set_motion(self.config)
            if not self.gd.uses_gcode(self.config):
                gantry.set_step_pulse(self.config)
                gantry.set_board(self.config)
            st = gantry.status()
            if self.config.get("manual_home_on_connect", False):
                self.restore_text = "Place the magnet beneath e2 and tap Set home here."
            elif st["homed"] and st["state"] != "Alarm":
                self.restore_text = ""
            else:
                _outcome, self.restore_text = gantry.restore_position(self.config)
            self.manual = gantry
        return self.manual

    def _manual(self, work) -> dict:
        try:
            gantry = self._manual_gantry()
            return work(gantry)
        except self.gd.GantryError as exc:
            self._make_safe()
            raise GantryError(str(exc)) from None
        except GantryError:
            self._make_safe()
            raise
        except Exception as exc:
            self._make_safe()
            self.close()
            raise GantryError(f"The gantry stopped: {exc}") from None

    def _usable(self, st: dict, homed: bool) -> dict:
        """Refuse while the board wants a person to look first; with homed=True also while it is not homed."""
        if homed and self.config.get("manual_home_on_connect", False) and not self.manual_home_ready:
            raise self.gd.GantryError("Set home beneath e2 on the Gantry screen before moving.")
        if st["host_lost"]:
            raise self.gd.GantryError("The board lost contact with the Pi and made itself safe. Check the carriage, "
                                      "put it on its home spot and tap Set home here.")
        if st["state"] == "Hold":
            raise self.gd.GantryError("The board is stopped (hold). Put the carriage on its home spot and tap Set home here.")
        if st["state"] == "Alarm" and st["reason"] not in ("", "not homed"):
            raise self.gd.GantryError(f"The board is in alarm ({st['reason']}). Check it, put the carriage on its home "
                                      "spot and tap Set home here.")
        if homed and not st["homed"]:
            raise self.gd.GantryError("Not homed. Put the carriage on its home spot and tap Set home here.")
        if st["state"] not in ("Idle", "Alarm"):
            raise self.gd.GantryError(f"The board is busy ({st['state']}).")
        return st

    def _away_words(self) -> str:
        return "right ▶ or down ▼" if self.gd.y_flip_mm(self.config) is not None else "right ▶ or up ▲"

    ARM_UP_JOG_MM_S = 20.0      # under the firmware's carry speed ($111: 40 at boot, 25 from the console)

    def _move(self, gantry, st: dict, x: float, y: float, what: str, check: bool = True,
              speed: float | None = None, keep_arm: bool = False) -> dict:
        """The empty magnet (magnet off, arm down) to x, y at the travel speed; waits until it is there.
        keep_arm (the jog pad): an arm that is up stays up, and the move runs at ARM_UP_JOG_MM_S, because the
        firmware refuses anything faster than a carry with the arm up (the magnet is at the board)."""
        if check:
            self.gd.BoardMap(self.config).check(x, y, what)
        if st["magnet"]:
            gantry.command("M5")
        arm_up = st.get("servo") == "up"
        if arm_up and keep_arm:
            limit = min(self.ARM_UP_JOG_MM_S, float(self.config.get("carry_speed_mm_s", 40.0)))
            speed = min(float(speed if speed is not None else self.config.get("travel_speed_mm_s", 60.0)), limit)
        elif arm_up:
            gantry.command("M8")
            settle = float(self.config.get("servo_settle_s", 3.0)) if self.config.get("servo", True) else 0.0
            if settle:
                gantry.command(f"G4 P{settle:.3f}")
                gantry.wait_idle(settle + 5, "the arm going down")
        if st["state"] == "Alarm":       # "not homed" after a restart: $X lets it jog away from the corner
            gantry.command("$X")
        speed = float(speed if speed is not None else self.config.get("travel_speed_mm_s", 60.0))
        distance = ((x - st["x"]) ** 2 + (y - st["y"]) ** 2) ** 0.5
        if distance >= 0.01:
            gantry.command(f"G1 X{x:.3f} Y{y:.3f} F{speed * 60:.0f}")
            gantry.wait_idle(10 + 3 * distance / max(speed, 1.0), what, expected=distance / max(speed, 1.0))
        return self._report(gantry.status())

    def _report(self, st: dict) -> dict:
        memory = getattr(self.link, "memory", None)
        if memory is not None:
            described = memory.describe()
        else:
            described = {"remembered": False, "text": "pretend board: the position is not saved"}
        try:
            units = self.gd.BoardMap(self.config).units(st["x"], st["y"]) if st["homed"] else None
        except Exception:
            units = None
        homed = bool(st["homed"]) and (not self.config.get("manual_home_on_connect", False) or self.manual_home_ready)
        return {"x": st["x"], "y": st["y"], "homed": homed, "state": st["state"], "reason": st["reason"],
                "magnet": bool(st["magnet"]), "servo": st.get("servo"), "remembered": bool(described.get("remembered")),
                "memory": described.get("text", ""), "restore": self.restore_text, "units": units}

    def _save(self, values: dict) -> None:
        """Into gantry_config.json (the old file kept once as gantry_config.json.before-touch) and into the config in
        use. The pretend board keeps it in memory only."""
        if self.save_to_file:
            try:
                self.gd.update_config_file(self.config_path, values)
            except (OSError, ValueError) as exc:
                raise self.gd.GantryError(f"Could not save {self.config_path}: {exc}") from None
        self.config.update(values)

    # ------------------------------------------------------------------ inside

    def _run(self, jobs: list[dict], say=None) -> None:
        if self.cancelled.is_set():
            raise GantryError("Stopped. Check the board and set home at e2 again before moving.")
        self.connect()
        if self.config.get("manual_home_on_connect", False) and not self.manual_home_ready:
            raise GantryError("Set home beneath e2 on the Gantry screen before starting a game.")
        if self.config.get("manual_home_on_connect", False):
            state = self._manual_gantry().status()
            if not state["homed"] or state["state"] != "Idle" or state["host_lost"]:
                self.manual_home_ready = False
                raise GantryError("The gantry lost its position. Check the board and set home beneath e2 again.")
        try:
            self.gd.run_jobs(jobs, self.config, self.link, log, progress=say)
        except self.gd.GantryError as exc:
            self._make_safe()
            raise GantryError(str(exc)) from None
        except Exception as exc:
            self._make_safe()
            self.close()
            raise GantryError(f"The gantry stopped: {exc}") from None

    def _make_safe(self) -> None:
        """Best effort, on top of what the driver already did: magnet off, arm down."""
        if self.link is None:
            return
        for text in ("M5", "M8"):
            try:
                self.link.write(text)
            except Exception:
                return
