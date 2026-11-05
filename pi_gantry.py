"""The gantry for the touch-screen app: the computer's moves played by the board itself.

The app (pi_touch.py) keeps playing exactly as before; when a gantry is configured it asks this
module to carry the computer's pieces instead of asking the person to, and still verifies the
board with the camera afterwards (expected_squares() says what it should see).

    player = GantryPlayer.from_env()          # None when no gantry is configured
    player.new_game()                          # the pieces are in the starting position
    player.human_moved(board, move)            # the person moved by hand: follow the pieces
    notes = player.play(board, move, say)      # the gantry plays: returns what the person must still do
    player.reset(say)                          # everything back to its home square
    player.expected_squares()                  # the squares the camera should see occupied

Configuration, from the environment:
    CHESS_GANTRY_PORT    serial device of the ESP32 (e.g. /dev/ttyUSB0), or "sim" for the pretend
                         board in gantry/firmware/host. Unset: no gantry, from_env() returns None.
    CHESS_GANTRY_DIR     the gantry/pi folder of the Self-Moving-Chess-Board-Code repo (branch
                         gantry). Default: the first of ~/chess-gantry/gantry/pi and
                         ~/Self-Moving-Chess-Board-Code/gantry/pi that exists.
    CHESS_GANTRY_CONFIG  gantry_config.json; default <dir>/gantry_config.json.

The planning (carry_plan.py: captures to the dock first, castling as two carries, promotion from
a docked piece) and the paths with real piece widths (board_paths.py) live in that folder with
the driver; this file only wires them to the app. Every fault comes back as GantryError whose
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

    def play(self, board_before: chess.Board, move: chess.Move, say=None) -> list[str]:
        """Carry the pieces for `move`. say(text) hears about each carry; returns the notes left for the person."""
        trial = self.layout.copy()
        try:
            job = self.cp.job_for_move(trial, board_before, move)
        except (ValueError, KeyError) as exc:
            raise GantryError(f"The gantry cannot play {move.uci()}: {exc}") from None
        self._run([job], say)
        self.layout = trial
        return [step["message"] for step in job["steps"] if step["op"] == "note"]

    def reset(self, say=None) -> list[str]:
        """Carry every piece back to its home square; returns the pieces the person has to put back by hand."""
        trial = self.layout.copy()
        job = self.cp.job_for_reset(trial)
        try:
            jobs = self.gd.split_job(job, self.gd.BoardMap(self.config), self.config)
        except self.gd.GantryError as exc:
            raise GantryError(str(exc)) from None
        self._run(jobs, say)
        self.layout = trial
        return [step["message"] for step in job["steps"] if step["op"] == "note"]

    def expected_squares(self) -> set[str]:
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
