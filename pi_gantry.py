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

Configuration, from the environment:
    CHESS_GANTRY_PORT    serial device of the ESP32 (e.g. /dev/ttyUSB0), or "sim" for the pretend
                         board in gantry/firmware/host. Unset: no gantry, from_env() returns None.
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

import logging
import os
from pathlib import Path
import sys

import chess

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
        self.layout = self.cp.Layout()

    # ------------------------------------------------------------------ set-up

    @classmethod
    def from_env(cls) -> "GantryPlayer | None":
        """The gantry the environment describes, or None when there is none. Never raises."""
        try:
            port = (os.environ.get("CHESS_GANTRY_PORT") or "").strip()
            if not port:
                return None
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
        try:
            self.link = self.gd.Link(self.port, int(self.config.get("baud", 115200)), log)
        except self.gd.GantryError as exc:
            raise GantryError(str(exc)) from None
        except Exception as exc:
            raise GantryError(f"Could not open the gantry on {self.port}: {exc}") from None
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

    def close(self) -> None:
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
