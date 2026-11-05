"""Touch screen front end for the camera verified Raspberry Pi chess game."""

from __future__ import annotations

import argparse
import base64
import json
import queue
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

import chess
import cv2
import numpy as np

from board_vision import (Observation, VisionError, board_corners, calibrate, load_calibration,
                          observe, read_image, save_calibration)
from chess_game import ChessGame, DIFFICULTIES, StockfishPlayer
from move_observation import occupied_squares
from pi_camera import Camera, CameraSettings
from pi_online import OnlineError, OnlineSession, OnlineSettings, describe_online, starting_position_problem

try:   # The gantry carries Stockfish's moves in the in-person game. Without pi_gantry (or a gantry) the person moves them.
    from pi_gantry import GantryError, GantryPlayer
except ImportError:   # pragma: no cover - depends on the Pi's checkout
    GantryPlayer = None

    class GantryError(Exception):
        """str(exc) is plain words for the screen."""


class MoveRejected(ValueError):
    """The physical board cannot safely advance to the next position."""


@dataclass(frozen=True)
class ScanFeedback:
    title: str
    detail: str
    source: str | None = None
    destination: str | None = None


MAX_UNCERTAIN_SQUARES = 2


@dataclass(frozen=True)
class PhotoAdjustments:
    """Edits applied equally to the empty, setup, and move photos."""

    brightness: int = 0
    contrast: int = 100
    warmth: int = 0

    def __post_init__(self) -> None:
        if self.brightness not in range(-60, 61, 10):
            raise ValueError("Photo brightness must be between -60 and 60 in steps of 10.")
        if self.contrast not in range(70, 161, 10):
            raise ValueError("Photo contrast must be between 70 and 160 percent in steps of 10.")
        if self.warmth not in range(-40, 41, 10):
            raise ValueError("Photo warmth must be between -40 and 40 in steps of 10.")


def adjust_photo(photo: np.ndarray, settings: PhotoAdjustments) -> np.ndarray:
    result = (photo.astype(np.float32) - 127.5) * (settings.contrast / 100)
    result += 127.5 + settings.brightness
    result[:, :, 0] -= settings.warmth
    result[:, :, 2] += settings.warmth
    return np.clip(result, 0, 255).astype(np.uint8)


def read_photo_adjustments(path: Path) -> PhotoAdjustments | None:
    try:
        return PhotoAdjustments(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def write_photo_adjustments(path: Path, settings: PhotoAdjustments) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2) + "\n")


def reusable_corners(photo: np.ndarray,
                     corners: list[list[float]] | None) -> list[list[float]] | None:
    """Use stored photo coordinates only when they fit the new frame."""
    if corners is None:
        return None
    points = np.asarray(corners, dtype=np.float32)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        return None
    height, width = photo.shape[:2]
    if (np.any(points[:, 0] < 0) or np.any(points[:, 0] >= width) or
            np.any(points[:, 1] < 0) or np.any(points[:, 1] >= height) or
            not cv2.isContourConvex(points) or cv2.contourArea(points) < 10000):
        return None
    return points.tolist()


def saved_scan_corners(directory: Path, calibration) -> list[list[float]] | None:
    if calibration.corners is not None:
        return [point.copy() for point in calibration.corners]
    try:
        return board_corners(read_image(directory / "starting.jpg")).tolist()
    except VisionError:
        return None


def read_camera_settings(path: Path) -> CameraSettings | None:
    try:
        return CameraSettings(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def write_camera_settings(path: Path, settings: CameraSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2) + "\n")


def _as_observation(value: Observation | frozenset[str]) -> Observation:
    return value if isinstance(value, Observation) else Observation(frozenset(value), (), {})


def _checked_observation(value: Observation | frozenset[str]) -> Observation:
    observation = _as_observation(value)
    if len(observation.uncertain) > MAX_UNCERTAIN_SQUARES:
        raise MoveRejected("Too many unclear squares: " + ", ".join(observation.uncertain) +
                           ". Retake the photo or adjust the corners.")
    return observation


def _matches_known_squares(expected: frozenset[str], observation: Observation) -> bool:
    unclear = set(observation.uncertain)
    return expected - unclear == observation.occupied - unclear


def piece_words(symbol) -> str:
    """"white bishop" for "B", "black pawn" for "p"; anything else comes back as it is."""
    try:
        piece = chess.Piece.from_symbol(symbol)
    except (ValueError, AttributeError):
        return str(symbol)
    return ("white " if piece.color else "black ") + chess.piece_name(piece.piece_type)


def _after_move(board: chess.Board, move: chess.Move) -> tuple[frozenset[str], set[str]]:
    before = occupied_squares(board)
    after_board = board.copy(stack=False)
    after_board.push(move)
    after = occupied_squares(after_board)
    affected = set(before ^ after)
    affected.update((chess.square_name(move.from_square), chess.square_name(move.to_square)))
    return after, affected


def rejected_move_feedback(board: chess.Board, observation: Observation,
                           reason: str) -> ScanFeedback:
    """Identify a piece only when occupancy gives one clear origin and destination."""
    if "No move detected" in reason:
        return ScanFeedback("NO CLEAR MOVE", "The camera found no clear change in occupied squares. Check the board and rescan.")
    if len(observation.uncertain) > MAX_UNCERTAIN_SQUARES:
        return ScanFeedback("PHOTO UNCLEAR", "Several squares are unclear. Check the photo and its corners.")
    unclear = set(observation.uncertain)
    before = occupied_squares(board)
    vacated = sorted(before - observation.occupied - unclear)
    filled = sorted(observation.occupied - before - unclear)
    if len(vacated) == 1 and len(filled) == 1:
        source, destination = vacated[0], filled[0]
        piece = board.piece_at(chess.parse_square(source))
        if piece is not None:
            legal_from_to = any(
                move.from_square == chess.parse_square(source) and
                move.to_square == chess.parse_square(destination)
                for move in board.legal_moves)
            if not legal_from_to:
                color = "White" if piece.color else "Black"
                name = chess.piece_name(piece.piece_type)
                turn = "White" if board.turn else "Black"
                detail = (f"Camera sees {color} {name}: {source} → {destination}. "
                          f"It is {turn}'s turn." if piece.color != board.turn else
                          f"Camera sees {color} {name}: {source} → {destination}. "
                          "That move is not legal in this position.")
                return ScanFeedback("ILLEGAL MOVE", detail, source, destination)
    return ScanFeedback("MOVE NOT VERIFIED", reason + " Check the board or adjust the photo corners.")


class CalibrationFlow:
    """Touch calibration steps; photos are processed off the Tk main thread."""

    CORNER_NAMES = ("top-left", "top-right", "bottom-right", "bottom-left")

    def __init__(self) -> None:
        self.stage = "empty"
        self.empty_photo: np.ndarray | None = None
        self.starting_photo: np.ndarray | None = None
        self.empty_corners: list[list[float]] = []
        self.starting_corners: list[list[float]] = []
        self.empty_auto_corners = False
        self.starting_auto_corners = False

    def accept_empty(self, photo: np.ndarray,
                     detected_corners: list[list[float]] | None) -> None:
        if self.stage != "empty":
            raise ValueError("The empty board was already photographed.")
        self.empty_photo = photo
        self.empty_corners = detected_corners or []
        self.empty_auto_corners = detected_corners is not None
        self.stage = "starting" if self.empty_auto_corners else "empty_corners"

    def accept_starting(self, photo: np.ndarray,
                        detected_corners: list[list[float]] | None,
                        *, reused_corners: bool = False) -> None:
        if self.stage != "starting":
            raise ValueError("The starting board is not waiting for a photo.")
        self.starting_photo = photo
        self.starting_corners = detected_corners or []
        self.starting_auto_corners = detected_corners is not None and not reused_corners
        self.stage = "ready" if detected_corners is not None else "starting_corners"

    def add_corner(self, x: float, y: float) -> None:
        if self.stage not in ("empty_corners", "starting_corners"):
            raise ValueError("Board corners are not being selected.")
        points = self.empty_corners if self.stage == "empty_corners" else self.starting_corners
        if len(points) == 4:
            raise ValueError("Four board corners have already been selected.")
        points.append([x, y])
        if len(points) == 4:
            self.stage = "starting" if points is self.empty_corners else "ready"

    def undo_corner(self) -> None:
        if self.stage in ("empty_corners", "starting") and not self.empty_auto_corners:
            if self.empty_corners:
                self.empty_corners.pop()
                self.stage = "empty_corners"
        elif self.stage in ("starting_corners", "ready") and not self.starting_auto_corners:
            if self.starting_corners:
                self.starting_corners.pop()
                self.stage = "starting_corners"

    def reset_empty_corners(self) -> None:
        if not self.empty_auto_corners and self.empty_photo is not None:
            self.empty_corners = []
            self.starting_photo = None
            self.starting_corners = []
            self.stage = "empty_corners"

    def reset_starting_corners(self) -> None:
        if not self.starting_auto_corners and self.starting_photo is not None:
            self.starting_corners = []
            self.stage = "starting_corners"

    def retake_starting(self) -> None:
        self.starting_photo = None
        self.starting_corners = []
        self.starting_auto_corners = False
        self.stage = "starting"


def calibrate_photos(empty: np.ndarray, starting: np.ndarray,
                     empty_corners: list[list[float]],
                     starting_corners: list[list[float]],
                     adjustments: PhotoAdjustments = PhotoAdjustments()):
    """Build and verify calibration before writing it to disk."""
    adjusted_starting = adjust_photo(starting, adjustments)
    calibration, reference, _scores = calibrate(
        adjust_photo(empty, adjustments), adjusted_starting, empty_corners,
        starting_corners=starting_corners)
    observation, _aligned = observe(reference, adjusted_starting, calibration, starting_corners)
    if observation.uncertain or observation.occupied != occupied_squares(chess.Board()):
        raise VisionError("Starting board could not be verified. Check the pieces, corners, and lighting.")
    return calibration, reference


