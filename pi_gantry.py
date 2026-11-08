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
        except self.gd.GantryError as exc:
            raise GantryError(str(exc)) from None
        except Exception as exc:
            raise GantryError(f"Could not open the gantry on {self.port}: {exc}") from None

    def close(self) -> None:
        self.manual = None
        if self.link is not None:
            try:
                self.link.close()
            finally:
                self.link = None

    def unlock(self) -> None:
        """After a person has checked the pieces: clear the board's alarm or hold and home it again."""
        self.connect()
        try:
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
                return self._move(gantry, st, st["x"] + dx, st["y"] + dy, "jog", check=False, speed=speed)
            travel = self.gd.BoardMap(self.config).travel
            tx = min(max(st["x"] + dx, 0.0), travel[0])
            ty = min(max(st["y"] + dy, 0.0), travel[1])
            if abs(tx - st["x"]) < 0.01 and abs(ty - st["y"]) < 0.01:
                raise self.gd.GantryError("The carriage is already at the edge of its travel that way.")
            return self._move(gantry, st, tx, ty, "jog", check=True)
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
            self.restore_text = ""
            return self._report(state)
        return self._manual(work)

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
            return dict(self._report(st), saved={"start_mm": start})
        return self._manual(work)

    def motors_off(self) -> dict:
        """M18: the carriage can be pushed by hand (the position file stops trusting the count)."""
        def work(gantry):
            st = gantry.status()
            if st["magnet"]:
                gantry.command("M5")
            gantry.command("M18")
            return self._report(gantry.status())
        return self._manual(work)

    def square_size_mm(self) -> tuple[float, float]:
        """One square along X (files) and along Y (ranks), mm: the jog pad's "1 square"."""
        board = self.gd.BoardMap(self.config)
        return board.square, board.square_y

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
            gantry.set_motion(self.config)
            if not self.gd.uses_gcode(self.config):
                gantry.set_step_pulse(self.config)
                gantry.set_board(self.config)
            st = gantry.status()
            if st["homed"] and st["state"] != "Alarm":
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

    def _move(self, gantry, st: dict, x: float, y: float, what: str, check: bool = True,
              speed: float | None = None) -> dict:
        """The empty magnet (magnet off, arm down) to x, y at the travel speed; waits until it is there."""
        if check:
            self.gd.BoardMap(self.config).check(x, y, what)
        if st["magnet"]:
            gantry.command("M5")
        if st.get("servo") == "up":
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
        return {"x": st["x"], "y": st["y"], "homed": bool(st["homed"]), "state": st["state"], "reason": st["reason"],
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
        self.connect()
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
