"""Touch screen front end for the camera verified Raspberry Pi chess game."""

from __future__ import annotations

import argparse
import base64
import json
import queue
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
from pi_camera import Camera, CameraError, CameraSettings


class MoveRejected(ValueError):
    """The physical board cannot safely advance to the next position."""


@dataclass(frozen=True)
class ScanFeedback:
    title: str
    detail: str
    sources: tuple[str, ...] = ()
    destinations: tuple[str, ...] = ()
    expected_sources: tuple[str, ...] = ()
    expected_destinations: tuple[str, ...] = ()

    @property
    def source(self) -> str | None:
        return self.sources[0] if len(self.sources) == 1 else None

    @property
    def destination(self) -> str | None:
        return self.destinations[0] if len(self.destinations) == 1 else None


MAX_UNCERTAIN_SQUARES = 2
MAX_SCAN_ATTEMPTS = 3


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
    """Name changed pieces without guessing which destination belongs to each."""
    if "No move detected" in reason:
        return ScanFeedback("NO CLEAR MOVE", "The camera found no clear change in occupied squares. Check the board and rescan.")
    if len(observation.uncertain) > MAX_UNCERTAIN_SQUARES:
        return ScanFeedback("PHOTO UNCLEAR", "Several squares are unclear. Check the photo and its corners.")
    unclear = set(observation.uncertain)
    before = occupied_squares(board)
    vacated = sorted(before - observation.occupied - unclear)
    filled = sorted(observation.occupied - before - unclear)
    if not unclear and 2 <= len(vacated) <= 4 and len(filled) <= 4:
        pieces = [board.piece_at(chess.parse_square(square)) for square in vacated]
        names = [f"{'White' if piece.color else 'Black'} {chess.piece_name(piece.piece_type)} {square}"
                 for piece, square in zip(pieces, vacated) if piece is not None]
        destination_text = ("New occupied squares: " + ", ".join(filled) + ". "
                            if filled else "")
        return ScanFeedback(
            "MULTIPLE PIECES CHANGED",
            "Camera sees these pieces leave their squares: " + ", ".join(names) + ". " +
            destination_text + "Restore the board and make one move, then scan again.",
            tuple(vacated), tuple(filled))
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
                return ScanFeedback("ILLEGAL MOVE", detail, (source,), (destination,))
    return ScanFeedback("MOVE NOT VERIFIED", reason + " Check the board or adjust the photo corners.")


def computer_move_feedback(board: chess.Board, move: chess.Move,
                           observation: Observation) -> ScanFeedback:
    """Compare the requested Black move with observable square changes."""
    source = chess.square_name(move.from_square)
    destination = chess.square_name(move.to_square)
    piece = board.piece_at(move.from_square)
    name = chess.piece_name(piece.piece_type) if piece else "piece"
    expected = f"Expected: Black {name} {source} → {destination}"
    expected_sources = (source,)
    expected_destinations = (destination,)
    if board.is_castling(move):
        rank = "8" if piece and piece.color == chess.BLACK else "1"
        rook_source, rook_destination = (("h" + rank, "f" + rank) if
                                         chess.square_file(move.to_square) == 6 else
                                         ("a" + rank, "d" + rank))
        expected += f", rook {rook_source} → {rook_destination}"
        expected_sources += (rook_source,)
        expected_destinations += (rook_destination,)
    expected += ". "
    unclear = set(observation.uncertain)
    before = occupied_squares(board)
    vacated = tuple(sorted(before - observation.occupied - unclear))
    filled = tuple(sorted(observation.occupied - before - unclear))
    _, affected = _after_move(board, move)
    if len(unclear) > MAX_UNCERTAIN_SQUARES or unclear & affected:
        squares = ", ".join(sorted(unclear & affected or unclear))
        return ScanFeedback(
            "PHOTO UNCLEAR", expected + f"Camera cannot read {squares} clearly. Retake the photo.",
            expected_sources=expected_sources, expected_destinations=expected_destinations)
    if len(vacated) + len(filled) > 8:
        return ScanFeedback(
            "PHOTO UNCLEAR", expected + "Too many squares changed to identify the move. "
            "Check the photo corners and try again.",
            expected_sources=expected_sources, expected_destinations=expected_destinations)
    if len(vacated) == 1 and len(filled) == 1:
        actual_piece = board.piece_at(chess.parse_square(vacated[0]))
        actual_name = (f"{'White' if actual_piece.color else 'Black'} "
                       f"{chess.piece_name(actual_piece.piece_type)}" if actual_piece else "piece")
        seen = f"Camera sees: {actual_name} {vacated[0]} → {filled[0]}. "
    elif vacated or filled:
        removed = ", ".join(vacated) if vacated else "none"
        added = ", ".join(filled) if filled else "none"
        seen = (f"Camera sees emptied squares: {removed}; newly occupied squares: {added}. "
                "It cannot tell which destination belongs to which piece. ")
    else:
        seen = "Camera sees no clear square changes. "
    return ScanFeedback(
        "COMPUTER MOVE MISMATCH", expected + seen + "Correct the physical board and scan again.",
        vacated, filled, expected_sources, expected_destinations)