class TouchSession:
    """Rules and camera match decisions, independent of the display and hardware.

    With a gantry (`GantryPlayer`, injected) the session also keeps the gantry's picture of the
    board in step: the start position once the camera verified it, the person's move once the
    camera registered it, and Stockfish's move once the gantry carried it and the camera
    verified the result. The gantry never takes a captured piece off the board: when Stockfish
    captures, the session waits in the "remove" phase until the camera has seen the person take
    the piece off (`verify_removal`), and only then does the gantry carry the capturing move.
    Without a gantry, the person moves Black's pieces by hand as before.
    """

    def __init__(self, level: str, gantry=None) -> None:
        self.game = ChessGame()
        self.game.start(level)
        self.phase = "setup"
        self.choices: tuple[chess.Move, ...] = ()
        self.computer_move: chess.Move | None = None
        self.result: str | None = None
        self.setup_uncertain: tuple[str, ...] = ()
        self.gantry = gantry
        self.layout_known = False               # the gantry was told the camera-verified start position
        self.gantry_note: str | None = None     # why the gantry could not carry the current computer move
        self.gantry_notes: tuple[str, ...] = () # what the gantry reported about the move it carried
        # (square, piece symbol) the person must take off before the gantry plays the computer move;
        # set from the announcement until the move is verified, so the by-hand text knows it is gone.
        self.removal: tuple[str, str] | None = None

    def verify_setup(self, observed: Observation | frozenset[str]) -> None:
        if self.phase != "setup":
            raise ValueError("Setup is not waiting for a scan.")
        observation = _checked_observation(observed)
        if not _matches_known_squares(occupied_squares(self.game.board), observation):
            raise MoveRejected("Board does not match the starting position. Correct the pieces and scan again.")
        self.setup_uncertain = observation.uncertain
        self.phase = "setup_confirm" if observation.uncertain else "human"
        if self.gantry is not None:
            self.gantry.new_game(self.game.board.copy(stack=False))
            self.layout_known = True

    def confirm_setup(self) -> None:
        if self.phase != "setup_confirm":
            raise ValueError("There are no setup squares to confirm.")
        self.setup_uncertain = ()
        self.phase = "human"

    def retry_setup(self) -> None:
        if self.phase != "setup_confirm":
            raise ValueError("Setup is not waiting for confirmation.")
        self.setup_uncertain = ()
        self.phase = "setup"

    def scan_human(self, observed: Observation | frozenset[str]) -> str | None:
        if self.phase != "human":
            raise ValueError("It is not your turn to scan a move.")
        observation = _checked_observation(observed)
        before = occupied_squares(self.game.board)
        if _matches_known_squares(before, observation):
            raise MoveRejected("No move detected. Move a white piece, then scan again.")
        candidates = []
        blocked = set()
        for move in self.game.board.legal_moves:
            after, affected = _after_move(self.game.board, move)
            if not _matches_known_squares(after, observation):
                continue
            unclear_affected = set(observation.uncertain) & affected
            if unclear_affected:
                blocked.update(unclear_affected)
            else:
                candidates.append(move)
        if not candidates and blocked:
            raise MoveRejected("Move touches unclear squares: " + ", ".join(sorted(blocked)) +
                               ". Retake the photo or adjust the corners.")
        if not candidates:
            raise MoveRejected("Illegal or unrecognized move. Correct the board and scan again.")
        if len(candidates) > 1:
            self.choices = tuple(candidates)
            self.phase = "choose"
            return None
        return self._accept_human(candidates[0])

    def select_move(self, move: chess.Move) -> str:
        if self.phase != "choose" or move not in self.choices:
            raise ValueError("Select one of the moves shown on screen.")
        self.choices = ()
        return self._accept_human(move)

    def retake(self) -> None:
        if self.phase != "choose":
            raise ValueError("There is no move selection to retake.")
        self.choices = ()
        self.phase = "human"

    def _accept_human(self, move: chess.Move) -> str:
        if self.gantry is not None:
            self.gantry.human_moved(self.game.board.copy(stack=False), move)
        san = self.game.play_human(
            chess.square_name(move.from_square),
            chess.square_name(move.to_square),
            chess.piece_symbol(move.promotion) if move.promotion else None,
        )
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "thinking"
        return san

    def announce_computer(self, move: chess.Move) -> str:
        """Stockfish's move is known. With a gantry that needs a captured piece taken off first, the
        session waits in "remove" for the camera to see it gone; otherwise straight to "computer"."""
        if self.phase != "thinking" or move not in self.game.board.legal_moves:
            raise ValueError("The computer did not provide a legal move.")
        removal = None
        # An older pi_gantry without removal_needed docks captured pieces itself: then nothing to take off.
        removal_needed = getattr(self.gantry, "removal_needed", None) if self.gantry is not None else None
        if removal_needed is not None:
            removal = removal_needed(self.game.board.copy(stack=False), move)
        self.computer_move = move
        self.removal = (str(removal[0]), removal[1]) if removal else None
        self.phase = "remove" if self.removal else "computer"
        return self.game.board.san(move)

    def removal_text(self) -> str:
        """"the white bishop off d5": what the person must take off before the gantry plays."""
        if self.removal is None:
            raise ValueError("No piece is waiting to be taken off the board.")
        square, symbol = self.removal
        return f"the {piece_words(symbol)} off {square}"

    def verify_removal(self, observed: Observation | frozenset[str]) -> str:
        """Check (phase "remove"): the camera must see the captured piece's square empty and every other
        square as it is; the capturing piece still stands on its from-square. Returns the square once it
        is clear and moves on to "computer", where `carry_computer_move` takes over. Raises MoveRejected."""
        if self.phase != "remove" or self.removal is None or self.computer_move is None:
            raise ValueError("No piece is waiting to be taken off the board.")
        observation = _checked_observation(observed)
        square, _symbol = self.removal
        if square in observation.uncertain:
            raise MoveRejected(f"The camera cannot tell whether {square} is empty. Retake the photo or adjust the corners.")
        expected = occupied_squares(self.game.board) - {square}
        if not _matches_known_squares(expected, observation):
            raise MoveRejected(self._removal_mismatch(expected, observation))
        self.phase = "computer"
        return square

    def _removal_mismatch(self, expected: frozenset[str], observation: Observation) -> str:
        square, _symbol = self.removal
        unclear = set(observation.uncertain)
        missing = sorted((expected - observation.occupied) - unclear)
        extra = sorted((observation.occupied - expected) - unclear)
        if extra == [square] and not missing:
            return f"{square} still has a piece on it. Take {self.removal_text()}, then tap Check."
        parts = []
        if missing:
            parts.append("missing on " + ", ".join(missing))
        if extra:
            parts.append("extra on " + ", ".join(extra))
        return ("The board does not match the game: " + "; ".join(parts) +
                f". Put the pieces back, take {self.removal_text()}, then tap Check.")

    def verify_computer(self, observed: Observation | frozenset[str]) -> str:
        if self.phase != "computer" or self.computer_move is None:
            raise ValueError("There is no computer move to verify.")
        observation = _checked_observation(observed)
        after, affected = _after_move(self.game.board, self.computer_move)
        unclear_affected = set(observation.uncertain) & affected
        if unclear_affected:
            raise MoveRejected("Computer move touches unclear squares: " +
                               ", ".join(sorted(unclear_affected)) + ". Retake the photo.")
        if not _matches_known_squares(after, observation):
            if self.gantry is not None:
                raise MoveRejected(self._gantry_mismatch(after, observation))
            raise MoveRejected("Board does not match the computer move. Correct it and scan again.")
        san = self.game.play_computer(self.computer_move)
        if self.gantry is not None and self.gantry_note is not None:
            # The person made the move by hand after the gantry failed: the gantry's picture follows the camera.
            self.gantry.new_game(self.game.board.copy(stack=False))
            self.gantry_note = None
        self.computer_move = None
        self.removal = None
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "human"
        return san

    def _gantry_mismatch(self, expected: frozenset[str], observation: Observation) -> str:
        unclear = set(observation.uncertain)
        parts = []
        missing = sorted((expected - observation.occupied) - unclear)
        extra = sorted((observation.occupied - expected) - unclear)
        if missing:
            parts.append("missing on " + ", ".join(missing))
        if extra:
            parts.append("extra on " + ", ".join(extra))
        move = self.computer_move.uci() if self.computer_move else "the move"
        return (f"The camera does not see the move {move} on the board: " + "; ".join(parts) +
                ". Fix the pieces, then tap Retake Photo.")

    # ------------------------------------------------------------------ the gantry carries Stockfish's move

    def carry_computer_move(self, say=None) -> list[str]:
        """Worker thread: the gantry carries the announced computer move; `say(text)` gets progress lines.

        Only the gantry moves here. The game position advances in `verify_computer`, once the camera
        has seen the move on the board, exactly as after a move made by hand. Raises GantryError.
        """
        if self.phase != "computer" or self.computer_move is None:
            raise ValueError("There is no computer move for the gantry to carry.")
        if self.gantry is None:
            raise ValueError("No gantry is connected.")
        return list(self.gantry.play(self.game.board.copy(stack=False), self.computer_move, say) or ())

    def gantry_moved(self, notes) -> None:
        """The gantry reports the move carried; the camera check comes next."""
        self.gantry_note = None
        self.gantry_notes = tuple(notes or ())

    def gantry_failed(self, message: str) -> None:
        """The gantry could not carry the move: the person makes it by hand, as without a gantry."""
        self.gantry_note = message
        self.gantry_notes = ()

    def put_pieces_back(self, say=None) -> list[str]:
        """Worker thread: every piece goes back to its home square, if the gantry knows where they are."""
        if self.gantry is None or not self.layout_known:
            return []
        return list(self.gantry.reset(say) or ())

    def resign(self) -> None:
        if self.phase != "finished":
            self.result = "You ended the game. Stockfish wins."
            self.phase = "finished"