def should_retry_scan(feedback: ScanFeedback, corner_source: str, attempts: int) -> bool:
    """Retry uncertain captures, while showing clear rule violations immediately."""
    return (corner_source != "manual" and attempts < MAX_SCAN_ATTEMPTS and
            feedback.title in ("NO CLEAR MOVE", "PHOTO UNCLEAR", "MOVE NOT VERIFIED",
                               "SETUP NOT VERIFIED", "COMPUTER MOVE MISMATCH"))


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
    """Rules and camera match decisions, independent of the display and hardware."""

    def __init__(self, level: str) -> None:
        self.game = ChessGame()
        self.game.start(level)
        self.phase = "setup"
        self.choices: tuple[chess.Move, ...] = ()
        self.computer_move: chess.Move | None = None
        self.result: str | None = None
        self.setup_uncertain: tuple[str, ...] = ()

    def verify_setup(self, observed: Observation | frozenset[str]) -> None:
        if self.phase != "setup":
            raise ValueError("Setup is not waiting for a scan.")
        observation = _checked_observation(observed)
        if not _matches_known_squares(occupied_squares(self.game.board), observation):
            raise MoveRejected("Board does not match the starting position. Correct the pieces and scan again.")
        self.setup_uncertain = observation.uncertain
        self.phase = "setup_confirm" if observation.uncertain else "human"

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
        san = self.game.play_human(
            chess.square_name(move.from_square),
            chess.square_name(move.to_square),
            chess.piece_symbol(move.promotion) if move.promotion else None,
        )
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "thinking"
        return san

    def announce_computer(self, move: chess.Move) -> str:
        if self.phase != "thinking" or move not in self.game.board.legal_moves:
            raise ValueError("The computer did not provide a legal move.")
        self.computer_move = move
        self.phase = "computer"
        return self.game.board.san(move)

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
            raise MoveRejected("Board does not match the computer move. Correct it and scan again.")
        san = self.game.play_computer(self.computer_move)
        self.computer_move = None
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "human"
        return san

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

    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
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
        self.scan_attempts = 0
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_geometry: tuple[float, float, float] | None = None
        self.level = "beginner"
        self.busy = False
        self.selected_square: str | None = None
        self.message = "Calibrate the board, or use a saved calibration."
        if (args.calibration / "calibration.json").is_file() and not self.saved_calibration_available():
            self.message = "Saved calibration cannot be reused. Take new calibration photos."
        self.message_color = self.MUTED
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

    def heading(self, title: str, detail: str = "") -> None:
        tk.Label(self.panel, text=title, bg=self.PANEL, fg=self.TEXT,
                 font=("DejaVu Sans", 12 if self.compact else 16, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 3 if self.compact else 8))
        if detail and not self.compact:
            tk.Label(self.panel, text=detail, bg=self.PANEL, fg=self.MUTED,
                     font=("DejaVu Sans", 11), anchor="w", justify="left",
                     wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 12))

    def render(self) -> None:
        for child in self.panel.winfo_children():
            child.destroy()
        self.draw_board()
        if self.busy:
            self.heading("Please wait", "The camera or chess engine is working.")
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
            self.button("Start Game", self.start_game, accent=True)
            self.button("Calibrate Board", self.start_calibration, short=True)
        elif session.phase == "setup":
            self.heading("Set up the board", "Place all 32 pieces in the starting position, then scan.")
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
            self.button("End Game", self.end_game)
        elif session.phase == "thinking":
            self.heading("Computer thinking", "Leave the physical board as it is.")
            self.button("Try Computer Move", self.request_computer_move, accent=True)
            self.button("End Game", self.end_game)
        elif session.phase == "computer":
            move = session.computer_move
            assert move is not None
            san = session.game.board.san(move)
            extra = []
            if session.game.board.is_capture(move):
                extra.append("Remove the captured piece.")
            if session.game.board.is_castling(move):
                extra.append("Move the rook too.")
            if move.promotion:
                extra.append(f"Replace the pawn with a {chess.piece_name(move.promotion)}.")
            self.heading(f"Stockfish: {san}", f"Move {move.uci()[:2]} to {move.uci()[2:4]} on the board. " + " ".join(extra))
            self.button("Verify Computer Move", self.scan, accent=True)
            self.button("End Game", self.end_game)
        else:
            self.heading("Game over", session.result or "Game finished.")
            self.button("New Game", self.back_to_menu, accent=True)

    def choose_level(self, level: str) -> None:
        self.level = level
        self.say(f"{DIFFICULTIES[level].label} selected.")
        self.render()

    def render_scan_review(self) -> None:
        if self.scan_feedback is not None:
            feedback = self.scan_feedback
            tk.Label(self.panel, text=feedback.title, bg="#8a2f32", fg=self.TEXT,
                     font=("DejaVu Sans", 15 if self.compact else 20, "bold"),
                     wraplength=self.panel_width - 35, pady=10).pack(fill="x", pady=(0, 10))
            tk.Label(self.panel, text=feedback.detail, bg=self.PANEL, fg=self.TEXT,
                     font=("DejaVu Sans", 11 if self.compact else 13, "bold"),
                     justify="left", wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 12))
            if feedback.expected_sources:
                tk.Label(self.panel, text="Green: expected · Red: observed",
                         bg=self.PANEL, fg=self.MUTED, justify="left",
                         font=("DejaVu Sans", 9 if self.compact else 11),
                         wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 6))
            if self.scan_corner_source == "saved":
                tk.Label(self.panel, text="Using saved corners. If the camera moved, adjust them.",
                         bg=self.PANEL, fg=self.GOLD, justify="left",
                         font=("DejaVu Sans", 9 if self.compact else 11),
                         wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 8))
            action = ("Correct Move & Verify" if feedback.title == "COMPUTER MOVE MISMATCH" else
                      "Fix Board & Scan Again" if feedback.title in
                      ("ILLEGAL MOVE", "MULTIPLE PIECES CHANGED") else
                      "Make Move & Rescan" if feedback.title == "NO CLEAR MOVE" else "Retake Photo")
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
                    value[2].close()
                    return
                self.results.put((generation, tag, value, None))
            except Exception as exc:
                self.results.put((generation, tag, None, exc))

        self.executor.submit(run)

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
        self.busy = True
        self.say("Loading calibration and starting Stockfish…")
        self.render()

        def load():
            calibration, reference = load_calibration(self.args.calibration)
            return calibration, reference, StockfishPlayer()

        self.submit("start", load)

    def scan(self) -> None:
        if self.session is None or self.busy:
            return
        self.scan_attempts = 0
        self._capture_scan_attempt()

    def _capture_scan_attempt(self) -> None:
        self.scan_attempts += 1
        self.scan_feedback = None
        self.scan_photo = None
        self.scan_corners = []
        remembered = (self.last_scan_corners or
                      (self.calibration.corners if self.calibration is not None else None))
        self.busy = True
        self.say(f"Taking board photo {self.scan_attempts} of {MAX_SCAN_ATTEMPTS}…")
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
                        self.executor.submit(value[2].close)
                    continue
                self.busy = False
                if error is not None:
                    if (((tag == "scan_photo" and
                          isinstance(error, (CameraError, VisionError, cv2.error))) or
                         (tag == "scan" and self.scan_corner_source != "manual" and
                          isinstance(error, (VisionError, cv2.error)))) and
                            self.scan_attempts < MAX_SCAN_ATTEMPTS):
                        self._capture_scan_attempt()
                        continue
                    if (tag == "scan" and self.scan_photo is not None and
                            isinstance(error, (VisionError, cv2.error))):
                        self.scan_feedback = ScanFeedback(
                            "PHOTO UNCLEAR", f"Tried {self.scan_attempts} photos. "
                            "The board could not be read. Check lighting or adjust the photo corners.")
                        self.say("Check the message on the photo.", self.RED)
                    elif tag == "start" and isinstance(error, VisionError):
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
                            feedback = rejected_move_feedback(
                                self.session.game.board, value, str(exc))
                        elif self.session.phase == "computer":
                            assert self.session.computer_move is not None
                            feedback = computer_move_feedback(
                                self.session.game.board, self.session.computer_move, value)
                        else:
                            feedback = ScanFeedback("SETUP NOT VERIFIED", str(exc))
                        if should_retry_scan(feedback, self.scan_corner_source,
                                             self.scan_attempts):
                            self._capture_scan_attempt()
                            continue
                        if (self.scan_attempts == MAX_SCAN_ATTEMPTS and
                                feedback.title not in ("ILLEGAL MOVE", "MULTIPLE PIECES CHANGED")):
                            feedback = replace(feedback, detail=(
                                f"Tried {MAX_SCAN_ATTEMPTS} photos. " + feedback.detail))
                        self.scan_feedback = feedback
                        self.say("Check the message on the photo.", self.RED)
                    else:
                        self.say(str(exc), self.RED)
                except (ValueError, RuntimeError, OSError, cv2.error) as exc:
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
            flow.stage = "done"
            self.say("Calibration saved and starting board verified. Tap Choose Difficulty.", self.GREEN)
        elif tag == "start":
            self.calibration, self.reference, self.player = value
            self.last_scan_corners = saved_scan_corners(self.args.calibration, self.calibration)
            self.session = TouchSession(self.level)
            self.say("Place the pieces in their starting squares and scan the board.")
        elif tag == "scan_photo":
            photo, corners, self.scan_corner_source = value
            self.scan_photo = photo
            self.scan_corners = corners or []
            if corners is not None:
                self.process_scan_photo()
            elif self.scan_attempts < MAX_SCAN_ATTEMPTS:
                self._capture_scan_attempt()
            else:
                self.say("Could not find corners in three photos. Tap the four outer board corners.",
                         self.GOLD)
        elif tag == "scan":
            assert self.session is not None
            if (self.session.phase == "setup" and value.uncertain and
                    self.scan_corner_source != "manual" and
                    self.scan_attempts < MAX_SCAN_ATTEMPTS):
                self._capture_scan_attempt()
                return
            used_corners = [point.copy() for point in self.scan_corners]
            if self.session.phase == "setup":
                self.session.verify_setup(value)
                self.clear_scan_photo()
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
            elif self.session.phase == "computer":
                san = self.session.verify_computer(value)
                self.clear_scan_photo()
                self.say(f"Computer move {san} verified. " +
                         (self.session.result or "Your turn.") +
                         (" Unclear squares outside the move: " + ", ".join(value.uncertain) + "."
                          if value.uncertain else ""), self.GREEN)
            self.last_scan_corners = used_corners
        elif tag == "engine":
            assert self.session is not None
            san = self.session.announce_computer(value)
            directions = f"Stockfish: {san}. Move {value.uci()[:2]} to {value.uci()[2:4]}."
            if self.session.game.board.is_capture(value):
                directions += " Remove the captured piece."
            if self.session.game.board.is_castling(value):
                directions += " Move the rook too."
            if value.promotion:
                directions += f" Use a {chess.piece_name(value.promotion)}."
            self.say(directions, self.GOLD)

    def after_human_move(self) -> None:
        assert self.session is not None
        self.selected_square = None
        if self.session.phase == "finished":
            self.say(self.session.result or "Game over.", self.GREEN)
            return
        assert self.player is not None
        self.request_computer_move()

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
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        if self.session is None or self.session.phase == "setup":
            self.session = None
            self.say("Game cancelled.")
        else:
            self.session.resign()
            self.say(self.session.result or "Game ended.", self.GOLD)
        self.render()

    def back_to_menu(self) -> None:
        self.generation += 1
        self.clear_scan_photo()
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        self.session = None
        self.selected_square = None
        self.say("Choose a difficulty to begin.")
        self.render()

    def tap_board(self, event) -> None:
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
            if (len(points) == 4 and
                    (feedback.sources or feedback.destinations or feedback.expected_sources) and
                    (self.calibration is None or self.calibration.rotation == "none")):
                board_outline = np.float32([[0, 0], [799, 0], [799, 799], [0, 799]])
                transform = cv2.getPerspectiveTransform(board_outline, np.float32(points))
                centers = {}
                all_squares = (feedback.expected_sources + feedback.expected_destinations +
                               feedback.sources + feedback.destinations)
                for square in all_squares:
                    index = chess.parse_square(square)
                    board_x = (chess.square_file(index) + 0.5) * 100
                    board_y = (7 - chess.square_rank(index) + 0.5) * 100
                    pixel = cv2.perspectiveTransform(
                        np.float32([[[board_x, board_y]]]), transform)[0, 0]
                    centers[square] = (left + float(pixel[0]) * scale,
                                       top + float(pixel[1]) * scale)
                for expected_source, expected_destination in zip(
                        feedback.expected_sources, feedback.expected_destinations):
                    canvas.create_line(*centers[expected_source], *centers[expected_destination],
                                       fill=self.GREEN, width=4, dash=(8, 5), arrow="last")
                for square in feedback.expected_sources + feedback.expected_destinations:
                    px, py = centers[square]
                    canvas.create_oval(px - 24, py - 24, px + 24, py + 24,
                                       outline=self.GREEN, width=4)
                    canvas.create_text(px, py + 32, text=square, fill=self.GREEN,
                                       font=("DejaVu Sans", 15, "bold"))
                if (feedback.title in ("ILLEGAL MOVE", "COMPUTER MOVE MISMATCH") and
                        feedback.source and feedback.destination):
                    canvas.create_line(*centers[feedback.source], *centers[feedback.destination],
                                       fill=self.RED, width=5, arrow="last")
                for square in feedback.sources + feedback.destinations:
                    px, py = centers[square]
                    color = (self.RED if feedback.expected_sources or square in feedback.sources
                             else self.GOLD)
                    canvas.create_oval(px - 18, py - 18, px + 18, py + 18,
                                       outline=color, width=4)
                    canvas.create_text(px, py - 28, text=square, fill=color,
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
        x, y, size, step = self.board_geometry()
        highlights: set[str] = set()
        if self.session and self.session.phase == "human" and self.selected_square:
            highlights = self.session.game.legal_destinations(self.selected_square)
        last_move = self.session.game.last_move if self.session else None
        pending = self.session.computer_move if self.session else None
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
                if (self.session and self.session.phase == "setup_confirm" and
                        name in self.session.setup_uncertain):
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
                    self.executor.submit(value[2].close)
        except queue.Empty:
            pass
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
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