class TouchApp:
    BG = "#101820"
    PANEL = "#192733"
    TEXT = "#f1f5f7"
    MUTED = "#bdcbd3"
    GREEN = "#77db9c"
    RED = "#ff8c85"
    GOLD = "#ffcf70"

    RESET_CONFIRM_SECONDS = 4.0   # a second tap on Reset Board / End Game within this time confirms it

    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.init_state(args)
        screen_width = 800 if args.windowed else root.winfo_screenwidth()
        screen_height = 480 if args.windowed else root.winfo_screenheight()
        self.compact = screen_width < 900 or screen_height < 600
        self.panel_width = 235 if self.compact else 320

        root.title("Physical Chess")
        root.configure(bg=self.BG)
        if args.windowed:
            root.geometry("800x480")
            root.minsize(800, 480)
        else:
            root.geometry(f"{screen_width}x{screen_height}+0+0")
            root.attributes("-fullscreen", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _event: self.close())

        self.header = tk.Label(root, text="PHYSICAL CHESS", bg=self.BG, fg=self.TEXT,
                               font=("DejaVu Sans", 13 if self.compact else 18, "bold"),
                               anchor="w", padx=12 if self.compact else 18)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.board_canvas = tk.Canvas(root, bg=self.BG, highlightthickness=0)
        self.board_canvas.grid(row=1, column=0, sticky="nsew", padx=(12, 4), pady=8)
        self.board_canvas.bind("<Configure>", lambda _event: self.draw_board())
        self.board_canvas.bind("<Button-1>", self.tap_board)
        self.panel = tk.Frame(root, bg=self.PANEL, width=self.panel_width,
                              padx=8 if self.compact else 16, pady=5 if self.compact else 12)
        self.panel.grid(row=1, column=1, sticky="nsew", padx=(2, 6 if self.compact else 12), pady=5 if self.compact else 8)
        self.panel.grid_propagate(False)
        self.status = tk.Label(root, text=self.message, bg=self.PANEL, fg=self.message_color,
                               font=("DejaVu Sans", 10 if self.compact else 13, "bold"),
                               anchor="w", justify="left", wraplength=screen_width - 36,
                               padx=8 if self.compact else 16, pady=4 if self.compact else 8)
        self.status.grid(row=2, column=0, columnspan=2, sticky="nsew",
                         padx=6 if self.compact else 12, pady=(0, 4 if self.compact else 10))
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)
        root.grid_columnconfigure(1, minsize=self.panel_width + (10 if self.compact else 20))
        self.render()
        if not args.windowed:
            root.after(250, self.ensure_fullscreen)
        root.after(70, self.poll_results)

    def init_state(self, args: argparse.Namespace) -> None:
        """Everything but the widgets, so the game flow can run without a display."""
        self.args = args
        self.camera_settings = ((read_camera_settings(args.calibration / "camera_settings.json")
                                 or CameraSettings()) if args.camera == "pi" else CameraSettings())
        self.camera = Camera(args.camera, args.device, self.camera_settings)
        self.photo_adjustments = (read_photo_adjustments(args.calibration / "photo_adjustments.json")
                                  or PhotoAdjustments())
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.generation = 0
        self.closed = False
        self.player: StockfishPlayer | None = None
        self.calibration = None
        self.reference = None
        self.session: TouchSession | None = None
        self.calibration_flow: CalibrationFlow | None = CalibrationFlow()
        self.startup_calibration = True
        self.scan_photo: np.ndarray | None = None
        self.scan_corners: list[list[float]] = []
        self.last_scan_corners: list[list[float]] | None = None
        self.scan_corner_source = ""
        self.scan_feedback: ScanFeedback | None = None
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_geometry: tuple[float, float, float] | None = None
        self.level = "beginner"
        self.online_session: OnlineSession | None = None   # the fourth menu option: the website plays on this board
        self.busy = False
        self.selected_square: str | None = None
        self.message = "Calibrate the board, or use a saved calibration."
        if (args.calibration / "calibration.json").is_file() and not self.saved_calibration_available():
            self.message = "Saved calibration cannot be reused. Take new calibration photos."
        self.message_color = self.MUTED
        # The gantry (pi_gantry) is opened at the first in-person game and closed when Online is selected,
        # since the website's game server drives it from its own process while Online.
        self.gantry = None
        self.gantry_activity: str | None = None   # heading while the gantry works ("Gantry moving e7e5…")
        self.resetting = False                    # the gantry is carrying every piece home
        self.after_reset = None                   # what happens on the Tk thread once the pieces are home
        self.reset_armed_until = 0.0              # Reset Board / End Game was tapped once; a second tap confirms
        # What the gantry's reset left for the person ("Put a black pawn on e7."): the pieces taken during the
        # game go back by hand, so the notes stay on the Set up the board screen until the camera verified it.
        self.reset_notes: tuple[str, ...] = ()

    def ensure_fullscreen(self) -> None:
        """Repeat the full-screen request after the desktop maps the window."""
        if self.closed:
            return
        self.root.geometry(
            f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0"
        )
        self.root.attributes("-fullscreen", True)

    def say(self, message: str, color: str | None = None) -> None:
        self.message = message
        self.message_color = color or self.MUTED
        self.status.configure(text=message, fg=self.message_color)

    def button_widget(self, parent, label: str, command, *,
                      accent: bool = False, short: bool = False) -> tk.Button:
        return tk.Button(parent, text=label, command=command,
                         height=1 if self.compact or short else 2,
                         font=("DejaVu Sans", 10 if self.compact else 12, "bold"),
                         bd=0, relief="flat",
                         bg=self.GREEN if accent else "#314554",
                         fg=self.BG if accent else self.TEXT,
                         activebackground=self.GOLD, activeforeground=self.BG,
                         wraplength=self.panel_width - 30)

    def button(self, label: str, command, *, accent: bool = False,
               short: bool = False) -> None:
        self.button_widget(self.panel, label, command, accent=accent, short=short).pack(
            fill="x", pady=2 if self.compact else 4,
        )

    def heading(self, title: str, detail: str = "", *, always: bool = False) -> None:
        """On the small screen the detail is normally left to the status bar; `always` shows it there too,
        for what the person has to do with the pieces before the game can go on."""
        tk.Label(self.panel, text=title, bg=self.PANEL, fg=self.TEXT,
                 font=("DejaVu Sans", 12 if self.compact else 16, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 3 if self.compact else 8))
        if detail and (always or not self.compact):
            tk.Label(self.panel, text=detail, bg=self.PANEL, fg=self.MUTED,
                     font=("DejaVu Sans", 10 if self.compact else 11), anchor="w", justify="left",
                     wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 6 if self.compact else 12))

    def render(self) -> None:
        for child in self.panel.winfo_children():
            child.destroy()
        self.draw_board()
        if self.online_session is not None:
            self.render_online()
            return
        if self.busy:
            if self.gantry_activity:
                self.heading(self.gantry_activity, "Keep hands clear of the board.")
            else:
                self.heading("Please wait", "The camera or chess engine is working.")
            if self.resetting:
                self.button("Back to Menu", self.back_to_menu)
                return
            self.button("Cancel Calibration" if self.calibration_flow else "End Game",
                        self.cancel_calibration if self.calibration_flow else self.end_game)
            return
        if self.calibration_flow is not None:
            self.render_calibration()
            return
        if self.scan_photo is not None:
            self.render_scan_review()
            return
        session = self.session
        if session is None:
            self.heading("New game", "Play White on the physical board. Tap a piece on the screen to see legal destinations.")
            for key, difficulty in DIFFICULTIES.items():
                label = ("● " if key == self.level else "○ ") + difficulty.label
                self.button(label, lambda selected=key: self.choose_level(selected), short=True)
            self.button("○ Online", self.go_online, short=True)
            self.button("Start Game", self.start_game, accent=True)
            self.button("Calibrate Board", self.start_calibration, short=True)
        elif session.phase == "setup":
            self.heading("Set up the board", self.setup_instructions(), always=bool(self.reset_notes))
            self.button("Scan Starting Board", self.scan, accent=True)
            self.button("Cancel Game", self.end_game)
        elif session.phase == "setup_confirm":
            squares = ", ".join(session.setup_uncertain)
            self.heading("Check unclear squares", f"Confirm {squares} match the displayed starting board.")
            self.button("Squares Match", self.confirm_setup, accent=True)
            self.button("Retake Photo", self.retry_setup_scan)
            self.button("Cancel Game", self.end_game)
        elif session.phase == "human":
            self.heading("Your move · White", "Move a piece on the physical board, then tap Scan Move. Tap a white piece on screen for legal squares.")
            self.button("Scan Move", self.scan, accent=True)
            self.button(self.reset_label("Reset Board"), self.reset_board)
            self.button("End Game", self.end_game)
        elif session.phase == "choose":
            promotion = all(move.promotion for move in session.choices)
            self.heading("Choose promotion" if promotion else "Choose your move",
                         "The camera sees the same occupied squares for these legal moves.")
            choices_frame = tk.Frame(self.panel, bg=self.PANEL)
            choices_frame.pack(fill="x", pady=(0, 4))
            choices_frame.grid_columnconfigure(0, weight=1)
            choices_frame.grid_columnconfigure(1, weight=1)
            for index, move in enumerate(session.choices):
                name = chess.piece_name(move.promotion).title() if move.promotion else session.game.board.san(move)
                label = name if self.compact else f"{name}  ({move.uci()})"
                choice_button = self.button_widget(
                    choices_frame, label, lambda chosen=move: self.select_move(chosen),
                )
                choice_button.configure(wraplength=(self.panel_width - 35) // 2,
                                        font=("DejaVu Sans", 10, "bold"))
                choice_button.grid(row=index // 2, column=index % 2,
                                   sticky="ew", padx=2, pady=2)
            self.button("Retake Photo", self.retake)
            self.button(self.reset_label("Reset Board"), self.reset_board)
            self.button("End Game", self.end_game)
        elif session.phase == "thinking":
            self.heading("Computer thinking", "Leave the physical board as it is.")
            self.button("Try Computer Move", self.request_computer_move, accent=True)
            self.button(self.reset_label("Reset Board"), self.reset_board)
            self.button("End Game", self.end_game)
        elif session.phase == "remove":
            # Stockfish captures: the person takes the captured piece off, Check has the camera confirm it,
            # and only then does the gantry carry the move. Every tap of Check photographs the board again.
            self.heading("Remove the piece", self.removal_instruction(), always=True)
            self.button("Check", self.scan, accent=True)
            self.button(self.reset_label("Reset Board"), self.reset_board)
            self.button("End Game", self.end_game)
        elif session.phase == "computer":
            move = session.computer_move
            assert move is not None
            san = session.game.board.san(move)
            extra = []
            if session.game.board.is_capture(move) and session.removal is None:
                extra.append("Remove the captured piece.")
            if session.game.board.is_castling(move):
                extra.append("Move the rook too.")
            if move.promotion:
                extra.append(f"Replace the pawn with a {chess.piece_name(move.promotion)}.")
            by_hand = f"Move {move.uci()[:2]} to {move.uci()[2:4]} on the board. " + " ".join(extra)
            if session.gantry_note:
                by_hand = f"The gantry could not make this move: {session.gantry_note} " + by_hand
            self.heading(f"Stockfish: {san}", by_hand)
            self.button("Verify Computer Move", self.scan, accent=True)
            self.button(self.reset_label("Reset Board"), self.reset_board)
            self.button("End Game", self.end_game)
        else:
            self.heading("Game over", session.result or "Game finished.")
            self.button("New Game", self.back_to_menu, accent=True)

    def setup_instructions(self) -> str:
        """The Set up the board screen, with the pieces the gantry's reset could not put back first."""
        notes = " ".join(self.reset_notes)
        return (notes + " " if notes else "") + "Place all 32 pieces in the starting position, then scan."

    def removal_instruction(self) -> str:
        """The Remove the piece screen: "Stockfish plays Bxd5. Take the white bishop off d5, then tap Check." """
        session = self.session
        assert session is not None and session.computer_move is not None
        san = session.game.board.san(session.computer_move)
        return f"Stockfish plays {san}. Take {session.removal_text()}, then tap Check."

    def choose_level(self, level: str) -> None:
        self.level = level
        self.say(f"{DIFFICULTIES[level].label} selected.")
        self.render()

    # ------------------------------------------------------------------ Online: the website plays on this board

    def render_online(self) -> None:
        holder = self.online_session
        assert holder is not None
        if holder.checking:
            self.heading("Online", "Checking the board with the camera…")
            self.button("Back to Menu", self.leave_online)
            return
        if not holder.verified:
            self.heading("Set up the board", holder.problem or "All 32 pieces in the starting position, then tap Check Again.")
            self.button("Check Again", self.check_online_board, accent=True)
            self.button("Back to Menu", self.leave_online)
            return
        view = holder.view
        if holder.error:
            self.heading("Online", holder.error)
        elif view is None:
            self.heading("Online", "Connecting to the game server on this Pi…")
        elif holder.confirming and view.check_job is not None:
            self.heading(view.title, "Checking the board with the camera…")
        elif view.mode == "pairing" and not view.connected:
            self.heading("Online", view.detail + " This board accepts website games only while Online is selected here.")
        else:
            self.heading(view.title, view.detail, always=view.check_job is not None)   # which piece to take off
        if view is not None and not holder.error:
            if view.mode == "fix_setup":
                self.button("Check Again", self.check_online_board, accent=True)
            elif view.mode == "pairing":
                self.button(self.reset_label("Reset Board"), self.reset_online_board)
            elif view.mode == "game":
                if view.check_job is not None:
                    # The website's game waits for a captured piece to be taken off: Check has the server's
                    # camera confirm it. While that runs the buttons wait for the answer.
                    if holder.confirming:
                        self.button("Back to Menu", self.leave_online)
                        return
                    self.button("Check", self.confirm_online_removal, accent=True)
                self.button(self.reset_label("End Game"), self.reset_online_board)
        self.button("Back to Menu", self.leave_online)

    def go_online(self) -> None:
        """The fourth option. The camera must see the starting position first; then the address and
        code are shown until a player connects, and then the game as the website plays it."""
        if self.session is not None or self.busy or self.online_session is not None or self.calibration_flow is not None:
            return
        try:
            settings = OnlineSettings.load()
        except OnlineError as exc:
            self.say(str(exc), self.RED)
            self.render()
            return
        self.generation += 1
        self.selected_square = None
        self.reset_armed_until = 0.0
        self.reset_notes = ()   # the Online check names any missing piece itself
        if self.gantry is not None:
            # The game server drives the gantry from its own process while Online; the next in-person game reopens it.
            gantry, self.gantry = self.gantry, None
            self.executor.submit(gantry.close)
        self.online_session = OnlineSession(settings, self.results, self.generation)
        self.say("Online selected. Checking that the board is set up…", self.GOLD)
        self.check_online_board()

    def reset_online_board(self) -> None:
        """Reset Board (pairing view) / End Game (live game): one confirming tap, then the next heartbeat
        carries `reset`. The server ends any running game as abandoned, puts the pieces back and clears
        the lease; the screen follows the snapshots: "Game over / Putting the pieces back…", then pairing."""
        holder = self.online_session
        if (holder is None or not holder.verified or holder.checking or holder.error or holder.view is None
                or holder.view.mode not in ("pairing", "game")):
            return
        if not self.confirm_reset("End Game" if holder.view.mode == "game" else "Reset Board"):
            return
        holder.request_reset()
        self.say("Resetting the board: the pieces go back to their starting squares.", self.GOLD)
        self.render()

    # ------------------------------------------------------------------ one confirming tap for Reset Board / End Game

    def reset_armed(self) -> bool:
        return self.reset_armed_until > time.monotonic()

    def reset_label(self, label: str) -> str:
        return "Tap Again to Confirm" if self.reset_armed() else label

    def confirm_reset(self, label: str) -> bool:
        """True on the second tap within RESET_CONFIRM_SECONDS; the first tap only arms the button,
        so a brush of the screen does not wipe a game."""
        if self.reset_armed():
            self.reset_armed_until = 0.0
            return True
        self.reset_armed_until = time.monotonic() + self.RESET_CONFIRM_SECONDS
        self.say(f"Tap {label} again to end this game; the pieces then go back to their starting squares."
                 if label == "End Game" else f"Tap {label} again to put every piece back on its starting square.", self.GOLD)
        self.render()
        self.root.after(int(self.RESET_CONFIRM_SECONDS * 1000) + 100, self.disarm_reset)
        return False

    def disarm_reset(self) -> None:
        if self.closed or self.reset_armed() or not self.reset_armed_until:
            return
        self.reset_armed_until = 0.0
        self.render()

    def check_online_board(self) -> None:
        """Photograph the board and compare it with the starting position. The website is let in only on a match."""
        holder = self.online_session
        if holder is None or self.busy:
            return
        if self.calibration is None or self.reference is None:
            holder.problem = "Calibrate the board first: Back to Menu, then Use Saved Calibration or Calibrate Board."
            self.say(holder.problem, self.RED)
            self.render()
            return
        holder.checking, holder.problem = True, None
        self.busy = True
        self.say("Taking a photo of the board…")
        self.render()
        calibration, reference, camera, args = self.calibration, self.reference, self.camera, self.args

        def check():
            path = args.calibration / "captures" / f"online_{uuid4().hex}.jpg"
            camera.capture(path)
            photo = read_image(path)
            if args.corners:
                corners = [point.copy() for point in args.corners]
            else:
                try:
                    corners = board_corners(photo).tolist()
                except VisionError:
                    corners = [point.copy() for point in calibration.corners] if calibration.corners else None
            if corners is None:
                raise VisionError("The board corners were not found in the photo. Check the markers and the lighting.")
            observation, _aligned = observe(reference, photo, calibration, corners)
            return observation

        self.submit("online_scan", check)

    def online_board_checked(self, observation) -> None:
        holder = self.online_session
        if holder is None:
            return
        holder.checking = False
        observation = _as_observation(observation)
        holder.problem = starting_position_problem(observation.occupied, observation.uncertain)
        if holder.problem:
            holder.verified = False   # also after a stopped reset: the setup screen stays until the camera agrees
            self.say(holder.problem, self.RED)
        else:
            holder.start()   # the heartbeat begins (its first beat carries `fresh`): from now on the website may take the board
            self.say("Board verified. Open the website and enter the code to play.", self.GREEN)
        self.render()

    def confirm_online_removal(self) -> None:
        """Check on the Online game screen: the person took the captured piece off. The game server
        photographs the board and, when the square is empty, plays the move; while the piece is still
        there it stays waiting with the reason in `motion.error`. The screen follows the next heartbeat;
        the answer to this request is the same kind of snapshot, so it is shown at once."""
        holder = self.online_session
        if (holder is None or not holder.verified or holder.checking or holder.confirming or holder.error
                or holder.view is None or holder.view.check_job is None):
            return
        job_id = holder.view.check_job
        client = holder.client
        holder.confirming = True
        self.reset_armed_until = 0.0
        self.say("Checking the board with the camera…", self.GOLD)
        self.render()
        self.submit("online_confirm", lambda: client.confirm(job_id))

    def online_confirmed(self, value, error) -> None:
        holder = self.online_session
        if holder is None:
            return
        holder.confirming = False
        if error is not None:
            self.say(str(error), self.RED)
        else:
            self.handle_online(value, None)
            if holder.view is not None and not holder.error:
                # Still waiting after a Check: the piece was still there (the status carries the server's reason).
                self.say(holder.view.status, self.RED if holder.view.check_job is not None else self.GREEN)
        self.render()

    def leave_online(self) -> None:
        holder = self.online_session
        if holder is None:
            return
        self.generation += 1   # results still in flight from the thread or the camera are ignored
        self.busy = False
        self.reset_armed_until = 0.0
        self.online_session = None
        holder.stop()           # its last heartbeat tells the game server Online is off
        self.say("Online mode off. Choose a difficulty to play on the board.")
        self.render()

    def handle_online(self, value, error) -> None:
        """Each heartbeat's answer: redraw the board, and rebuild the panel only when the state changes."""
        holder = self.online_session
        if holder is None:
            return
        if error is not None:
            text = str(error)
            if holder.error != text:
                holder.error, holder.view = text, None
                self.say(text, self.RED)
                self.render()
            return
        view = describe_online(value, holder.settings)
        before, holder.error, holder.view = holder.view, None, view
        if (before is None or before.connected != view.connected or before.title != view.title or before.mode != view.mode
                or before.detail != view.detail):
            if before is not None and before.mode != view.mode:
                self.reset_armed_until = 0.0   # a half-confirmed End Game / Reset Board does not carry over
            self.say(view.status, self.GREEN if view.connected else self.MUTED)
            self.render()
        else:
            if before.status != view.status:
                self.say(view.status, self.GREEN)
            self.draw_board()

    def draw_online(self) -> None:
        holder = self.online_session
        canvas = self.board_canvas
        canvas.delete("all")
        self.preview_geometry = None
        view = holder.view if holder else None
        if view is not None and view.mode != "pairing":
            self._draw_position(view.board, view.last_move, None, set(), ())
            return
        width = canvas.winfo_width() if canvas.winfo_width() > 1 else 400
        height = canvas.winfo_height() if canvas.winfo_height() > 1 else 360
        cx, scale = width / 2, 0.8 if self.compact else 1.0
        if holder is not None and not holder.verified:
            # Until the camera has seen the full starting position, show the board with what is wrong.
            self._draw_position(chess.Board(), None, None, set(), ())
            text = "Checking the board with the camera…" if holder.checking else (holder.problem or "Set up the starting position.")
            canvas.create_rectangle(10, height - 46, width - 10, height - 6, fill=self.PANEL, outline=self.PANEL)
            canvas.create_text(cx, height - 26, text=text, fill=self.GOLD if holder.checking else self.RED, width=width - 30,
                               font=("DejaVu Sans", int(11 * scale), "bold"), justify="center")
            return
        settings = holder.settings if holder else None
        code = settings.code if settings else None
        lines = [
            ("PLAY ONLINE", int(15 * scale), self.MUTED),
            (settings.website_display if settings else "", int(24 * scale), self.GOLD),
            ("then enter this code" if code else "then enter the board's access code", int(12 * scale), self.MUTED),
            (code or "ask the board owner", int(44 * scale) if code else int(16 * scale), self.TEXT if code else self.MUTED),
            (holder.error if holder and holder.error else view.status if view is not None else "Board verified · waiting for a player to connect…",
             int(12 * scale), self.RED if holder and holder.error else self.GREEN),
        ]
        total = sum(size * 2.1 for _, size, _ in lines)
        y = max(10, (height - total) / 2)
        for text, size, color in lines:
            canvas.create_text(cx, y + size, text=text, fill=color, width=width - 20,
                               font=("DejaVu Sans", size, "bold"), justify="center")
            y += size * 2.1

    def render_scan_review(self) -> None:
        if self.scan_feedback is not None:
            feedback = self.scan_feedback
            tk.Label(self.panel, text=feedback.title, bg="#8a2f32", fg=self.TEXT,
                     font=("DejaVu Sans", 15 if self.compact else 20, "bold"),
                     wraplength=self.panel_width - 35, pady=10).pack(fill="x", pady=(0, 10))
            tk.Label(self.panel, text=feedback.detail, bg=self.PANEL, fg=self.TEXT,
                     font=("DejaVu Sans", 11 if self.compact else 13, "bold"),
                     justify="left", wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 12))
            if self.scan_corner_source == "saved":
                tk.Label(self.panel, text="Using saved corners. If the camera moved, adjust them.",
                         bg=self.PANEL, fg=self.GOLD, justify="left",
                         font=("DejaVu Sans", 9 if self.compact else 11),
                         wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 8))
            action = ("Fix Board & Scan Again" if feedback.title == "ILLEGAL MOVE" else
                      "Make Move & Rescan" if feedback.title == "NO CLEAR MOVE" else
                      "Check" if feedback.title == "REMOVE THE PIECE" else "Retake Photo")
            self.button(action, self.retake_scan, accent=True)
            self.button("Adjust Photo Corners", self.redo_scan_corners, short=True)
            self.button("End Game", self.end_game, short=True)
            return
        if len(self.scan_corners) < 4:
            corner = CalibrationFlow.CORNER_NAMES[len(self.scan_corners)]
            self.heading("Mark photo corners", f"Tap the outer {corner} corner in this photo.")
            self.button("Undo Corner", self.undo_scan_corner)
            if self.last_scan_corners is not None:
                self.button("Use Saved Corners", self.use_saved_scan_corners)
        else:
            self.heading("Photo aligned", "Check the board using these four corners.")
            self.button("Check Photo", self.process_scan_photo, accent=True)
            self.button("Redo Corners", self.redo_scan_corners)
        self.button("Retake Photo", self.retake_scan)
        self.button("End Game", self.end_game)

    def render_calibration(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        if flow.stage == "empty":
            self.heading("1. Empty board", "Remove every piece and take one empty-board photo.")
            self.button("Take Empty Photo", self.capture_empty, accent=True)
            if self.saved_calibration_available():
                self.button("Use Saved Calibration", self.use_saved_calibration)
            if not self.startup_calibration:
                self.button("Back to Menu", self.cancel_calibration)
        elif flow.stage == "empty_corners":
            next_corner = flow.CORNER_NAMES[len(flow.empty_corners)]
            self.heading("2. Empty corners", f"Tap the outer {next_corner} corner in the empty-board photo.")
            self.button("Undo Corner", self.undo_corner)
            self.button("Retake Empty Photo", self.retake_empty)
            self.button("Cancel Calibration", self.cancel_calibration)
        elif flow.stage == "starting":
            self.heading("3. Starting pieces", "Place all 32 pieces, with White at the bottom. You can edit this photo after capture.")
            self.button("Take Setup Photo", self.capture_starting, accent=True)
            if not flow.empty_auto_corners:
                self.button("Redo Empty Corners", self.redo_empty_corners)
            self.button("Cancel Calibration", self.cancel_calibration)
        elif flow.stage == "starting_corners":
            next_corner = flow.CORNER_NAMES[len(flow.starting_corners)]
            self.heading("4. Setup corners", f"Tap the outer {next_corner} corner in the setup photo.")
            self.button("Undo Corner", self.undo_corner)
            self.button("Retake Setup Photo", self.retake_starting)
            self.button("Cancel Calibration", self.cancel_calibration)
        elif flow.stage == "ready":
            self.heading("Adjust setup photo", "The same edits will be applied to the empty photo and every game scan.")
            self.render_photo_adjustments()
            self.button("Check Board", self.finish_calibration, accent=True, short=True)
            if not flow.starting_auto_corners:
                self.button("Redo Setup Corners", self.redo_starting_corners, short=True)
            if not flow.empty_auto_corners:
                self.button("Redo Empty Corners", self.redo_empty_corners, short=True)
            self.button("Retake Setup Photo", self.retake_starting, short=True)
            self.button("Cancel Calibration", self.cancel_calibration, short=True)
        else:
            self.heading("Calibration saved", "The starting board was verified. You can now play.")
            self.button("Choose Difficulty", self.continue_after_calibration, accent=True)

    def render_photo_adjustments(self) -> None:
        for label, name, start, end in (("Brightness", "brightness", -60, 60),
                                         ("Contrast %", "contrast", 70, 160),
                                         ("Color warmth", "warmth", -40, 40)):
            slider = tk.Scale(self.panel, from_=start, to=end, resolution=10,
                              orient="horizontal", label=label, showvalue=True,
                              bg=self.PANEL, fg=self.TEXT, troughcolor="#314554",
                              activebackground=self.GREEN, highlightthickness=0,
                              bd=0, font=("DejaVu Sans", 9 if self.compact else 10),
                              length=self.panel_width - 40)
            slider.set(getattr(self.photo_adjustments, name))
            slider.configure(command=lambda value, field=name: self.set_photo_adjustment(field, int(float(value))))
            slider.pack(fill="x", pady=0 if self.compact else 1)

    def set_photo_adjustment(self, name: str, value: int) -> None:
        if self.calibration_flow is None or self.calibration_flow.stage != "ready":
            return
        settings = replace(self.photo_adjustments, **{name: value})
        if settings == self.photo_adjustments:
            return
        self.photo_adjustments = settings
        self.draw_board()
        self.say("Preview updated. Tap Check Board when the pieces stand out.")

    def saved_calibration_available(self) -> bool:
        directory = self.args.calibration
        if not ((directory / "calibration.json").is_file() and
                (directory / "empty_warp.png").is_file()):
            return False
        recorded = directory / "calibration_camera.json"
        if not recorded.exists():
            camera_matches = self.camera_settings == CameraSettings()
        else:
            camera_matches = read_camera_settings(recorded) == self.camera_settings
        adjustments = directory / "photo_adjustments.json"
        return camera_matches and (not adjustments.exists() or
                                   read_photo_adjustments(adjustments) is not None)

    def use_saved_calibration(self) -> None:
        if self.calibration_flow is None or self.busy or not self.saved_calibration_available():
            return
        self.busy = True
        self.say("Checking saved calibration…")
        self.render()
        self.submit("saved_calibration", lambda: load_calibration(self.args.calibration))

    def start_calibration(self) -> None:
        if self.session is not None or self.busy:
            return
        self.generation += 1
        self.calibration_flow = CalibrationFlow()
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Remove all pieces, then tap Take Empty Photo.")
        self.render()

    def cancel_calibration(self) -> None:
        self.generation += 1
        self.busy = False
        self.calibration_flow = CalibrationFlow() if self.startup_calibration else None
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Calibrate the board, or use a saved calibration." if self.startup_calibration else
                 "Choose a difficulty or recalibrate the board.")
        self.render()

    def retake_empty(self) -> None:
        self.calibration_flow = CalibrationFlow()
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Remove all pieces and retake the empty-board photo.")
        self.render()

    def undo_corner(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        flow.undo_corner()
        points = flow.empty_corners if flow.stage == "empty_corners" else flow.starting_corners
        self.say(f"Tap the outer {flow.CORNER_NAMES[len(points)]} corner.")
        self.render()

    def redo_empty_corners(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        flow.reset_empty_corners()
        self.say("Tap the outer top-left corner in the empty-board photo.")
        self.render()

    def redo_starting_corners(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        flow.reset_starting_corners()
        self.say("Tap the outer top-left corner in the setup photo.")
        self.render()

    def retake_starting(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        flow.retake_starting()
        self.say("Place all 32 starting pieces and retake the setup photo.")
        self.render()

    def capture_empty(self) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage != "empty" or self.busy:
            return
        self.busy = True
        self.say("Taking the empty-board photo…")
        self.render()

        def take_photo():
            path = self.args.calibration / "empty.jpg"
            self.camera.capture(path)
            photo = read_image(path)
            if self.args.corners:
                return photo, [point.copy() for point in self.args.corners]
            try:
                return photo, board_corners(photo).tolist()
            except VisionError:
                return photo, None

        self.submit("cal_empty", take_photo)

    def capture_starting(self) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage != "starting" or flow.empty_photo is None or self.busy:
            return
        self.busy = True
        self.say("Taking the starting-board photo…")
        self.render()
        previous_corners = [point.copy() for point in flow.empty_corners]

        def take_photo():
            path = self.args.calibration / "starting.jpg"
            self.camera.capture(path)
            photo = read_image(path)
            try:
                return photo, board_corners(photo).tolist(), False
            except VisionError:
                corners = reusable_corners(photo, previous_corners)
                return photo, corners, corners is not None

        self.submit("cal_start_photo", take_photo)

    def finish_calibration(self) -> None:
        flow = self.calibration_flow
        if (flow is None or flow.stage != "ready" or self.busy or
                flow.empty_photo is None or flow.starting_photo is None or
                len(flow.empty_corners) != 4 or len(flow.starting_corners) != 4):
            return
        empty = flow.empty_photo
        starting = flow.starting_photo
        empty_corners = [point.copy() for point in flow.empty_corners]
        starting_corners = [point.copy() for point in flow.starting_corners]
        adjustments = self.photo_adjustments
        self.busy = True
        self.say("Aligning both photos and checking the starting board…")
        self.render()
        self.submit("cal_finish", lambda: calibrate_photos(
            empty, starting, empty_corners, starting_corners, adjustments))

    def continue_after_calibration(self) -> None:
        self.startup_calibration = False
        self.calibration_flow = None
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Calibration is ready. Choose a difficulty to begin.", self.GREEN)
        self.render()

    def submit(self, tag: str, work) -> None:
        generation = self.generation

        def run() -> None:
            try:
                value = work()
                if tag == "start" and (self.closed or generation != self.generation):
                    self.discard_start(value)
                    return
                self.results.put((generation, tag, value, None))
            except Exception as exc:
                self.results.put((generation, tag, None, exc))

        self.executor.submit(run)

    def discard_start(self, value) -> None:
        """A game start nobody waits for any more: close the Stockfish it opened, and a gantry it opened."""
        value[2].close()
        gantry = value[3] if len(value) > 3 else None
        if gantry is not None and gantry is not self.gantry:
            gantry.close()

    def start_game(self) -> None:
        if not self.saved_calibration_available():
            self.start_calibration()
            self.say("Saved calibration is missing or incomplete. Take empty and setup photos.", self.GOLD)
            return
        self.photo_adjustments = (read_photo_adjustments(self.args.calibration / "photo_adjustments.json")
                                  or PhotoAdjustments())
        self.generation += 1
        self.scan_photo = None
        self.scan_corners = []
        self.forget_gantry_work()
        self.busy = True
        self.say("Loading calibration and starting Stockfish…")
        self.render()
        gantry = self.gantry   # still open from the last game, or None

        def load():
            calibration, reference = load_calibration(self.args.calibration)
            player = StockfishPlayer()
            opened, problem = gantry, None
            if opened is None and GantryPlayer is not None:
                opened = GantryPlayer.from_env()   # None when no gantry is configured
                if opened is not None:
                    try:
                        opened.connect()
                    except GantryError as exc:
                        opened, problem = None, str(exc)
            return calibration, reference, player, opened, problem

        self.submit("start", load)

    def scan(self) -> None:
        if self.session is None or self.busy:
            return
        self.scan_feedback = None
        remembered = (self.last_scan_corners or
                      (self.calibration.corners if self.calibration is not None else None))
        self.busy = True
        self.say("Taking a photo of the physical board…")
        self.render()

        def take_photo():
            path = self.args.calibration / "captures" / f"capture_{uuid4().hex}.jpg"
            self.camera.capture(path)
            photo = read_image(path)
            if self.args.corners:
                return photo, [point.copy() for point in self.args.corners], "specified"
            try:
                return photo, board_corners(photo).tolist(), "markers"
            except VisionError:
                corners = reusable_corners(photo, remembered)
                return photo, corners, "saved" if corners is not None else "manual"

        self.submit("scan_photo", take_photo)

    def process_scan_photo(self) -> None:
        if (self.scan_photo is None or len(self.scan_corners) != 4 or self.busy or
                self.calibration is None or self.reference is None):
            return
        photo = self.scan_photo
        corners = [point.copy() for point in self.scan_corners]
        calibration, reference = self.calibration, self.reference
        self.busy = True
        self.say("Checking the aligned board photo…")
        self.render()

        def check_photo():
            observation, _aligned = observe(reference, adjust_photo(photo, self.photo_adjustments),
                                            calibration, corners)
            return observation

        self.submit("scan", check_photo)

    def use_saved_scan_corners(self) -> None:
        if self.last_scan_corners is None:
            return
        self.scan_corners = [point.copy() for point in self.last_scan_corners]
        self.scan_corner_source = "saved"
        self.scan_feedback = None
        self.process_scan_photo()

    def undo_scan_corner(self) -> None:
        if self.scan_corners:
            self.scan_corners.pop()
        self.say(f"Tap the outer {CalibrationFlow.CORNER_NAMES[len(self.scan_corners)]} corner.")
        self.render()

    def redo_scan_corners(self) -> None:
        self.scan_corners = []
        self.scan_corner_source = "manual"
        self.scan_feedback = None
        self.say("Tap the outer top-left corner in this photo.")
        self.render()

    def retake_scan(self) -> None:
        self.scan_photo = None
        self.scan_corners = []
        self.scan_feedback = None
        self.scan()

    def poll_results(self) -> None:
        if self.closed:
            return
        try:
            while True:
                generation, tag, value, error = self.results.get_nowait()
                if generation != self.generation:
                    if tag == "start" and value is not None:
                        self.executor.submit(self.discard_start, value)
                    continue
                if tag == "online":
                    self.handle_online(value, error)
                    continue
                if tag == "online_confirm":
                    self.online_confirmed(value, error)
                    continue
                if tag == "gantry_progress":
                    self.say(value, self.GOLD)   # the gantry's own words while it works; the screen stays busy
                    continue
                self.busy = False
                if tag in ("gantry_play", "gantry_reset"):
                    self.handle_gantry_result(tag, value, error)
                    continue
                if tag == "online_scan":
                    holder = self.online_session
                    if holder is not None:
                        if error is not None:
                            holder.checking = False
                            holder.problem = f"The camera could not check the board: {error}"
                            self.say(holder.problem, self.RED)
                        else:
                            self.online_board_checked(value)
                    self.render()
                    continue
                if error is not None:
                    if tag == "start" and isinstance(error, VisionError):
                        self.say("Calibration missing or unreadable. Tap Calibrate Board.", self.RED)
                    elif tag == "cal_finish" and isinstance(error, VisionError):
                        print(f"Calibration check failed: {error}")
                        self.say("Calibration check failed. Redo either photo's corners or retake the setup photo.",
                                 self.RED)
                    else:
                        self.say(f"Error: {error}", self.RED)
                    self.render()
                    continue
                try:
                    self.handle_result(tag, value)
                except MoveRejected as exc:
                    if tag == "scan" and self.scan_photo is not None and self.session is not None:
                        if self.session.phase == "human":
                            self.scan_feedback = rejected_move_feedback(
                                self.session.game.board, value, str(exc))
                        elif self.session.phase == "computer":
                            self.scan_feedback = ScanFeedback("CHECK COMPUTER MOVE", str(exc))
                        elif self.session.phase == "remove":
                            self.scan_feedback = ScanFeedback("REMOVE THE PIECE", str(exc))
                        else:
                            self.scan_feedback = ScanFeedback("SETUP NOT VERIFIED", str(exc))
                        self.say("Check the message on the photo.", self.RED)
                    else:
                        self.say(str(exc), self.RED)
                except (ValueError, RuntimeError, OSError, cv2.error, GantryError) as exc:
                    self.say(str(exc), self.RED)
                self.render()
        except queue.Empty:
            pass
        self.root.after(70, self.poll_results)

    def handle_result(self, tag: str, value) -> None:
        if tag == "saved_calibration":
            self.calibration, self.reference = value
            self.last_scan_corners = saved_scan_corners(self.args.calibration, self.calibration)
            self.photo_adjustments = (read_photo_adjustments(self.args.calibration / "photo_adjustments.json")
                                      or PhotoAdjustments())
            self.startup_calibration = False
            self.calibration_flow = None
            if self.calibration.scoring == "legacy":
                self.say("Saved calibration uses the older lighting method. Recalibrate for light correction.",
                         self.GOLD)
            else:
                self.say("Saved calibration loaded. Choose a difficulty to begin.", self.GREEN)
        elif tag == "cal_empty":
            flow = self.calibration_flow
            assert flow is not None
            photo, detected_corners = value
            flow.accept_empty(photo, detected_corners)
            if self.args.corners:
                self.say("Empty-board corners are ready. Place the starting pieces and take the next photo.")
            elif detected_corners is not None:
                self.say("Empty-board markers found. Place the starting pieces and take the next photo.")
            else:
                self.say("Tap the outer top-left corner in the empty-board photo.")
        elif tag == "cal_start_photo":
            flow = self.calibration_flow
            assert flow is not None
            photo, detected_corners, reused_corners = value
            flow.accept_starting(photo, detected_corners, reused_corners=reused_corners)
            if flow.stage == "ready":
                self.say("Adjust the captured setup photo, then tap Check Board.")
            else:
                self.say("Tap the outer top-left corner in the setup photo.")
        elif tag == "cal_finish":
            flow = self.calibration_flow
            assert flow is not None
            calibration, reference = value
            save_calibration(self.args.calibration, calibration, reference)
            write_photo_adjustments(self.args.calibration / "photo_adjustments.json",
                                    self.photo_adjustments)
            write_camera_settings(self.args.calibration / "calibration_camera.json",
                                  self.camera_settings)
            self.calibration, self.reference = calibration, reference
            self.last_scan_corners = calibration.corners
            self.reset_notes = ()   # the camera just saw all 32 pieces
            flow.stage = "done"
            self.say("Calibration saved and starting board verified. Tap Choose Difficulty.", self.GREEN)
        elif tag == "start":
            self.calibration, self.reference, self.player, self.gantry, gantry_problem = value
            self.last_scan_corners = saved_scan_corners(self.args.calibration, self.calibration)
            self.session = TouchSession(self.level, gantry=self.gantry)
            notes = " ".join(self.reset_notes)
            place = (notes + " " if notes else "") + "Place the pieces in their starting squares and scan the board."
            if gantry_problem:
                self.say(f"{gantry_problem} Playing without the gantry: move Black's pieces by hand. " + place, self.GOLD)
            else:
                self.say(place, self.GOLD if notes else None)
        elif tag == "scan_photo":
            photo, corners, self.scan_corner_source = value
            self.scan_photo = photo
            self.scan_corners = corners or []
            if corners is not None:
                self.process_scan_photo()
            else:
                self.say("Tap the four outer board corners in this photo, starting at top-left.")
        elif tag == "scan":
            assert self.session is not None
            used_corners = [point.copy() for point in self.scan_corners]
            if self.session.phase == "setup":
                self.session.verify_setup(value)
                self.clear_scan_photo()
                self.reset_notes = ()   # the camera saw the starting position: the pieces are all back
                if self.session.phase == "setup_confirm":
                    self.say("Please check these squares on the physical board: " +
                             ", ".join(self.session.setup_uncertain) + ".", self.GOLD)
                else:
                    self.say("Starting position verified. Your move is legal when the scan matches a chess move.",
                             self.GREEN)
            elif self.session.phase == "human":
                san = self.session.scan_human(value)
                self.clear_scan_photo()
                if san is None:
                    self.say("Legal move detected. Choose the piece or move shown on screen.", self.GREEN)
                else:
                    detail = (" Unclear squares outside the move: " + ", ".join(value.uncertain) + "."
                              if value.uncertain else "")
                    self.say(f"Legal move: {san}." + detail, self.GREEN)
                    self.after_human_move()
            elif self.session.phase == "remove":
                square = self.session.verify_removal(value)
                self.clear_scan_photo()
                self.say(f"{square} is clear.", self.GREEN)
                self.carry_computer_move()   # the gantry plays the capturing move; the camera check follows
            elif self.session.phase == "computer":
                san = self.session.verify_computer(value)
                self.clear_scan_photo()
                notes = " ".join(self.session.gantry_notes)
                self.session.gantry_notes = ()
                self.say(f"Computer move {san} verified. " +
                         (self.session.result or "Your turn.") +
                         (" Unclear squares outside the move: " + ", ".join(value.uncertain) + "."
                          if value.uncertain else "") + (" " + notes if notes else ""), self.GREEN)
                if self.session.phase == "finished":
                    self.reset_pieces()
            self.last_scan_corners = used_corners
        elif tag == "engine":
            assert self.session is not None
            san = self.session.announce_computer(value)
            if self.session.phase == "remove":
                # The gantry does not take captured pieces off: the person does, and Check has the camera confirm it.
                self.say(self.removal_instruction(), self.GOLD)
                return
            directions = f"Stockfish: {san}. Move {value.uci()[:2]} to {value.uci()[2:4]}."
            if self.session.game.board.is_capture(value):
                directions += " Remove the captured piece."
            if self.session.game.board.is_castling(value):
                directions += " Move the rook too."
            if value.promotion:
                directions += f" Use a {chess.piece_name(value.promotion)}."
            self.say(directions, self.GOLD)
            if self.session.gantry is not None:
                self.carry_computer_move()

    def after_human_move(self) -> None:
        assert self.session is not None
        self.selected_square = None
        if self.session.phase == "finished":
            self.say(self.session.result or "Game over.", self.GREEN)
            self.reset_pieces()
            return
        assert self.player is not None
        self.request_computer_move()

    # ------------------------------------------------------------------ the gantry in the in-person game

    def gantry_progress(self):
        """A `say` for the worker thread: progress lines reach the status bar through the result queue."""
        generation, results = self.generation, self.results
        return lambda text: results.put((generation, "gantry_progress", str(text), None))

    def carry_computer_move(self) -> None:
        """Stockfish's move is known: the gantry carries it in the worker, then the camera verifies the
        board exactly as after a move made by hand."""
        session = self.session
        if session is None or session.phase != "computer" or session.gantry is None or self.busy:
            return
        move = session.computer_move
        assert move is not None
        self.busy = True
        self.gantry_activity = f"Gantry moving {move.uci()}…"
        self.say(f"Gantry moving {move.uci()}… keep hands clear of the board.", self.GOLD)
        progress = self.gantry_progress()
        self.submit("gantry_play", lambda: session.carry_computer_move(say=progress))

    def reset_pieces(self, then=None) -> None:
        """With a gantry that knows the layout: every piece goes home in the worker behind a
        "Putting the pieces back…" screen, then `then(problem)` runs with the gantry's error text or
        None. Otherwise `then(None)` runs at once."""
        session = self.session
        if self.gantry is None or session is None or not session.layout_known or self.resetting:
            if then is not None:
                then(None)
            return
        self.resetting = True
        self.busy = True
        self.gantry_activity = "Putting the pieces back…"
        self.after_reset = then
        self.say("Putting the pieces back… keep hands clear of the board.", self.GOLD)
        progress = self.gantry_progress()
        self.submit("gantry_reset", lambda: session.put_pieces_back(say=progress))

    def handle_gantry_result(self, tag: str, value, error) -> None:
        self.gantry_activity = None
        session = self.session
        if tag == "gantry_play":
            if session is None or session.phase != "computer":
                self.render()
                return
            if error is not None:
                message = str(error) if isinstance(error, GantryError) else f"Error: {error}"
                session.gantry_failed(message)
                move = session.computer_move
                self.say(f"The gantry could not make the move: {message} Move {move.uci()[:2]} to {move.uci()[2:4]} "
                         "by hand, then tap Verify Computer Move.", self.RED)
                self.render()
                return
            session.gantry_moved(value)
            self.say(f"Gantry moved {session.computer_move.uci()}. Checking the board with the camera…", self.GOLD)
            self.scan()   # the same verification as after a move by hand
            return
        # gantry_reset
        self.resetting = False
        then, self.after_reset = self.after_reset, None
        problem = f"The gantry could not put the pieces back: {error}" if error is not None else None
        # The pieces the person took during the game are not the gantry's to put back: the notes stay on
        # the Set up the board screen until the camera has verified the starting position.
        self.reset_notes = tuple(str(note) for note in (value or ())) if error is None else ()
        if then is not None:
            then(problem)
        elif problem:
            self.say(f"{problem} Set up the starting position by hand before the next game.", self.RED)
        else:
            notes = " ".join(value or ())
            result = session.result + " " if session is not None and session.result else ""
            self.say(result + "The pieces are back on their starting squares." + (" " + notes if notes else ""), self.GREEN)
        self.render()

    def forget_gantry_work(self) -> None:
        """A gantry job whose result nobody waits for any more (the generation moved on)."""
        self.gantry_activity = None
        self.resetting = False
        self.after_reset = None
        self.reset_armed_until = 0.0

    def reset_board(self) -> None:
        """Reset Board on the in-person game screens: one confirming tap, then the pieces go back to their
        starting squares (by the gantry, or by hand) and the game starts over with the usual camera check."""
        session = self.session
        if session is None or self.busy or session.phase in ("setup", "setup_confirm", "finished"):
            return
        if not self.confirm_reset("Reset Board"):
            return
        self.generation += 1
        self.clear_scan_photo()
        self.selected_square = None
        self.forget_gantry_work()

        def start_over(problem: str | None) -> None:
            self.session = TouchSession(self.level, gantry=self.gantry)
            notes = " ".join(self.reset_notes)
            self.say((problem or "Board reset.") + (" " + notes if notes else "") +
                     " Place the pieces in their starting squares and scan the board.",
                     self.RED if problem else self.GOLD)

        self.reset_pieces(then=start_over)
        self.render()

    def request_computer_move(self) -> None:
        if self.session is None or self.session.phase != "thinking" or self.busy:
            return
        assert self.player is not None
        board = self.session.game.board.copy(stack=True)
        level = self.session.game.level
        player = self.player
        self.busy = True
        self.submit("engine", lambda: player.choose_move(board, level))
        self.render()

    def select_move(self, move: chess.Move) -> None:
        assert self.session is not None
        try:
            san = self.session.select_move(move)
            self.say(f"Legal move: {san}", self.GREEN)
            self.after_human_move()
        except ValueError as exc:
            self.say(str(exc), self.RED)
        self.render()

    def retake(self) -> None:
        assert self.session is not None
        self.session.retake()
        self.say("Retake the board photo after your move.")
        self.render()

    def confirm_setup(self) -> None:
        assert self.session is not None
        self.session.confirm_setup()
        self.say("Starting board confirmed. Your turn.", self.GREEN)
        self.render()

    def retry_setup_scan(self) -> None:
        assert self.session is not None
        self.session.retry_setup()
        self.scan()

    def clear_scan_photo(self) -> None:
        self.scan_photo = None
        self.scan_corners = []
        self.scan_feedback = None
        self.preview_photo = None
        self.preview_geometry = None

    def end_game(self) -> None:
        self.generation += 1
        self.busy = False
        self.clear_scan_photo()
        self.forget_gantry_work()
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        if self.session is None or self.session.phase == "setup":
            self.session = None
            self.say("Game cancelled.")
        else:
            abandoned = self.session.phase != "finished"
            self.session.resign()
            self.say(self.session.result or "Game ended.", self.GOLD)
            if abandoned:
                self.reset_pieces()   # with a gantry: the pieces go home now, behind "Putting the pieces back…"
        self.render()

    def back_to_menu(self) -> None:
        self.generation += 1
        self.busy = False
        self.clear_scan_photo()
        self.forget_gantry_work()
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        self.session = None
        self.selected_square = None
        self.say("Choose a difficulty to begin.")
        self.render()

    def tap_board(self, event) -> None:
        if self.online_session is not None:
            return   # the website is playing; the screen only shows
        if self.calibration_flow is not None:
            self.tap_calibration_corner(event)
            return
        if self.scan_photo is not None:
            self.tap_scan_corner(event)
            return
        if self.session is None or self.session.phase != "human" or self.busy:
            return
        x, y, size, square_size = self.board_geometry()
        if not (x <= event.x < x + size and y <= event.y < y + size):
            return
        col = int((event.x - x) / square_size)
        row = int((event.y - y) / square_size)
        if not (0 <= col < 8 and 0 <= row < 8):
            return
        name = f"{'abcdefgh'[col]}{8 - row}"
        legal = self.session.game.legal_destinations(name)
        self.selected_square = name if legal else None
        self.say(f"{name}: legal destinations {', '.join(sorted(legal))}." if legal else
                 f"{name}: choose one of your pieces.", self.GOLD if legal else self.MUTED)
        self.draw_board()

    def tap_calibration_corner(self, event) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage not in ("empty_corners", "starting_corners") or self.busy:
            return
        if self.preview_geometry is None:
            return
        photo = flow.empty_photo if flow.stage == "empty_corners" else flow.starting_photo
        if photo is None:
            return
        left, top, scale = self.preview_geometry
        height, width = photo.shape[:2]
        x = (event.x - left) / scale
        y = (event.y - top) / scale
        if not (0 <= x < width and 0 <= y < height):
            return
        flow.add_corner(x, y)
        if flow.stage == "starting":
            self.say("Empty-board corners set. Place all starting pieces, then take the setup photo.")
        elif flow.stage == "ready":
            self.say("Adjust the captured setup photo, then tap Check Board.")
        else:
            points = flow.empty_corners if flow.stage == "empty_corners" else flow.starting_corners
            self.say(f"Tap the outer {flow.CORNER_NAMES[len(points)]} corner.")
        self.render()

    def tap_scan_corner(self, event) -> None:
        if self.scan_photo is None or len(self.scan_corners) == 4 or self.busy:
            return
        if self.preview_geometry is None:
            return
        left, top, scale = self.preview_geometry
        height, width = self.scan_photo.shape[:2]
        x = (event.x - left) / scale
        y = (event.y - top) / scale
        if not (0 <= x < width and 0 <= y < height):
            return
        self.scan_corners.append([x, y])
        if len(self.scan_corners) == 4:
            self.process_scan_photo()
        else:
            self.say(f"Tap the outer {CalibrationFlow.CORNER_NAMES[len(self.scan_corners)]} corner.")
            self.render()

    def draw_photo_preview(self) -> None:
        flow = self.calibration_flow
        canvas = self.board_canvas
        canvas.delete("all")
        if flow is not None:
            show_starting = flow.stage in ("starting_corners", "ready")
            photo = flow.starting_photo if show_starting else flow.empty_photo
            points = flow.starting_corners if show_starting else flow.empty_corners
        else:
            photo = self.scan_photo
            points = self.scan_corners
        assert photo is not None
        height, width = photo.shape[:2]
        available_width = canvas.winfo_width() if canvas.winfo_width() > 1 else 400
        available_height = canvas.winfo_height() if canvas.winfo_height() > 1 else 360
        scale = min((available_width - 20) / width, (available_height - 20) / height)
        display_width = max(1, round(width * scale))
        display_height = max(1, round(height * scale))
        resized = adjust_photo(
            cv2.resize(photo, (display_width, display_height), interpolation=cv2.INTER_AREA),
            self.photo_adjustments)
        ok, encoded = cv2.imencode(".png", resized)
        if not ok:
            raise VisionError("Could not display the empty-board photo.")
        self.preview_photo = tk.PhotoImage(data=base64.b64encode(encoded.tobytes()).decode("ascii"))
        left = (available_width - display_width) / 2
        top = (available_height - display_height) / 2
        canvas.create_image(left, top, image=self.preview_photo, anchor="nw")
        self.preview_geometry = (left, top, scale)
        for index, (x, y) in enumerate(points):
            px, py = left + x * scale, top + y * scale
            canvas.create_oval(px - 7, py - 7, px + 7, py + 7,
                               fill=self.GOLD, outline=self.BG, width=2)
            canvas.create_text(px + 12, py - 12, text=str(index + 1), fill=self.GOLD,
                               font=("DejaVu Sans", 12, "bold"))
        if flow is None and self.scan_feedback is not None:
            feedback = self.scan_feedback
            canvas.create_rectangle(left, top, left + display_width, top + 48,
                                    fill="#8a2f32", outline="")
            canvas.create_text(left + 15, top + 24, text=feedback.title,
                               fill=self.TEXT, anchor="w",
                               font=("DejaVu Sans", 16, "bold"))
            if (len(points) == 4 and feedback.source and feedback.destination and
                    (self.calibration is None or self.calibration.rotation == "none")):
                board_outline = np.float32([[0, 0], [799, 0], [799, 799], [0, 799]])
                transform = cv2.getPerspectiveTransform(board_outline, np.float32(points))
                centers = []
                for square in (feedback.source, feedback.destination):
                    index = chess.parse_square(square)
                    board_x = (chess.square_file(index) + 0.5) * 100
                    board_y = (7 - chess.square_rank(index) + 0.5) * 100
                    pixel = cv2.perspectiveTransform(
                        np.float32([[[board_x, board_y]]]), transform)[0, 0]
                    centers.append((left + float(pixel[0]) * scale,
                                    top + float(pixel[1]) * scale))
                canvas.create_line(*centers[0], *centers[1], fill=self.RED,
                                   width=5, arrow="last")
                for center, square in zip(centers, (feedback.source, feedback.destination)):
                    px, py = center
                    canvas.create_oval(px - 18, py - 18, px + 18, py + 18,
                                       outline=self.RED, width=4)
                    canvas.create_text(px, py - 28, text=square, fill=self.RED,
                                       font=("DejaVu Sans", 15, "bold"))

    def board_geometry(self) -> tuple[float, float, float, float]:
        actual_width = self.board_canvas.winfo_width()
        actual_height = self.board_canvas.winfo_height()
        width = actual_width if actual_width > 1 else 400
        height = actual_height if actual_height > 1 else 360
        size = max(80, min(width - 34, height - 30))
        square_size = size / 8
        return (width - size) / 2, (height - size) / 2, size, square_size

    def draw_board(self) -> None:
        if self.online_session is not None:
            self.draw_online()
            return
        if (self.scan_photo is not None or
                (self.calibration_flow is not None and
                 self.calibration_flow.stage in ("empty_corners", "starting", "starting_corners", "ready") and
                 self.calibration_flow.empty_photo is not None)):
            self.draw_photo_preview()
            return
        canvas = self.board_canvas
        canvas.delete("all")
        self.preview_geometry = None
        board = self.session.game.board if self.session else chess.Board()
        highlights: set[str] = set()
        if self.session and self.session.phase == "human" and self.selected_square:
            highlights = self.session.game.legal_destinations(self.selected_square)
        last_move = self.session.game.last_move if self.session else None
        pending = self.session.computer_move if self.session else None
        uncertain = (self.session.setup_uncertain if self.session and self.session.phase == "setup_confirm" else ())
        remove = (self.session.removal[0] if self.session and self.session.phase == "remove" and self.session.removal
                  else None)
        self._draw_position(board, last_move, pending, highlights, uncertain, remove)

    def _draw_position(self, board: chess.Board, last_move: chess.Move | None, pending: chess.Move | None,
                       highlights: set[str], uncertain, remove: str | None = None) -> None:
        """The 8x8 board on the canvas: used for the in-person game and for the online game alike.
        `remove` is the square whose piece the person must take off before the gantry plays."""
        canvas = self.board_canvas
        x, y, size, step = self.board_geometry()
        for row in range(8):
            for col in range(8):
                square = chess.square(col, 7 - row)
                name = chess.square_name(square)
                left, top = x + col * step, y + row * step
                fill = "#e8ddc4" if (row + col) % 2 == 0 else "#678577"
                if last_move and square in (last_move.from_square, last_move.to_square):
                    fill = "#c6ae65"
                if pending and square in (pending.from_square, pending.to_square):
                    fill = "#e3b76d"
                if name in uncertain or name == remove:
                    fill = "#ff8c85"
                if name == self.selected_square:
                    fill = "#ffe08a"
                canvas.create_rectangle(left, top, left + step, top + step,
                                        fill=fill, outline=fill)
                if name in highlights:
                    margin = step * .36
                    canvas.create_oval(left + margin, top + margin,
                                       left + step - margin, top + step - margin,
                                       fill="#ffd66e", outline="")
                piece = board.piece_at(square)
                if piece:
                    radius = step * .35
                    cx, cy = left + step / 2, top + step / 2
                    canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                                       fill="#faf6ea" if piece.color else "#243541",
                                       outline="#354550", width=2)
                    canvas.create_text(cx, cy, text=piece.symbol().upper(),
                                       fill="#202c32" if piece.color else "#ffffff",
                                       font=("DejaVu Sans", max(16, int(step * .46)), "bold"))
        for col, file in enumerate("abcdefgh"):
            canvas.create_text(x + (col + .5) * step, y + size + 10, text=file,
                               fill=self.MUTED, font=("DejaVu Sans", 9))

    def close(self) -> None:
        self.closed = True
        self.generation += 1
        try:
            while True:
                _generation, tag, value, _error = self.results.get_nowait()
                if tag == "start" and value is not None:
                    self.executor.submit(self.discard_start, value)
        except queue.Empty:
            pass
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        if self.gantry is not None:
            # Leaving mid-game: the pieces go home first (the worker finishes before the process exits), then the link closes.
            gantry, self.gantry = self.gantry, None
            session = self.session
            if session is not None and session.layout_known and not self.resetting:
                self.executor.submit(lambda: (session.put_pieces_back(), gantry.close()))
            else:
                self.executor.submit(gantry.close)
        if self.online_session is not None:
            holder, self.online_session = self.online_session, None
            holder.stop()
        self.executor.shutdown(wait=False)
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, default=Path("calibration"))
    parser.add_argument("--camera", choices=("pi", "usb"), default="pi")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--corners", nargs=8, type=float,
                        metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"))
    parser.add_argument("--windowed", action="store_true", help="Show an 800×480 window for development")
    args = parser.parse_args(argv)
    if args.corners:
        args.corners = [args.corners[index:index + 2] for index in range(0, 8, 2)]
    root = tk.Tk()
    TouchApp(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
