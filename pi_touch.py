"""Touch screen front end for the camera verified Raspberry Pi chess game."""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import chess
import cv2
import numpy as np

from board_vision import (Calibration, Observation, TemporalObservation, VisionError,
                          board_corners, calibrate, load_calibration, observe,
                          observe_temporal, read_image, rotate_board, save_calibration,
                          warp_board, robust_square_scores)
from chess_game import ChessGame, DIFFICULTIES, StockfishPlayer
from move_observation import occupied_squares
from pi_camera import Camera, CameraError, CameraSettings

try:   # The gantry carries Stockfish's moves when CHESS_GANTRY_PORT is set. Without it the person moves them.
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


def photo_square_center(square: str, corners: list[list[float]],
                        rotation: str) -> tuple[float, float]:
    """Map an oriented chess square back onto the unrotated camera photo."""
    index = chess.parse_square(square)
    x = (chess.square_file(index) + 0.5) * 100
    y = (7 - chess.square_rank(index) + 0.5) * 100
    if rotation == "cw":
        x, y = y, 799 - x
    elif rotation == "ccw":
        x, y = 799 - y, x
    elif rotation == "180":
        x, y = 799 - x, 799 - y
    elif rotation != "none":
        raise ValueError(f"Invalid photo rotation: {rotation}")
    board_outline = np.float32([[0, 0], [799, 0], [799, 799], [0, 799]])
    transform = cv2.getPerspectiveTransform(board_outline, np.float32(corners))
    pixel = cv2.perspectiveTransform(np.float32([[[x, y]]]), transform)[0, 0]
    return float(pixel[0]), float(pixel[1])


def corners_from_rotated_preview(points: list[list[float]], width: int, height: int,
                                 rotation: str) -> list[list[float]]:
    """Convert corners tapped on a rotated preview to raw photo coordinates."""
    if len(points) != 4:
        raise ValueError("Four photo corners are required.")
    if rotation == "none":
        raw = [point.copy() for point in points]
        order = (0, 1, 2, 3)
    elif rotation == "cw":
        raw = [[y, height - 1 - x] for x, y in points]
        order = (1, 2, 3, 0)
    elif rotation == "180":
        raw = [[width - 1 - x, height - 1 - y] for x, y in points]
        order = (2, 3, 0, 1)
    elif rotation == "ccw":
        raw = [[width - 1 - y, x] for x, y in points]
        order = (3, 0, 1, 2)
    else:
        raise ValueError(f"Invalid photo rotation: {rotation}")
    return [raw[index] for index in order]


def rotate_photo_point(x: float, y: float, width: int, height: int,
                       rotation: str) -> tuple[float, float]:
    """Map a raw camera pixel onto its rotated screen preview."""
    if rotation == "none":
        return x, y
    if rotation == "cw":
        return height - 1 - y, x
    if rotation == "180":
        return width - 1 - x, height - 1 - y
    if rotation == "ccw":
        return y, width - 1 - x
    raise ValueError(f"Invalid photo rotation: {rotation}")


def corners_to_rotated_preview(points: list[list[float]], width: int, height: int,
                               rotation: str) -> list[list[float]]:
    """Show raw board corners in top-left-first order on the rotated preview."""
    if len(points) != 4:
        raise ValueError("Four raw photo corners are required.")
    order = {"none": (0, 1, 2, 3), "cw": (3, 0, 1, 2),
             "180": (2, 3, 0, 1), "ccw": (1, 2, 3, 0)}.get(rotation)
    if order is None:
        raise ValueError(f"Invalid photo rotation: {rotation}")
    return [list(rotate_photo_point(*points[index], width, height, rotation))
            for index in order]


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


@dataclass(frozen=True)
class CheckNotice:
    king: str
    attackers: tuple[str, ...]
    title: str
    detail: str


def check_notice(board: chess.Board) -> CheckNotice | None:
    """Describe the actual checking pieces, including discovered and double check."""
    if not board.is_check() or board.is_checkmate():
        return None
    king = board.king(board.turn)
    if king is None:
        return None
    color = "White" if board.turn else "Black"
    opponent = "Black" if board.turn else "White"
    attackers = tuple(chess.square_name(square) for square in board.checkers())
    pieces = [f"{opponent} {chess.piece_name(board.piece_at(chess.parse_square(square)).piece_type)} on {square}"
              for square in attackers]
    king_name = chess.square_name(king)
    title = "Your king is in check" if board.turn else "Black is in check"
    if len(attackers) > 1:
        title = "Double check" if board.turn else "Black is in double check"
    return CheckNotice(king_name, attackers, title,
                       f"{color} king on {king_name} is attacked by " + " and ".join(pieces) + ".")


def consensus_observation(samples: list[Observation]) -> Observation:
    """Require two clear votes per square; a lone photo cannot verify a move."""
    if len(samples) < 2:
        raise ValueError("Two photos are required to verify a scan.")
    occupied, uncertain, scores = set(), [], {}
    for square in chess.SQUARE_NAMES:
        present = sum(square in sample.occupied for sample in samples
                      if square not in sample.uncertain)
        absent = sum(square not in sample.occupied for sample in samples
                     if square not in sample.uncertain)
        if present >= 2 and present > absent:
            occupied.add(square)
        elif absent < 2 or absent <= present:
            uncertain.append(square)
        values = [sample.scores[square] for sample in samples if square in sample.scores]
        if values:
            scores[square] = float(np.median(values))
    confidence = {square: (max(sum(square in sample.occupied and square not in sample.uncertain
                                   for sample in samples),
                               sum(square not in sample.occupied and square not in sample.uncertain
                                   for sample in samples)) / len(samples))
                  for square in chess.SQUARE_NAMES}
    return Observation(frozenset(occupied), tuple(uncertain), scores, confidence)


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


def capture_destination_changed(board: chess.Board, move: chess.Move,
                                scores: dict[str, float] | None) -> bool:
    """A capture must replace the destination piece, not only remove its source."""
    if scores is None or not board.is_capture(move) or board.is_en_passant(move):
        return True
    destination = chess.square_name(move.to_square)
    _, affected = _after_move(board, move)
    background = [value for square, value in scores.items() if square not in affected]
    noise = float(np.percentile(background, 90)) if background else 0.0
    return scores.get(destination, 0.0) >= max(8.0, noise + 5.0)


def _temporal_match(board: chess.Board, move: chess.Move,
                    observation: TemporalObservation) -> bool:
    """Require every changed square to stand above every unchanged square."""
    _, affected = _after_move(board, move)
    scores = observation.scores
    signal = min(scores[square] for square in affected)
    background = max(score for square, score in scores.items() if square not in affected)
    baseline = float(np.median(list(scores.values())))
    return signal >= max(12.0, baseline + 8.0) and signal - background >= 8.0


def _temporal_as_occupancy(board: chess.Board,
                           observation: TemporalObservation) -> Observation:
    before = occupied_squares(board)
    return Observation(before.symmetric_difference(observation.changed), (), observation.scores)


def temporal_move_feedback(board: chess.Board, observation: TemporalObservation,
                           reason: str) -> ScanFeedback:
    if not observation.changed:
        return ScanFeedback("NO CLEAR MOVE", "Leave your move on the board. Center the piece on its square, move your hands away, then scan again.")
    if len(observation.changed) > 8:
        return ScanFeedback("PHOTO UNCLEAR", "Many squares changed. Check the photo corners and lighting.")
    before = occupied_squares(board)
    sources = tuple(square for square in observation.changed if square in before)
    destinations = tuple(square for square in observation.changed if square not in before)
    if len(sources) == 1 and len(destinations) == 1:
        return rejected_move_feedback(board, _temporal_as_occupancy(board, observation), reason)
    if len(sources) >= 2 and len(destinations) >= 2:
        return rejected_move_feedback(board, _temporal_as_occupancy(board, observation), reason)
    return ScanFeedback(
        "MOVE NOT VERIFIED", "Camera sees changes on " + ", ".join(observation.changed) +
        ". It cannot determine one legal move from these squares. Check the board and rescan.",
        sources, destinations)


def rejected_move_feedback(board: chess.Board, observation: Observation,
                           reason: str) -> ScanFeedback:
    """Name changed pieces without guessing which destination belongs to each."""
    if "No move detected" in reason:
        return ScanFeedback("NO CLEAR MOVE", "Leave your move on the board. Center the piece on its square and remove your hands, then scan again.")
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
                notice = check_notice(board)
                if notice is not None and piece.color == board.turn:
                    detail += f" Your king on {notice.king} must be protected. Restore the piece and choose a move that ends the check."
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


def temporal_computer_move_feedback(board: chess.Board, move: chess.Move,
                                    observation: TemporalObservation) -> ScanFeedback:
    """Describe visual changes without treating occupied capture squares as empty."""
    source = chess.square_name(move.from_square)
    destination = chess.square_name(move.to_square)
    piece = board.piece_at(move.from_square)
    name = chess.piece_name(piece.piece_type) if piece else "piece"
    expected = f"Expected: Black {name} {source} → {destination}. "
    before = occupied_squares(board)
    changed = observation.changed
    sources = tuple(square for square in changed if square in before)
    new_squares = tuple(square for square in changed if square not in before)
    if len(sources) == 1 and len(new_squares) == 1:
        actual_piece = board.piece_at(chess.parse_square(sources[0]))
        actual_name = (f"{'White' if actual_piece.color else 'Black'} "
                       f"{chess.piece_name(actual_piece.piece_type)}" if actual_piece else "piece")
        seen = f"Camera sees: {actual_name} {sources[0]} → {new_squares[0]}. "
    elif changed:
        seen = "Camera sees changes on " + ", ".join(changed) + ". "
        seen += "The photo does not show one clear source and destination. "
    else:
        seen = "Camera sees no clear square changes. "
    return ScanFeedback(
        "COMPUTER MOVE MISMATCH", expected + seen + "Correct the physical board and scan again.",
        sources, new_squares, (source,), (destination,))


def should_retry_scan(feedback: ScanFeedback, corner_source: str, attempts: int) -> bool:
    """Retry uncertain captures, while showing clear rule violations immediately."""
    return (corner_source != "manual" and attempts < MAX_SCAN_ATTEMPTS and
            feedback.title in ("NO CLEAR MOVE", "PHOTO UNCLEAR", "MOVE NOT VERIFIED",
                               "SETUP NOT VERIFIED", "COMPUTER MOVE MISMATCH"))


class CalibrationFlow:
    """Touch calibration steps; photos are processed off the Tk main thread."""

    CORNER_NAMES = ("top-left", "top-right", "bottom-right", "bottom-left")
    A1_CORNERS = (("none", "bottom left"), ("cw", "bottom right"),
                  ("180", "top right"), ("ccw", "top left"))

    def __init__(self, *, setup_only: bool = False) -> None:
        self.setup_only = setup_only
        self.rotation = "180" if setup_only else "none"
        self.suggested_rotation: str | None = None
        self.stage = "setup_only" if setup_only else "empty"
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
        if self.stage not in ("starting", "setup_only"):
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
        if self.starting_photo is not None:
            self.starting_corners = []
            self.starting_auto_corners = False
            self.suggested_rotation = None
            self.stage = "starting_corners"

    def cycle_rotation(self) -> None:
        rotations = [rotation for rotation, _ in self.A1_CORNERS]
        self.rotation = rotations[(rotations.index(self.rotation) + 1) % len(rotations)]

    def flip_rotation(self) -> None:
        self.rotation = {"none": "180", "180": "none", "cw": "ccw", "ccw": "cw"}[self.rotation]

    def retake_starting(self) -> None:
        self.starting_photo = None
        self.starting_corners = []
        self.starting_auto_corners = False
        self.suggested_rotation = None
        self.stage = "setup_only" if self.setup_only else "starting"

    def white_side_in_preview(self) -> str | None:
        if self.suggested_rotation is None:
            return None
        rotations = [rotation for rotation, _ in self.A1_CORNERS]
        turns = (rotations.index(self.rotation) -
                 rotations.index(self.suggested_rotation)) % 4
        return ("bottom", "left", "top", "right")[turns]


def calibrate_photos(empty: np.ndarray, starting: np.ndarray,
                     empty_corners: list[list[float]],
                     starting_corners: list[list[float]],
                     adjustments: PhotoAdjustments = PhotoAdjustments(),
                     rotation: str = "none"):
    """Build and verify calibration before writing it to disk."""
    adjusted_starting = adjust_photo(starting, adjustments)
    calibration, reference, _scores = calibrate(
        adjust_photo(empty, adjustments), adjusted_starting, empty_corners,
        rotation, starting_corners=starting_corners)
    observation, _aligned = observe(reference, adjusted_starting, calibration, starting_corners)
    if observation.uncertain or observation.occupied != occupied_squares(chess.Board()):
        raise VisionError("Starting board could not be verified. Check the pieces, corners, and lighting.")
    return calibration, reference


def starting_position_unchanged(reference: np.ndarray,
                                photos: list[np.ndarray | None]) -> bool:
    """Confirm a saved starting setup from its actual photo, including two scans."""
    if len(photos) < 2 or any(photo is None for photo in photos):
        return False
    try:
        return all(max(robust_square_scores(reference, photo).values()) < 8.0
                   for photo in photos if photo is not None)
    except VisionError:
        return False


def aligned_setup_photo(starting: np.ndarray, corners: list[list[float]],
                        adjustments: PhotoAdjustments, rotation: str) -> np.ndarray:
    """Show the same adjusted and oriented image that move detection will use."""
    return rotate_board(warp_board(adjust_photo(starting, adjustments), corners), rotation)


def suggest_setup_rotation(starting: np.ndarray, corners: list[list[float]],
                           adjustments: PhotoAdjustments) -> str | None:
    """Suggest which rotation puts the lighter starting pieces at the bottom."""
    aligned = warp_board(adjust_photo(starting, adjustments), corners)
    lightness = cv2.cvtColor(aligned, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    contrast = np.empty((8, 8), dtype=np.float32)
    for row in range(8):
        for col in range(8):
            patch = lightness[row * 100:(row + 1) * 100, col * 100:(col + 1) * 100]
            bare = np.concatenate((patch[:15, :15].ravel(), patch[:15, -15:].ravel(),
                                   patch[-15:, :15].ravel(), patch[-15:, -15:].ravel()))
            contrast[row, col] = float(np.median(patch[30:70, 30:70]) - np.median(bare))
    rotations = (("none", 0), ("cw", -1), ("180", 2), ("ccw", 1))
    ranked = sorted(((float(np.mean(np.rot90(contrast, k)[-2:]) -
                            np.mean(np.rot90(contrast, k)[:2])), rotation)
                     for rotation, k in rotations), reverse=True)
    return ranked[0][1] if ranked[0][0] >= 12 and ranked[0][0] - ranked[1][0] >= 8 else None


def calibrate_setup_photo(starting: np.ndarray, corners: list[list[float]],
                          adjustments: PhotoAdjustments,
                          rotation: str = "none") -> tuple[Calibration, np.ndarray]:
    """Use the confirmed starting position as the first move reference."""
    aligned = aligned_setup_photo(starting, corners, adjustments, rotation)
    return Calibration(corners, (0.0, 0.0), (0.0, 0.0), rotation,
                       scoring="temporal_v1"), aligned


class TouchSession:
    """Rules and camera match decisions, independent of the display and hardware.

    With a gantry (`GantryPlayer`, injected) the session also keeps the gantry's picture of the
    board in step and lets it carry Stockfish's moves; see "the gantry" below. Without a gantry
    (gantry=None) nothing here changes: the person moves Black's pieces by hand.
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
        self.layout_known = False               # the gantry was told the verified start position
        self.gantry_note: str | None = None     # why the gantry could not carry the current computer move
        self.gantry_notes: tuple[str, ...] = () # what the gantry reported about the move it carried
        # (square, piece symbol) the person must take off before the gantry plays the computer move.
        self.removal: tuple[str, str] | None = None

    def verify_setup(self, observed: Observation | frozenset[str]) -> None:
        if self.phase != "setup":
            raise ValueError("Setup is not waiting for a scan.")
        observation = _checked_observation(observed)
        if not _matches_known_squares(occupied_squares(self.game.board), observation):
            expected = occupied_squares(self.game.board)
            unclear = set(observation.uncertain)
            missing = sorted(expected - observation.occupied - unclear)
            extra = sorted(observation.occupied - expected - unclear)
            details = []
            if missing:
                details.append("Place pieces on " + ", ".join(missing) + ".")
            if extra:
                details.append("Clear " + ", ".join(extra) + ".")
            raise MoveRejected(" ".join(details) + " Center the pieces and scan again.")
        self.setup_uncertain = observation.uncertain
        self.phase = "setup_confirm" if observation.uncertain else "human"
        self.gantry_start_known()

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

    def scan_human(self, observed: Observation | frozenset[str], *,
                   change_scores: dict[str, float] | None = None) -> str | None:
        if self.phase != "human":
            raise ValueError("It is not your turn to scan a move.")
        observation = _checked_observation(observed)
        before = occupied_squares(self.game.board)
        if _matches_known_squares(before, observation):
            raise MoveRejected("No move detected. Move a white piece, then scan again.")
        candidates = []
        blocked = set()
        unverified_captures = set()
        for move in self.game.board.legal_moves:
            after, affected = _after_move(self.game.board, move)
            if not _matches_known_squares(after, observation):
                continue
            unclear_affected = set(observation.uncertain) & affected
            if change_scores is not None:
                background = [score for square, score in change_scores.items() if square not in affected]
                changed_cutoff = max(8.0, float(np.percentile(background, 90)) + 5.0)
                unclear_affected.update(square for square in observation.uncertain
                                        if change_scores.get(square, 0.0) >= changed_cutoff)
            if unclear_affected:
                blocked.update(unclear_affected)
            elif not capture_destination_changed(self.game.board, move, change_scores):
                unverified_captures.add(chess.square_name(move.to_square))
            else:
                candidates.append(move)
        if not candidates and blocked:
            raise MoveRejected("Move touches unclear squares: " + ", ".join(sorted(blocked)) +
                               ". Retake the photo or adjust the corners.")
        if not candidates and unverified_captures:
            raise MoveRejected("Capture not verified on " + ", ".join(sorted(unverified_captures)) +
                               ". Remove the opponent's piece and place your piece on that square, then scan again.")
        if not candidates:
            raise MoveRejected("Illegal or unrecognized move. Correct the board and scan again.")
        if len(candidates) > 1:
            self.choices = tuple(candidates)
            self.phase = "choose"
            return None
        return self._accept_human(candidates[0])

    def scan_human_temporal(self, observed: TemporalObservation) -> str | None:
        if self.phase != "human":
            raise ValueError("It is not your turn to scan a move.")
        candidates = tuple(move for move in self.game.board.legal_moves
                           if _temporal_match(self.game.board, move, observed))
        if not candidates:
            raise MoveRejected("No clear move detected." if not observed.changed else
                               "Move could not be matched to one legal move.")
        if len(candidates) > 1:
            self.choices = candidates
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
        board_before = self.game.board.copy(stack=False)
        san = self.game.play_human(
            chess.square_name(move.from_square),
            chess.square_name(move.to_square),
            chess.piece_symbol(move.promotion) if move.promotion else None,
        )
        self._gantry_follow_human(board_before, move)
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "thinking"
        return san

    def announce_computer(self, move: chess.Move) -> str:
        if self.phase != "thinking" or move not in self.game.board.legal_moves:
            raise ValueError("The computer did not provide a legal move.")
        self.computer_move = move
        self.phase = "computer"
        self._gantry_check_removal(move)
        return self.game.board.san(move)

    def verify_computer(self, observed: Observation | frozenset[str], *,
                        change_scores: dict[str, float] | None = None) -> str:
        if self.phase != "computer" or self.computer_move is None:
            raise ValueError("There is no computer move to verify.")
        observation = _checked_observation(observed)
        if not capture_destination_changed(self.game.board, self.computer_move, change_scores):
            raise MoveRejected("The capture destination has not changed. Complete the computer's capture, then scan again.")
        after, affected = _after_move(self.game.board, self.computer_move)
        unclear_affected = set(observation.uncertain) & affected
        if change_scores is not None:
            background = [score for square, score in change_scores.items() if square not in affected]
            changed_cutoff = max(8.0, float(np.percentile(background, 90)) + 5.0)
            unclear_affected.update(square for square in observation.uncertain
                                    if change_scores.get(square, 0.0) >= changed_cutoff)
        if unclear_affected:
            raise MoveRejected("Computer move touches unclear squares: " +
                               ", ".join(sorted(unclear_affected)) + ". Retake the photo.")
        if not _matches_known_squares(after, observation):
            raise MoveRejected("Board does not match the computer move. Correct it and scan again.")
        san = self.game.play_computer(self.computer_move)
        self._gantry_after_computer()
        self.computer_move = None
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "human"
        return san

    def verify_computer_temporal(self, observed: TemporalObservation) -> str:
        if self.phase != "computer" or self.computer_move is None:
            raise ValueError("There is no computer move to verify.")
        if not _temporal_match(self.game.board, self.computer_move, observed):
            raise MoveRejected("Board does not match the computer move. Correct it and scan again.")
        san = self.game.play_computer(self.computer_move)
        self._gantry_after_computer()
        self.computer_move = None
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "human"
        return san

    def resign(self) -> None:
        if self.phase != "finished":
            self.result = "You ended the game. Stockfish wins."
            self.phase = "finished"

    # ------------------------------------------------------------------ the gantry
    # The gantry follows the game: the start position once it is verified, the person's move once
    # the camera registered it, and Stockfish's move, which it carries itself. The gantry never takes
    # a captured piece off the board: when Stockfish captures, the session waits in the "remove"
    # phase until the camera has seen the person take the piece off (`verify_removal`), and only then
    # does the gantry carry the capturing move. Every gantry fault leaves the move to the person.

    def gantry_start_known(self) -> None:
        """The pieces stand in the game's position (verified by the camera or by the calibration)."""
        if self.gantry is not None:
            self.gantry.new_game(self.game.board.copy(stack=False))
            self.layout_known = True

    def _gantry_follow_human(self, board_before: chess.Board, move: chess.Move) -> None:
        if self.gantry is None:
            return
        try:
            self.gantry.human_moved(board_before, move)
        except GantryError:
            # The gantry's picture went out of step: it follows the camera-verified board instead.
            self.gantry.new_game(self.game.board.copy(stack=False))

    def _gantry_check_removal(self, move: chess.Move) -> None:
        """Announcing Stockfish's move: does the person have to take a captured piece off first?"""
        self.removal = None
        self.gantry_note = None
        self.gantry_notes = ()
        if self.gantry is None:
            return
        board = self.game.board.copy(stack=False)
        removal_needed = getattr(self.gantry, "removal_needed", None)
        if removal_needed is None:
            if board.is_capture(move):   # an old pi_gantry would take the piece off itself: not allowed
                self.gantry_failed("This gantry software cannot wait for a captured piece to be taken off.")
            return
        try:
            removal = removal_needed(board, move)
        except (GantryError, ValueError, KeyError) as exc:
            self.gantry_failed(f"The gantry cannot plan {move.uci()}: {exc}")
            return
        if removal:
            self.removal = (str(removal[0]), str(removal[1]))
            self.phase = "remove"

    def removal_text(self) -> str:
        """"the white bishop off d5": what the person must take off before the gantry plays."""
        if self.removal is None:
            raise ValueError("No piece is waiting to be taken off the board.")
        square, symbol = self.removal
        piece = chess.Piece.from_symbol(symbol)
        color = "white" if piece.color == chess.WHITE else "black"
        return f"the {color} {chess.piece_name(piece.piece_type)} off {square}"

    def verify_removal(self, observed: Observation | frozenset[str]) -> str:
        """Check in the "remove" phase: the camera must see the captured piece's square empty and every
        other square as it is. Returns the square and moves on to "computer". Raises MoveRejected."""
        if self.phase != "remove" or self.removal is None or self.computer_move is None:
            raise ValueError("No piece is waiting to be taken off the board.")
        observation = _checked_observation(observed)
        square = self.removal[0]
        if square in observation.uncertain:
            raise MoveRejected(f"The camera cannot tell whether {square} is empty. Retake the photo.")
        expected = occupied_squares(self.game.board) - {square}
        if not _matches_known_squares(expected, observation):
            unclear = set(observation.uncertain)
            missing = sorted(expected - observation.occupied - unclear)
            extra = sorted(observation.occupied - expected - unclear)
            if extra == [square] and not missing:
                raise MoveRejected(f"{square} still has a piece on it. Take {self.removal_text()}, then tap Check.")
            parts = (["missing on " + ", ".join(missing)] if missing else []) + \
                    (["extra on " + ", ".join(extra)] if extra else [])
            raise MoveRejected("The board does not match the game: " + "; ".join(parts) +
                               f". Put the pieces back, take {self.removal_text()}, then tap Check.")
        self.phase = "computer"
        return square

    def verify_removal_temporal(self, observed: TemporalObservation) -> str:
        """verify_removal for temporal calibrations: only the captured piece's square may have changed."""
        if self.phase != "remove" or self.removal is None or self.computer_move is None:
            raise ValueError("No piece is waiting to be taken off the board.")
        square = self.removal[0]
        scores = observed.scores
        signal = scores.get(square, 0.0)
        background = max((score for name, score in scores.items() if name != square), default=0.0)
        baseline = float(np.median(list(scores.values()))) if scores else 0.0
        if not (signal >= max(12.0, baseline + 8.0) and signal - background >= 8.0):
            raise MoveRejected(f"The camera does not see {square} emptied. Take {self.removal_text()}, then tap Check.")
        self.phase = "computer"
        return square

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
        self.gantry_notes = tuple(str(note) for note in (notes or ()))

    def gantry_failed(self, message: str) -> None:
        """The gantry could not carry the move: the person makes it by hand, as without a gantry."""
        self.gantry_note = message
        self.gantry_notes = ()

    def _gantry_after_computer(self) -> None:
        if self.gantry is not None and self.gantry_note is not None:
            # The person made the move by hand: the gantry's picture follows the camera-verified board.
            self.gantry.new_game(self.game.board.copy(stack=False))
            self.gantry_note = None
        self.removal = None

    def put_pieces_back(self, say=None) -> list[str]:
        """Worker thread: every piece goes back home, if the gantry knows where they are. Returns
        the notes for the pieces the person must put back (the captured ones). Raises GantryError."""
        if self.gantry is None or not self.layout_known:
            return []
        return list(self.gantry.reset(say) or ())

    def winner_text(self) -> str:
        outcome = self.game.board.outcome(claim_draw=True)
        if outcome is None:
            return "Stockfish wins · Black"
        if outcome.winner is chess.WHITE:
            return "You win! · White"
        if outcome.winner is chess.BLACK:
            return "Stockfish wins · Black"
        return "Draw"


class TouchApp:
    BG = "#0c1c1b"
    PANEL = "#16302d"
    TEXT = "#f5efdf"
    MUTED = "#aec1b7"
    GREEN = "#a1dbb5"
    RED = "#ffa59b"
    GOLD = "#eac784"

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
        self.calibration_flow: CalibrationFlow | None = None
        self.startup_calibration = False
        self.welcome_screen = True
        self.pending_game_start = False
        self.adjustments_open = False
        self.confirming_end = False
        self.confirming_new_game = False
        self.calibration_error: str | None = None
        self.scan_photo: np.ndarray | None = None
        self.scan_corners: list[list[float]] = []
        self.last_scan_corners: list[list[float]] | None = None
        self.scan_corner_source = ""
        self.scan_feedback: ScanFeedback | None = None
        self.capture_error: str | None = None
        self.scan_attempts = 0
        self.scan_samples: list[Observation] = []
        self.scan_aligned_samples: list[np.ndarray | None] = []
        self.scan_confirmations_active = False
        self.latest_observation: Observation | None = None
        self.position_reference: np.ndarray | None = None
        self.fresh_starting_reference: np.ndarray | None = None
        self.saved_starting_reference: np.ndarray | None = None
        self.pending_position_reference: np.ndarray | None = None
        self.pending_reference: np.ndarray | None = None
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_geometry: tuple[float, float, float] | None = None
        self.level = "beginner"
        self.busy = False
        self.selected_square: str | None = None
        self.message = "Welcome. Tap Play Chess to begin."
        self.message_color = self.MUTED
        # The gantry (pi_gantry, when CHESS_GANTRY_PORT is set) is opened at the first game and closed with the app.
        self.gantry = None
        self.gantry_activity: str | None = None   # heading while the gantry works ("Gantry moving e7e5…")
        self.resetting = False                    # the gantry is carrying every piece home
        # What the gantry's reset left for the person ("Put a black pawn on e7."): the pieces taken during the
        # game go back by hand, so the notes stay on the set-up screen until the starting board is verified.
        self.reset_notes: tuple[str, ...] = ()
        # The Gantry screen (open_gantry_panel): a full-screen layer over the game for jogging, centering and homing.
        self.gantry_screen = None                 # its tk.Frame while it is open
        self.gantry_screen_owned = False          # it opened the gantry itself (no game had): close it on Back
        self.gantry_step = "5"                    # jog step: "1", "5" (mm) or "square"
        self.gantry_confirm = False               # asking "Make this spot home?"
        self.gantry_readout: dict | None = None   # the last status the gantry reported
        self.gantry_problem: str | None = None    # why there is no gantry to control
        self.gantry_done = None                   # words for the screen once the running gantry action worked
        screen_width = 800 if args.windowed else root.winfo_screenwidth()
        screen_height = 480 if args.windowed else root.winfo_screenheight()
        self.compact = screen_width < 900 or screen_height < 600
        self.screen_width = screen_width
        self.panel_width = 252 if self.compact else 340

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

        self.header = tk.Label(root, text="♟  PHYSICAL CHESS", bg=self.BG, fg=self.TEXT,
                               font=("DejaVu Sans", 12 if self.compact else 17, "bold"),
                               anchor="w", padx=12 if self.compact else 18)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.board_canvas = tk.Canvas(root, bg=self.BG, highlightthickness=0)
        self.board_canvas.grid(row=1, column=0, sticky="nsew", padx=(12, 4), pady=8)
        self.board_canvas.bind("<Configure>", lambda _event: self.draw_board())
        self.board_canvas.bind("<Button-1>", self.tap_board)
        self.panel_shell = tk.Frame(root, bg=self.PANEL, width=self.panel_width)
        self.panel_shell.grid(row=1, column=1, sticky="nsew",
                              padx=(2, 10), pady=8)
        self.panel_shell.grid_propagate(False)
        self.panel_canvas = tk.Canvas(self.panel_shell, bg=self.PANEL,
                                      highlightthickness=0, width=self.panel_width - 20)
        self.panel_scrollbar = tk.Scrollbar(self.panel_shell, orient="vertical",
                                            command=self.panel_canvas.yview, width=16)
        self.panel_canvas.configure(yscrollcommand=self.panel_scrollbar.set)
        self.panel_scrollbar.pack(side="right", fill="y")
        self.panel_canvas.pack(side="left", fill="both", expand=True)
        self.panel = tk.Frame(self.panel_canvas, bg=self.PANEL,
                              padx=10 if self.compact else 18, pady=12 if self.compact else 20)
        panel_window = self.panel_canvas.create_window(0, 0, window=self.panel, anchor="nw")
        self.panel.bind("<Configure>", lambda _event: self.panel_canvas.configure(
            scrollregion=self.panel_canvas.bbox("all")))
        self.panel_canvas.bind("<Configure>", lambda event: self.panel_canvas.itemconfigure(
            panel_window, width=event.width))
        self.panel_canvas.bind("<MouseWheel>", lambda event: self.panel_canvas.yview_scroll(
            -1 if event.delta > 0 else 1, "units"))
        self.status = tk.Label(root, text=self.message, bg=self.PANEL, fg=self.message_color,
                               font=("DejaVu Sans", 10 if self.compact else 12), height=2,
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
                         height=1 if short else 2, pady=7 if short else 5,
                         font=("DejaVu Sans", 10 if self.compact else 12, "bold"),
                         bd=0, relief="flat",
                         bg=self.GREEN if accent else "#294943",
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
                 anchor="w", justify="left", wraplength=self.panel_width - 48).pack(fill="x", pady=(0, 8))
        if detail:
            tk.Label(self.panel, text=detail, bg=self.PANEL, fg=self.MUTED,
                     font=("DejaVu Sans", 10 if self.compact else 11), anchor="w", justify="left",
                     wraplength=self.panel_width - 48).pack(fill="x", pady=(0, 12))

    def render(self) -> None:
        for child in self.panel.winfo_children():
            child.destroy()
        if hasattr(self, "panel_canvas"):
            self.panel_canvas.yview_moveto(0)
        self.draw_board()
        if getattr(self, "welcome_screen", False) and self.calibration_flow is None:
            self.heading("Ready to play?", "You play White on the physical board.")
            self.button("Start Game", self.begin_play, accent=True)
            self.button("Calibrate Board", self.open_board_setup, short=True)
            self.button("Gantry", self.open_gantry_panel, short=True)
            return
        if getattr(self, "confirming_end", False) and self.session is not None:
            self.heading("End this game?", "Ending the game counts as a resignation. Your board setup will stay saved.")
            self.button("Keep Playing", self.cancel_end_game, accent=True)
            self.button("End Game", self.end_game, short=True)
            return
        if getattr(self, "confirming_new_game", False) and self.session is not None:
            self.heading("Start a new game?", "This will end the current game and return to difficulty selection.")
            self.button("Keep Playing", self.cancel_new_game, accent=True)
            self.button("Start New Game", self.back_to_menu, short=True)
            return
        if self.busy and getattr(self, "gantry_activity", None):
            self.heading(self.gantry_activity, "Keep hands clear of the board.")
            if self.resetting:
                self.button("New Game", self.back_to_menu, short=True)
            else:
                self.button("End Game", self.request_end_game, short=True)
            return
        if self.busy:
            if self.session is not None and self.session.phase == "thinking":
                self.render_check_notice()
            self.heading("Checking the board" if self.scan_photo is not None else "One moment…",
                         "Keep the board still and your hands away while the photos are checked." if
                         getattr(self, "scan_confirmations_active", False) else self.message)
            self.button("Cancel Calibration" if self.calibration_flow else "End Game",
                        self.cancel_calibration if self.calibration_flow else self.request_end_game, short=True)
            return
        if self.calibration_flow is not None:
            self.render_calibration()
            return
        session = self.session
        if self.scan_photo is not None:
            self.render_scan_review()
            return
        if session is not None and getattr(self, "capture_error", None):
            self.heading("Camera photo failed")
            tk.Label(self.panel, text=self.capture_error, bg="#8a2f32", fg=self.TEXT,
                     font=("DejaVu Sans", 10 if self.compact else 12), justify="left",
                     wraplength=self.panel_width - 48, padx=8, pady=12).pack(fill="x", pady=(0, 10))
            self.button("Try Camera Again", self.scan, accent=True)
            self.button("Cancel Game", self.end_game, short=True)
            return
        if session is None:
            self.heading("Choose your challenge", "You play White. Choose a level, then start your game.")
            for key, difficulty in DIFFICULTIES.items():
                label = ("●  " if key == self.level else "○  ") + difficulty.label
                self.button(label, lambda selected=key: self.choose_level(selected), short=True)
            self.button("Start Game" if self.saved_calibration_available() else "Set Up Board  →",
                        self.start_game, accent=True)
            self.button("Recalibrate Board" if self.saved_calibration_available() else "Back to Welcome",
                        self.open_board_setup if self.saved_calibration_available() else self.go_home, short=True)
            self.button("Gantry", self.open_gantry_panel, short=True)
        elif session.phase == "setup":
            self.heading("Ready to play?", self.reset_notes_text() +
                         "Place all 32 pieces as shown. Center them on their squares, then lift your hands away.")
            self.button("Scan Starting Board", self.scan, accent=True)
            self.button("Cancel Game", self.end_game)
        elif session.phase == "setup_confirm":
            squares = ", ".join(session.setup_uncertain)
            self.heading("Check unclear squares", f"Confirm {squares} match the displayed starting board.")
            self.button("Squares Match", self.confirm_setup, accent=True)
            self.button("Retake Photo", self.retry_setup_scan)
            self.button("Cancel Game", self.end_game)
        elif session.phase == "human":
            in_check = self.render_check_notice()
            if in_check:
                if len(session.game.board.checkers()) > 1:
                    self.heading("Move your king", "Move your king to a safe square to escape both attacks. Then tap Scan Move.")
                else:
                    self.heading("Protect your king", "Move the king, capture the attacker, or block the attack if legal. Then tap Scan Move.")
            else:
                self.heading("Your turn", "Move one White piece. Lift your hands away, then tap Scan Move. The app checks two photos automatically.")
            self.button("Scan Move", self.scan, accent=True)
            self.button("End Game", self.request_end_game, short=True)
            if in_check:
                self.button("New Game", self.request_new_game, short=True)
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
            self.button("End Game", self.request_end_game, short=True)
        elif session.phase == "thinking":
            self.render_check_notice()
            self.heading("Stockfish is thinking", "Leave the physical board as it is. Your next instruction will appear here.")
            self.button("Try Computer Move", self.request_computer_move, accent=True)
            self.button("End Game", self.request_end_game, short=True)
        elif session.phase == "remove":
            # Stockfish captures with the gantry: the person takes the piece off, Check has the camera
            # confirm it, and only then does the gantry carry the move.
            self.render_check_notice()
            self.heading("Remove the piece", self.removal_instruction())
            self.button("Check", self.scan, accent=True)
            self.button("End Game", self.request_end_game, short=True)
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
            self.render_check_notice()
            by_hand = "Move the Black piece as shown, then remove your hands. " + " ".join(extra)
            if session.gantry_note:
                by_hand = f"The gantry could not make this move: {session.gantry_note} " + by_hand
            self.heading("Computer's move", by_hand)
            tk.Label(self.panel, text=f"{move.uci()[:2]}  →  {move.uci()[2:4]}", bg=self.PANEL,
                     fg=self.GOLD, font=("DejaVu Sans", 23 if self.compact else 30, "bold")).pack(fill="x", pady=(0, 12))
            self.button("Verify Computer Move", self.scan, accent=True)
            self.button("End Game", self.request_end_game, short=True)
        else:
            self.heading("Checkmate" if session.game.board.is_checkmate() else "Game over",
                         session.winner_text())
            self.button("New Game", self.back_to_menu, accent=True)
            self.button("Gantry", self.open_gantry_panel, short=True)

    def render_check_notice(self) -> bool:
        notice = check_notice(self.session.game.board) if self.session is not None else None
        if notice is None:
            return False
        tk.Label(self.panel, text=notice.title.upper(), bg="#8a2f32", fg=self.TEXT,
                 font=("DejaVu Sans", 12 if self.compact else 16, "bold"),
                 wraplength=self.panel_width - 48, pady=8).pack(fill="x", pady=(0, 8))
        tk.Label(self.panel, text=notice.detail, bg=self.PANEL, fg=self.TEXT,
                 font=("DejaVu Sans", 10 if self.compact else 12), justify="left",
                 wraplength=self.panel_width - 48).pack(fill="x", pady=(0, 10))
        return True

    def begin_play(self) -> None:
        self.welcome_screen = False
        self.pending_game_start = False
        self.say("Choose a difficulty to begin.")
        self.render()

    def go_home(self) -> None:
        self.welcome_screen = True
        self.pending_game_start = False
        self.fresh_starting_reference = None
        self.calibration_flow = None
        self.say("Welcome. Tap Play Chess to begin.")
        self.render()

    def open_board_setup(self) -> None:
        self.pending_game_start = False
        self.start_calibration()

    def request_end_game(self) -> None:
        if self.session is None or self.session.phase == "setup":
            self.end_game()
            return
        self.confirming_end = True
        self.render()

    def cancel_end_game(self) -> None:
        self.confirming_end = False
        self.render()

    def request_new_game(self) -> None:
        self.confirming_new_game = True
        self.render()

    def cancel_new_game(self) -> None:
        self.confirming_new_game = False
        self.render()

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
                     font=("DejaVu Sans", 10 if self.compact else 12),
                     justify="left", wraplength=self.panel_width - 48).pack(fill="x", pady=(0, 12))
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
            action = ("Check Again" if feedback.title == "REMOVE THE PIECE" else
                      "Correct Move & Verify" if feedback.title == "COMPUTER MOVE MISMATCH" else
                      "Fix Board & Scan Again" if feedback.title in
                      ("ILLEGAL MOVE", "MULTIPLE PIECES CHANGED") else
                      "Check Move & Rescan" if feedback.title == "NO CLEAR MOVE" else "Retake Photo")
            self.button(action, self.retake_scan, accent=True)
            self.button("Adjust Photo Corners", self.redo_scan_corners, short=True)
            self.button("End Game", self.request_end_game, short=True)
            return
        if len(self.scan_corners) < 4:
            corner = CalibrationFlow.CORNER_NAMES[len(self.scan_corners)]
            self.heading("Align this photo", f"Tap the {corner} corner of the 8 × 8 playing area. Leave the wooden border outside.")
            self.button("Undo Corner", self.undo_scan_corner)
            if self.last_scan_corners is not None:
                self.button("Use Saved Corners", self.use_saved_scan_corners)
        else:
            self.heading("Corners ready", "The four points should enclose only the 8 × 8 playing area.")
            self.button("Check Photo", self.process_scan_photo, accent=True)
            self.button("Redo Corners", self.redo_scan_corners)
        self.button("Retake Photo", self.retake_scan)
        self.button("End Game", self.request_end_game, short=True)

    def render_calibration(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        if getattr(self, "calibration_error", None):
            tk.Label(self.panel, text="SETUP NEEDS ATTENTION", bg="#8a2f32", fg=self.TEXT,
                     font=("DejaVu Sans", 11, "bold"), wraplength=self.panel_width - 48,
                     pady=8).pack(fill="x", pady=(0, 6))
            tk.Label(self.panel, text=self.calibration_error, bg=self.PANEL, fg=self.RED,
                     font=("DejaVu Sans", 10), wraplength=self.panel_width - 48,
                     justify="left").pack(fill="x", pady=(0, 10))
        if flow.stage == "setup_only":
            self.heading("Starting board", "Set up all 32 pieces. The preview will show which edge the camera treats as White's side.")
            self.button("Take Setup Photo", self.capture_starting, accent=True)
            if self.saved_calibration_available():
                self.button("Use Saved Calibration", self.use_saved_calibration)
            if not self.startup_calibration:
                self.button("Back to Menu", self.cancel_calibration)
        elif flow.stage == "empty":
            self.heading("1 / 3 · Empty board", "Remove every piece from the board. Keep the camera fixed and lift your hands away.")
            self.button("Take Empty Photo", self.capture_empty, accent=True)
            if self.saved_calibration_available():
                self.button("Use Saved Calibration", self.use_saved_calibration)
            if not self.startup_calibration:
                self.button("Back to Menu", self.cancel_calibration)
        elif flow.stage == "empty_corners":
            next_corner = flow.CORNER_NAMES[len(flow.empty_corners)]
            self.heading("Mark the playing area", f"Tap its {next_corner} corner. Select the edge of the squares, excluding the wooden border.")
            self.button("Undo Corner", self.undo_corner)
            self.button("Retake Empty Photo", self.retake_empty)
            self.button("Cancel Calibration", self.cancel_calibration)
        elif flow.stage == "starting":
            self.heading("2 / 3 · Place the pieces", "Set up all 32 pieces. Queens start on d1 / d8 and kings on e1 / e8. Keep the camera fixed.")
            self.button("Take Setup Photo", self.capture_starting, accent=True)
            if not flow.empty_auto_corners:
                self.button("Redo Empty Corners", self.redo_empty_corners)
            self.button("Cancel Calibration", self.cancel_calibration)
        elif flow.stage == "starting_corners":
            next_corner = flow.CORNER_NAMES[len(flow.starting_corners)]
            self.heading("Align the setup photo", f"Rotate until White is at the bottom. Tap the {next_corner} corner of the playing squares.")
            self.render_rotation_controls()
            self.button("Undo Corner", self.undo_corner)
            self.button("Retake Setup Photo", self.retake_starting)
            self.button("Cancel Calibration", self.cancel_calibration)
        elif flow.stage == "ready":
            self.heading("3 / 3 · Check your setup", "White should be at the bottom: a1 on the left, h1 on the right. Check that every piece is centered.")
            self.button("Confirm White at Bottom" if flow.setup_only else "Check Board",
                        self.finish_calibration, accent=True, short=True)
            side = flow.white_side_in_preview()
            if side is not None:
                message = ("WHITE APPEARS AT THE BOTTOM" if side == "bottom" else
                           f"WHITE APPEARS ON THE {side.upper()}")
                tk.Label(self.panel, text=message,
                         bg=self.GREEN if side == "bottom" else "#8a2f32",
                         fg=self.BG if side == "bottom" else self.TEXT,
                         font=("DejaVu Sans", 9 if self.compact else 11, "bold"),
                         wraplength=self.panel_width - 30).pack(fill="x", pady=(2, 4))
            self.render_rotation_controls()
            if flow.setup_only:
                tk.Label(self.panel, text="Queens: d1 / d8 · Kings: e1 / e8",
                         bg=self.PANEL, fg=self.GOLD,
                         font=("DejaVu Sans", 9 if self.compact else 11, "bold"),
                         wraplength=self.panel_width - 30).pack(fill="x", pady=(2, 4))
            self.button("Hide Image Settings" if getattr(self, "adjustments_open", False) else "Image Settings",
                        self.toggle_photo_settings, short=True)
            if getattr(self, "adjustments_open", False):
                self.render_photo_adjustments()
            self.button("Redo Setup Corners", self.redo_starting_corners, short=True)
            if not flow.setup_only and not flow.empty_auto_corners:
                self.button("Redo Empty Corners", self.redo_empty_corners, short=True)
            self.button("Retake Setup Photo", self.retake_starting, short=True)
            self.button("Cancel Calibration", self.cancel_calibration, short=True)
        else:
            self.heading("Your board is ready", "Setup saved. You can reuse it while the camera and board stay in place.")
            self.button("Start Game" if getattr(self, "pending_game_start", False) else "Choose Difficulty",
                        self.continue_after_calibration, accent=True)

    def toggle_photo_settings(self) -> None:
        self.adjustments_open = not getattr(self, "adjustments_open", False)
        self.render()

    def render_rotation_controls(self) -> None:
        rotations = tk.Frame(self.panel, bg=self.PANEL)
        rotations.pack(fill="x", pady=2 if self.compact else 4)
        for column, (label, command) in enumerate(
                (("Flip 180°", self.flip_setup_rotation),
                 ("Rotate 90°", self.cycle_setup_rotation))):
            rotations.grid_columnconfigure(column, weight=1)
            self.button_widget(rotations, label, command, short=True).grid(
                row=0, column=column, sticky="ew", padx=2)

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
        self.say("Preview updated. Confirm the starting position when the pieces stand out." if
                 self.calibration_flow.setup_only else
                 "Preview updated. Tap Check Board when the pieces stand out.")

    def saved_calibration_available(self) -> bool:
        directory = self.args.calibration
        if not ((directory / "calibration.json").is_file() and
                (directory / "empty_warp.png").is_file()):
            return False
        try:
            scoring = json.loads((directory / "calibration.json").read_text()).get("scoring", "legacy")
        except (OSError, ValueError, AttributeError):
            return False
        if scoring != "robust_occupancy_v2":
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
        self.welcome_screen = False
        self.adjustments_open = False
        self.calibration_error = None
        self.fresh_starting_reference = None
        self.saved_starting_reference = None
        self.calibration_flow = CalibrationFlow()
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Remove all pieces, then take an empty-board photo.")
        self.render()

    def cancel_calibration(self) -> None:
        self.generation += 1
        self.busy = False
        self.pending_game_start = False
        self.adjustments_open = False
        self.calibration_flow = CalibrationFlow() if self.startup_calibration else None
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Take an empty-board photo, or use a saved calibration." if self.startup_calibration else
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

    def cycle_setup_rotation(self) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage not in ("starting_corners", "ready"):
            return
        if flow.stage == "starting_corners":
            flow.starting_corners = []
        flow.cycle_rotation()
        self.say("Photo rotated. White's pieces should be at the bottom; tap the outer top-left corner." if
                 flow.stage == "starting_corners" else
                 "Rotate until White's back row is at the bottom, with a1 left and h1 right.")
        self.render()

    def suggest_starting_orientation(self) -> bool:
        flow = self.calibration_flow
        if (flow is None or flow.stage != "ready" or
                flow.starting_photo is None or len(flow.starting_corners) != 4):
            return False
        try:
            suggestion = suggest_setup_rotation(
                flow.starting_photo, flow.starting_corners, self.photo_adjustments)
        except (VisionError, cv2.error):
            return False
        if suggestion is None:
            return False
        flow.rotation = suggestion
        flow.suggested_rotation = suggestion
        return True

    def flip_setup_rotation(self) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage not in ("starting_corners", "ready"):
            return
        if flow.stage == "starting_corners":
            flow.starting_corners = []
        flow.flip_rotation()
        self.say("Photo flipped. White's pieces should be at the bottom; tap the outer top-left corner." if
                 flow.stage == "starting_corners" else
                 "White's back row should now be at the bottom. Check a1 on the left and h1 on the right.")
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
        if (flow is None or flow.stage not in ("starting", "setup_only") or self.busy or
                (not flow.setup_only and flow.empty_photo is None)):
            return
        self.busy = True
        self.say("Taking the starting-board photo…")
        self.render()
        previous_corners = ([point.copy() for point in flow.empty_corners]
                            if not flow.setup_only else None)

        def take_photo():
            path = self.args.calibration / "starting.jpg"
            self.camera.capture(path)
            photo = read_image(path)
            if self.args.corners:
                return photo, [point.copy() for point in self.args.corners], False
            try:
                return photo, board_corners(photo).tolist(), False
            except VisionError:
                corners = reusable_corners(photo, previous_corners)
                return photo, corners, corners is not None

        self.submit("cal_start_photo", take_photo)

    def finish_calibration(self) -> None:
        flow = self.calibration_flow
        if (flow is None or flow.stage != "ready" or self.busy or
                flow.starting_photo is None or len(flow.starting_corners) != 4 or
                (not flow.setup_only and
                 (flow.empty_photo is None or len(flow.empty_corners) != 4))):
            return
        starting = flow.starting_photo
        starting_corners = [point.copy() for point in flow.starting_corners]
        adjustments = self.photo_adjustments
        self.calibration_error = None
        self.busy = True
        self.say("Saving the confirmed starting position…" if flow.setup_only else
                 "Aligning both photos and checking the starting board…")
        self.render()
        if flow.setup_only:
            self.submit("cal_finish", lambda: calibrate_setup_photo(
                starting, starting_corners, adjustments, flow.rotation))
        else:
            empty = flow.empty_photo
            empty_corners = [point.copy() for point in flow.empty_corners]
            self.submit("cal_finish", lambda: calibrate_photos(
                empty, starting, empty_corners, starting_corners, adjustments, flow.rotation))

    def continue_after_calibration(self) -> None:
        self.startup_calibration = False
        self.calibration_flow = None
        self.preview_photo = None
        self.preview_geometry = None
        if getattr(self, "pending_game_start", False):
            self.start_game()
            return
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

    def start_game(self) -> None:
        self.welcome_screen = False
        self.pending_game_start = True
        if not self.saved_calibration_available():
            self.start_calibration()
            self.say("Take empty-board and setup photos before starting.", self.GOLD)
            return
        self.photo_adjustments = (read_photo_adjustments(self.args.calibration / "photo_adjustments.json")
                                  or PhotoAdjustments())
        self.generation += 1
        self.position_reference = None
        self.pending_position_reference = None
        self.scan_photo = None
        self.scan_corners = []
        self.busy = True
        self.say("Loading calibration and starting Stockfish…")
        self.render()

        def load():
            calibration, reference = load_calibration(self.args.calibration)
            starting_path = self.args.calibration / "starting_warp.png"
            try:
                starting_reference = read_image(starting_path) if starting_path.is_file() else None
            except VisionError:
                starting_reference = None
            if starting_reference is not None and starting_reference.shape != reference.shape:
                starting_reference = None
            if starting_reference is None:
                # Calibrations made before starting_warp.png was introduced
                # still have the verified setup capture on disk.
                try:
                    adjustments = (read_photo_adjustments(
                        self.args.calibration / "photo_adjustments.json") or PhotoAdjustments())
                    setup = adjust_photo(read_image(self.args.calibration / "starting.jpg"),
                                         adjustments)
                    observed, aligned = observe(reference, setup, calibration,
                                                calibration.corners)
                    if (not observed.uncertain and
                            observed.occupied == occupied_squares(chess.Board())):
                        starting_reference = aligned
                except VisionError:
                    pass
            return calibration, reference, StockfishPlayer(), starting_reference

        gantry = self.gantry   # still open from the last game, or None

        def load_with_gantry():
            loaded = load()
            opened, problem = gantry, None
            if opened is None and GantryPlayer is not None:
                opened = GantryPlayer.from_env()   # None when CHESS_GANTRY_PORT is not set
                if opened is not None:
                    try:
                        opened.connect()
                    except GantryError as exc:
                        opened.close()
                        opened, problem = None, str(exc)
            return (*loaded, opened, problem)

        self.submit("start", load_with_gantry)

    def discard_start(self, value) -> None:
        """A game start nobody waits for any more: close the Stockfish it opened, and a gantry it opened."""
        value[2].close()
        gantry = value[4] if len(value) > 4 else None
        if gantry is not None and gantry is not self.gantry:
            gantry.close()

    def scan(self) -> None:
        if self.session is None or self.busy or self.session.phase not in ("setup", "human", "computer", "remove"):
            return
        self.capture_error = None
        self.scan_attempts = 0
        self.scan_samples = []
        self.scan_aligned_samples = []
        self.latest_observation = None
        self.scan_confirmations_active = True
        self._capture_scan_attempt()

    def _capture_scan_attempt(self) -> None:
        current_corners = getattr(self, "scan_corners", [])
        remembered = ([point.copy() for point in current_corners] if len(current_corners) == 4 else
                      self.last_scan_corners or
                      (self.calibration.corners if self.calibration is not None else None))
        self.scan_attempts += 1
        self.scan_feedback = None
        self.scan_photo = None
        self.scan_corners = []
        self.busy = True
        task = {"setup": "starting pieces", "remove": "captured square"}.get(getattr(self.session, "phase", None), "move")
        self.say(f"Checking {task}: photo {self.scan_attempts} of {MAX_SCAN_ATTEMPTS}. Keep hands clear.")
        self.render()

        def take_photo():
            path = self.args.calibration / "captures" / "latest.jpg"
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
        board_fen = self.session.game.board.fen() if self.session is not None else ""
        attempt = self.scan_attempts
        self.busy = True
        self.say("Checking the aligned board photo…")
        self.render()

        def check_photo():
            adjusted = adjust_photo(photo, self.photo_adjustments)
            if calibration.scoring == "temporal_v1":
                return observe_temporal(reference, adjusted, calibration, corners)
            result = observe(reference, adjusted, calibration, corners)
            observation, _aligned = result
            try:
                report = {"scoring": calibration.scoring, "rotation": calibration.rotation,
                          "position": board_fen, "attempt": attempt, "corners": corners,
                          "occupied": sorted(observation.occupied), "uncertain": observation.uncertain,
                          "scores": observation.scores, "confidence": observation.confidence,
                          "quality": observation.quality}
                (self.args.calibration / "last_scan.json").write_text(json.dumps(report, indent=2) + "\n")
                cv2.imwrite(str(self.args.calibration / "last_scan.jpg"), photo)
            except OSError as exc:
                print(f"Could not save scan diagnostics: {exc}")
            return result

        self.submit("scan", check_photo)

    def use_saved_scan_corners(self) -> None:
        if self.last_scan_corners is None:
            return
        self.scan_corners = [point.copy() for point in self.last_scan_corners]
        self.scan_corner_source = "saved"
        self.scan_feedback = None
        if getattr(self, "scan_confirmations_active", False) and not self.scan_samples:
            self.scan_attempts = 1
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
                if tag == "gantry_progress":
                    self.say(value, self.GOLD)   # the gantry's own words while it works; the screen stays busy
                    continue
                self.busy = False
                if tag == "gantry_control":
                    self.handle_gantry_control(value, error)
                    continue
                if tag in ("gantry_play", "gantry_reset"):
                    self.handle_gantry_result(tag, value, error)
                    continue
                if error is not None:
                    if (((tag == "scan_photo" and
                          isinstance(error, (VisionError, cv2.error))) or
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
                    elif tag == "scan_photo":
                        self.capture_error = str(error)
                        self.say("Camera photo failed. Tap Try Camera Again.", self.RED)
                    elif tag == "start" and isinstance(error, VisionError):
                        self.say("Calibration missing or unreadable. Tap Calibrate Board.", self.RED)
                    elif tag == "cal_finish" and isinstance(error, VisionError):
                        print(f"Calibration check failed: {error}")
                        detail = str(error)
                        if "Pieces are unclear on " in detail:
                            squares = detail.split("Pieces are unclear on ", 1)[1].split(".", 1)[0]
                            self.calibration_error = f"Pieces are hard to see on {squares}. Center them, remove glare, and check the corners."
                        elif "line up" in detail or "grid" in detail:
                            self.calibration_error = "The two photos do not line up. Keep the camera fixed and adjust the photo corners."
                        else:
                            self.calibration_error = "Check all 32 starting pieces, the playing-area corners, and the camera focus. Then try Check Board again."
                        flow = self.calibration_flow
                        self.say(("Calibration failed. Redo the setup photo's corners or retake it." if
                                  flow is None or flow.setup_only else
                                  "Calibration failed. Check both photos' corners, lighting, and pieces."),
                                 self.RED)
                    else:
                        self.say(f"Error: {error}", self.RED)
                    self.render()
                    continue
                try:
                    self.handle_result(tag, value)
                except MoveRejected as exc:
                    if tag == "scan" and self.scan_photo is not None and self.session is not None:
                        observation = (getattr(self, "latest_observation", None) or
                                       (value[0] if isinstance(value, tuple) else value))
                        if self.session.phase == "human":
                            feedback = (temporal_move_feedback(self.session.game.board, observation, str(exc))
                                        if isinstance(observation, TemporalObservation) else
                                        rejected_move_feedback(self.session.game.board, observation, str(exc)))
                        elif self.session.phase == "computer":
                            assert self.session.computer_move is not None
                            feedback = (temporal_computer_move_feedback(
                                self.session.game.board, self.session.computer_move, observation)
                                if isinstance(observation, TemporalObservation) else
                                computer_move_feedback(
                                    self.session.game.board, self.session.computer_move, observation))
                        elif self.session.phase == "remove":
                            feedback = ScanFeedback("PHOTO UNCLEAR" if ("cannot tell" in str(exc) or
                                                                     "unclear" in str(exc)) else
                                                    "REMOVE THE PIECE", str(exc))
                        else:
                            feedback = ScanFeedback("CHECK THE STARTING PIECES", str(exc))
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
            if self.calibration.scoring == "temporal_v1":
                self.say("Saved starting-position calibration loaded. Choose a difficulty.", self.GREEN)
            elif self.calibration.scoring == "legacy":
                self.say("Saved calibration uses the older lighting method. Recalibrate for light correction.",
                         self.GOLD)
            else:
                self.say("Saved calibration loaded. Choose a difficulty to begin.", self.GREEN)
            if getattr(self, "pending_game_start", False):
                self.start_game()
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
                suggested = self.suggest_starting_orientation()
                self.say(("White's side was oriented from the setup photo. " if suggested else
                          "Check the setup photo's orientation. ") +
                         "Confirm White is at the bottom before saving.")
            else:
                self.say("Tap the outer top-left corner in the setup photo.")
        elif tag == "cal_finish":
            flow = self.calibration_flow
            assert flow is not None
            calibration, reference = value
            starting_reference = (aligned_setup_photo(
                flow.starting_photo, flow.starting_corners, self.photo_adjustments,
                flow.rotation) if flow.setup_only else observe(
                    reference, adjust_photo(flow.starting_photo, self.photo_adjustments),
                    calibration, flow.starting_corners)[1])
            if not cv2.imwrite(str(self.args.calibration / "starting_warp.png"), starting_reference):
                raise VisionError("Could not save the verified starting board photo.")
            save_calibration(self.args.calibration, calibration, reference)
            write_photo_adjustments(self.args.calibration / "photo_adjustments.json",
                                    self.photo_adjustments)
            write_camera_settings(self.args.calibration / "calibration_camera.json",
                                  self.camera_settings)
            self.calibration, self.reference = calibration, reference
            self.last_scan_corners = calibration.corners
            # The setup photo was already checked by calibrate_photos. Reuse
            # that verified position when the new game starts immediately.
            self.fresh_starting_reference = starting_reference
            flow.stage = "done"
            self.say("Board setup saved. You're ready to play.", self.GREEN)
        elif tag == "start":
            self.calibration, self.reference, self.player = value[:3]
            self.saved_starting_reference = value[3] if len(value) > 3 else None
            self.last_scan_corners = saved_scan_corners(self.args.calibration, self.calibration)
            if len(value) > 4:
                self.gantry, gantry_problem = value[4], value[5]
            else:
                gantry_problem = None
            self.session = TouchSession(self.level, gantry=self.gantry)
            self.pending_game_start = False
            self.position_reference = None
            if self.calibration.scoring == "temporal_v1":
                self.session.phase = "human"
                self.say("Starting position loaded. Move White, then tap Scan Move.", self.GREEN)
            elif self.fresh_starting_reference is not None:
                self.session.phase = "human"
                self.position_reference = self.fresh_starting_reference
                self.fresh_starting_reference = None
                self.say("Starting board verified. Make your first White move, then tap Scan Move.",
                         self.GREEN)
            else:
                self.say(self.reset_notes_text() +
                         "Place all 32 pieces in their starting squares, then tap Scan Starting Board.",
                         self.GOLD if self.reset_notes else None)
            if self.session.phase == "human":
                self.session.gantry_start_known()   # the start position came from the calibration
                self.reset_notes = ()
            if gantry_problem:
                self.say(f"{gantry_problem} Playing without the gantry: move Black's pieces by hand. " +
                         self.message, self.GOLD)
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
            observation, aligned = value if isinstance(value, tuple) else (value, None)
            temporal = isinstance(observation, TemporalObservation)
            if getattr(self, "scan_confirmations_active", False) and not temporal:
                self.scan_samples.append(observation)
                self.scan_aligned_samples.append(aligned)
                if len(self.scan_samples) < 2:
                    if self.scan_attempts < MAX_SCAN_ATTEMPTS:
                        self._capture_scan_attempt()
                    else:
                        self.scan_feedback = ScanFeedback(
                            "PHOTO NOT VERIFIED", "Two clear photos are needed. Keep the board still, remove your hands, and scan again.")
                        self.say("The board has not changed in the game. Please scan again.", self.GOLD)
                    return
                observation = consensus_observation(self.scan_samples)
                known = set(chess.SQUARE_NAMES) - set(observation.uncertain)
                best = min(range(len(self.scan_samples)), key=lambda index: (
                    len((self.scan_samples[index].occupied ^ observation.occupied) & known),
                    len(self.scan_samples[index].uncertain)))
                aligned = self.scan_aligned_samples[best]
            self.latest_observation = observation if not temporal else None
            position_reference = getattr(self, "position_reference", None)
            change_scores = None
            if not temporal and aligned is not None and position_reference is not None:
                frames = ([frame for frame in self.scan_aligned_samples if frame is not None]
                          if getattr(self, "scan_confirmations_active", False) else [aligned])
                differences = [robust_square_scores(position_reference, frame) for frame in frames]
                if differences:
                    # Two independently captured frames must show the replacement piece.
                    evidence_index = -2 if len(differences) >= 2 else -1
                    change_scores = {square: sorted(scores[square] for scores in differences)[evidence_index]
                                     for square in chess.SQUARE_NAMES}
            if (self.session.phase == "setup" and observation.uncertain and
                    self.scan_corner_source != "manual" and
                    self.scan_attempts < MAX_SCAN_ATTEMPTS):
                self._capture_scan_attempt()
                return
            used_corners = [point.copy() for point in self.scan_corners]
            if self.session.phase == "setup":
                if (not temporal and self.saved_starting_reference is not None and
                        starting_position_unchanged(self.saved_starting_reference,
                                                    self.scan_aligned_samples)):
                    # The saved setup photo was verified during calibration.
                    self.session.verify_setup(occupied_squares(self.session.game.board))
                else:
                    self.session.verify_setup(observation)
                self.position_reference = aligned
                self.clear_scan_photo()
                self.reset_notes = ()   # the camera saw the starting position: the pieces are all back
                if self.session.phase == "setup_confirm":
                    self.say("Please check these squares on the physical board: " +
                             ", ".join(self.session.setup_uncertain) + ".", self.GOLD)
                else:
                    self.say("Starting pieces verified. Make your first White move.",
                             self.GREEN)
            elif self.session.phase == "human":
                san = (self.session.scan_human_temporal(observation) if temporal else
                       self.session.scan_human(observation, change_scores=change_scores))
                self.clear_scan_photo()
                if san is None:
                    self.pending_position_reference = aligned
                    if temporal:
                        self.pending_reference = aligned
                    self.say("Legal move detected. Choose the piece or move shown on screen.", self.GREEN)
                else:
                    self.position_reference = aligned
                    if temporal:
                        self.reference = aligned
                    detail = (" Unclear squares outside the move: " + ", ".join(observation.uncertain) + "."
                              if not temporal and observation.uncertain else "")
                    self.say(f"Legal move: {san}." + detail, self.GREEN)
                    self.after_human_move()
            elif self.session.phase == "remove":
                square = (self.session.verify_removal_temporal(observation) if temporal else
                          self.session.verify_removal(observation))
                self.position_reference = aligned
                if temporal:
                    self.reference = aligned
                self.clear_scan_photo()
                self.say(f"{square} is clear.", self.GREEN)
                self.carry_computer_move()   # the gantry plays the capturing move; the camera check follows
            elif self.session.phase == "computer":
                san = (self.session.verify_computer_temporal(observation) if temporal else
                       self.session.verify_computer(observation, change_scores=change_scores))
                self.position_reference = aligned
                if temporal:
                    self.reference = aligned
                self.clear_scan_photo()
                gantry_notes = " ".join(self.session.gantry_notes)
                self.session.gantry_notes = ()
                self.say(f"Computer move {san} verified. " +
                         (self.session.result or "Your turn.") +
                         (" Unclear squares outside the move: " + ", ".join(observation.uncertain) + "."
                          if not temporal and observation.uncertain else "") +
                         (" " + gantry_notes if gantry_notes else ""), self.GREEN)
                notice = check_notice(self.session.game.board)
                if self.session.phase == "finished":
                    self.say("Good game. " + self.session.winner_text() + ".", self.GREEN)
                    self.reset_pieces()
                elif notice is not None:
                    self.say(notice.detail + " Make a legal move to protect your king.", self.RED)
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
            if self.session.gantry_note:
                directions = f"The gantry could not make this move: {self.session.gantry_note} " + directions
            self.say(directions, self.GOLD)
            self.carry_computer_move()   # with a gantry: it carries the move, then the camera checks it

    def after_human_move(self) -> None:
        assert self.session is not None
        self.selected_square = None
        if self.session.phase == "finished":
            self.say("Good game. " + self.session.winner_text() + ".", self.GREEN)
            self.reset_pieces()
            return
        assert self.player is not None
        self.request_computer_move()

    # ------------------------------------------------------------------ the gantry
    # Everything below does nothing without a gantry (self.gantry is None, session.gantry is None).

    def reset_notes_text(self) -> str:
        """"Put a black pawn on e7. " for the set-up screen, or ""."""
        return "".join(note + " " for note in self.reset_notes)

    def removal_instruction(self) -> str:
        """The Remove the piece screen: "Stockfish plays Bxd5. Take the white bishop off d5, then tap Check." """
        session = self.session
        assert session is not None and session.computer_move is not None
        san = session.game.board.san(session.computer_move)
        return f"Stockfish plays {san}. Take {session.removal_text()}, then tap Check."

    def gantry_progress(self):
        """A `say` for the worker thread: progress lines reach the status bar through the result queue."""
        generation, results = self.generation, self.results
        return lambda text: results.put((generation, "gantry_progress", str(text), None))

    def carry_computer_move(self) -> None:
        """Stockfish's move is known (and a captured piece is off): the gantry carries it in the worker,
        then the camera verifies the board exactly as after a move made by hand."""
        session = self.session
        if (session is None or session.phase != "computer" or session.gantry is None or
                session.gantry_note is not None or self.busy):
            return
        move = session.computer_move
        assert move is not None
        self.busy = True
        self.gantry_activity = f"Gantry moving {move.uci()}…"
        self.say(f"Gantry moving {move.uci()}… keep hands clear of the board.", self.GOLD)
        progress = self.gantry_progress()
        self.submit("gantry_play", lambda: session.carry_computer_move(say=progress))

    def reset_pieces(self, session: TouchSession | None = None) -> None:
        """With a gantry that knows the layout: every piece goes home in the worker behind a
        "Putting the pieces back…" screen. Otherwise nothing happens."""
        session = session or self.session
        if self.gantry is None or session is None or not session.layout_known or self.resetting:
            return
        self.resetting = True
        self.busy = True
        self.gantry_activity = "Putting the pieces back…"
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
            move = session.computer_move
            assert move is not None
            if error is not None:
                message = str(error) if isinstance(error, GantryError) else f"Error: {error}"
                session.gantry_failed(message)
                self.say(f"The gantry could not make the move: {message} Move {move.uci()[:2]} to "
                         f"{move.uci()[2:4]} by hand, then tap Verify Computer Move.", self.RED)
                self.render()
                return
            session.gantry_moved(value)
            self.say(f"Gantry moved {move.uci()}. Checking the board with the camera…", self.GOLD)
            self.scan()   # the same verification as after a move by hand
            return
        # gantry_reset
        self.resetting = False
        if error is not None:
            self.reset_notes = ()
            self.say(f"The gantry could not put the pieces back: {error} "
                     "Set up the starting position by hand before the next game.", self.RED)
        else:
            # The pieces the person took during the game are not the gantry's to put back: the notes stay
            # on the set-up screen until the camera has verified the starting position.
            self.reset_notes = tuple(str(note) for note in (value or ()))
            result = session.result + " " if session is not None and session.result else ""
            self.say(result + "The pieces are back on their starting squares." +
                     (" " + " ".join(self.reset_notes) if self.reset_notes else ""), self.GREEN)
        self.render()

    def forget_gantry_work(self) -> None:
        """A gantry job whose result nobody waits for any more (the generation moved on)."""
        self.gantry_activity = None
        self.resetting = False

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
            if getattr(self, "pending_position_reference", None) is not None:
                self.position_reference = self.pending_position_reference
                self.pending_position_reference = None
            if self.pending_reference is not None:
                self.reference = self.pending_reference
                self.pending_reference = None
            self.say(f"Legal move: {san}", self.GREEN)
            self.after_human_move()
        except ValueError as exc:
            self.say(str(exc), self.RED)
        self.render()

    def retake(self) -> None:
        assert self.session is not None
        self.session.retake()
        self.pending_reference = None
        self.pending_position_reference = None
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
        self.capture_error = None
        self.preview_photo = None
        self.preview_geometry = None
        self.scan_confirmations_active = False
        self.scan_samples = []
        self.scan_aligned_samples = []

    def end_game(self) -> None:
        self.generation += 1
        self.busy = False
        self.confirming_end = False
        self.scan_confirmations_active = False
        self.pending_reference = None
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
        self.welcome_screen = False
        self.pending_game_start = False
        self.confirming_end = False
        self.confirming_new_game = False
        self.pending_reference = None
        self.clear_scan_photo()
        if self.resetting:
            self.busy = False   # New Game on the "Putting the pieces back…" screen: stop waiting for it
        self.forget_gantry_work()
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        abandoned = self.session if self.session is not None and self.session.phase != "finished" else None
        self.session = None
        self.selected_square = None
        self.say("Choose a difficulty to begin.")
        if abandoned is not None:
            self.reset_pieces(abandoned)   # with a gantry: a game left mid-way puts the pieces back first
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
        shown_photo = (rotate_board(photo, flow.rotation)
                       if flow.stage == "starting_corners" else photo)
        height, width = shown_photo.shape[:2]
        x = (event.x - left) / scale
        y = (event.y - top) / scale
        if not (0 <= x < width and 0 <= y < height):
            return
        selecting_setup = flow.stage == "starting_corners"
        flow.add_corner(x, y)
        if selecting_setup and flow.stage == "ready":
            raw_height, raw_width = photo.shape[:2]
            flow.starting_corners = corners_from_rotated_preview(
                flow.starting_corners, raw_width, raw_height, flow.rotation)
        if flow.stage == "starting":
            self.say("Empty-board corners set. Place all starting pieces, then take the setup photo.")
        elif flow.stage == "ready":
            self.suggest_starting_orientation()
            self.say("Check White is at the bottom of the aligned view, then confirm the board.")
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
        raw_height, raw_width = self.scan_photo.shape[:2]
        rotation = self.calibration.rotation if self.calibration is not None else "none"
        shown_photo = rotate_board(self.scan_photo, rotation)
        height, width = shown_photo.shape[:2]
        x = (event.x - left) / scale
        y = (event.y - top) / scale
        if not (0 <= x < width and 0 <= y < height):
            return
        self.scan_corners.append([x, y])
        if len(self.scan_corners) == 4:
            self.scan_corners = corners_from_rotated_preview(
                self.scan_corners, raw_width, raw_height, rotation)
            if getattr(self, "scan_confirmations_active", False) and not self.scan_samples:
                self.scan_attempts = 1
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
            raw_points = points
        else:
            photo = self.scan_photo
            points = self.scan_corners
            raw_points = points
        assert photo is not None
        raw_height, raw_width = photo.shape[:2]
        aligned_preview = None
        if flow is not None and flow.stage == "ready" and len(points) == 4:
            try:
                aligned_preview = aligned_setup_photo(
                    photo, points, self.photo_adjustments, flow.rotation)
            except (VisionError, cv2.error):
                pass
        if aligned_preview is not None:
            shown_photo = aligned_preview
        elif flow is not None and flow.stage == "starting_corners":
            shown_photo = rotate_board(photo, flow.rotation)
        elif flow is None:
            rotation = self.calibration.rotation if self.calibration is not None else "none"
            shown_photo = rotate_board(photo, rotation)
            if len(points) == 4:
                points = corners_to_rotated_preview(
                    points, raw_width, raw_height, rotation)
        else:
            shown_photo = photo
        height, width = shown_photo.shape[:2]
        available_width = canvas.winfo_width() if canvas.winfo_width() > 1 else 400
        available_height = canvas.winfo_height() if canvas.winfo_height() > 1 else 360
        scale = min((available_width - 20) / width, (available_height - 20) / height)
        display_width = max(1, round(width * scale))
        display_height = max(1, round(height * scale))
        resized = cv2.resize(shown_photo, (display_width, display_height),
                             interpolation=cv2.INTER_AREA)
        if aligned_preview is None:
            resized = adjust_photo(resized, self.photo_adjustments)
        ok, encoded = cv2.imencode(".png", resized)
        if not ok:
            raise VisionError("Could not display the board photo.")
        self.preview_photo = tk.PhotoImage(data=base64.b64encode(encoded.tobytes()).decode("ascii"))
        left = (available_width - display_width) / 2
        top = (available_height - display_height) / 2
        canvas.create_image(left, top, image=self.preview_photo, anchor="nw")
        self.preview_geometry = None if aligned_preview is not None else (left, top, scale)
        if aligned_preview is None:
            for index, (x, y) in enumerate(points):
                px, py = left + x * scale, top + y * scale
                canvas.create_oval(px - 7, py - 7, px + 7, py + 7,
                                   fill=self.GOLD, outline=self.BG, width=2)
                canvas.create_text(px + 12, py - 12, text=str(index + 1), fill=self.GOLD,
                                   font=("DejaVu Sans", 12, "bold"))
        if aligned_preview is not None:
            for square in ("a1", "h1"):
                index = chess.parse_square(square)
                x = (chess.square_file(index) + 0.5) * 100
                y = (7 - chess.square_rank(index) + 0.5) * 100
                px, py = left + x * scale, top + y * scale
                canvas.create_rectangle(px - 21, py - 14, px + 21, py + 14,
                                        fill=self.BG, outline=self.GOLD, width=2)
                canvas.create_text(px, py, text=square, fill=self.GOLD,
                                   font=("DejaVu Sans", 12, "bold"))
        elif flow is not None and flow.stage == "ready":
            canvas.create_text(left + 12, top + 18,
                               text="Corners need adjustment", anchor="w",
                               fill=self.RED, font=("DejaVu Sans", 12, "bold"))
        if flow is None and self.scan_feedback is not None:
            feedback = self.scan_feedback
            canvas.create_rectangle(left, top, left + display_width, top + 48,
                                    fill="#8a2f32", outline="")
            canvas.create_text(left + 15, top + 24, text=feedback.title,
                               fill=self.TEXT, anchor="w",
                               font=("DejaVu Sans", 16, "bold"))
            if (len(points) == 4 and
                    (feedback.sources or feedback.destinations or feedback.expected_sources)):
                rotation = self.calibration.rotation if self.calibration is not None else "none"
                centers = {}
                all_squares = (feedback.expected_sources + feedback.expected_destinations +
                               feedback.sources + feedback.destinations)
                for square in all_squares:
                    x, y = photo_square_center(square, raw_points, rotation)
                    x, y = rotate_photo_point(x, y, raw_width, raw_height, rotation)
                    centers[square] = (left + x * scale, top + y * scale)
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

    def draw_welcome(self) -> None:
        canvas = self.board_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), 300)
        center = width / 2
        canvas.create_text(center, height * .42, text="CHESS",
                           fill=self.TEXT, font=("DejaVu Sans", 38 if self.compact else 62, "bold"))
        canvas.create_line(center - 64, height * .57, center + 64, height * .57,
                           fill=self.GREEN, width=3)
        canvas.create_text(center, height * .7, text="Play on your physical board",
                           fill=self.MUTED, font=("DejaVu Sans", 12 if self.compact else 17))

    def board_geometry(self) -> tuple[float, float, float, float]:
        actual_width = self.board_canvas.winfo_width()
        actual_height = self.board_canvas.winfo_height()
        width = actual_width if actual_width > 1 else 400
        height = actual_height if actual_height > 1 else 360
        size = max(80, min(width - 34, height - 30))
        square_size = size / 8
        return (width - size) / 2, (height - size) / 2, size, square_size

    def draw_board(self) -> None:
        if getattr(self, "welcome_screen", False) and self.calibration_flow is None:
            self.draw_welcome()
            return
        if self.session is not None and self.session.phase == "finished":
            self.draw_game_over()
            return
        if (self.scan_photo is not None or
                (self.calibration_flow is not None and
                 self.calibration_flow.stage in ("empty_corners", "starting", "starting_corners", "ready") and
                 (self.calibration_flow.empty_photo is not None or
                  self.calibration_flow.starting_photo is not None))):
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
        notice = check_notice(board)
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
                if (self.session and self.session.phase == "remove" and self.session.removal and
                        name == self.session.removal[0]):
                    fill = "#ff8c85"   # the captured piece the person takes off before the gantry plays
                if name == self.selected_square:
                    fill = "#ffe08a"
                if notice is not None and name == notice.king:
                    fill = "#d57570"
                canvas.create_rectangle(left, top, left + step, top + step,
                                        fill=fill, outline=fill)
                if name in highlights:
                    margin = step * .36
                    canvas.create_oval(left + margin, top + margin,
                                       left + step - margin, top + step - margin,
                                       fill="#ffd66e", outline="")
                piece = board.piece_at(square)
                if piece:
                    cx, cy = left + step / 2, top + step / 2
                    glyph = {chess.KING: "♚", chess.QUEEN: "♛", chess.ROOK: "♜",
                             chess.BISHOP: "♝", chess.KNIGHT: "♞", chess.PAWN: "♟"}[piece.piece_type]
                    canvas.create_text(cx + 1, cy + 1, text=glyph,
                                       fill="#102721" if piece.color else "#e8ddc4",
                                       font=("DejaVu Sans", max(20, int(step * .75))))
                    canvas.create_text(cx, cy, text=glyph,
                                       fill="#fff9e9" if piece.color else "#142c28",
                                       font=("DejaVu Sans", max(20, int(step * .75))))
                if notice is not None and name in notice.attackers:
                    canvas.create_rectangle(left + 3, top + 3, left + step - 3, top + step - 3,
                                            outline="#8a2f32", width=3)
        for row in range(8):
            canvas.create_text(x - 12, y + (row + .5) * step, text=str(8 - row),
                               fill=self.MUTED, font=("DejaVu Sans", 9))
        for col, file in enumerate("abcdefgh"):
            canvas.create_text(x + (col + .5) * step, y + size + 10, text=file,
                               fill=self.MUTED, font=("DejaVu Sans", 9))

    def draw_game_over(self) -> None:
        canvas = self.board_canvas
        canvas.delete("all")
        self.preview_geometry = None
        width, height = max(canvas.winfo_width(), 400), max(canvas.winfo_height(), 300)
        canvas.create_text(width / 2, height * .36, text="Good game",
                           fill=self.TEXT, font=("DejaVu Sans", 32 if self.compact else 52, "bold"))
        canvas.create_text(width / 2, height * .55, text=self.session.winner_text(),
                           fill=self.GREEN, font=("DejaVu Sans", 19 if self.compact else 28, "bold"),
                           width=width - 40)
        canvas.create_text(width / 2, height * .7,
                           text="Checkmate" if self.session.game.board.is_checkmate() else self.session.result,
                           fill=self.MUTED, font=("DejaVu Sans", 12 if self.compact else 17),
                           width=width - 40)

    # ------------------------------------------------------------------ the Gantry screen
    # Between games (welcome, level choice, game over; never while the gantry or the camera works) the Gantry
    # button opens a full-screen layer for putting the magnet in the right place: Center gantry, a jog pad seen
    # from the player's side (White at the bottom, as the board is drawn: ▲ is towards rank 8, ▶ towards the
    # h-file), Go to a square on a little board, Set home here, Make this spot home, Set start here, Return home
    # and Motors off. Each action runs in the worker (submit "gantry_control") with every button disabled, and
    # the gantry's own words come back to the screen.

    GANTRY_STEPS = (("1", "1 mm"), ("5", "5 mm"), ("square", "1 square"))
    GANTRY_PAD = (("▲", 0, 1), ("◀", -1, 0), ("▶", 1, 0), ("▼", 0, -1))   # label, files (+X), ranks (+Y)

    def open_gantry_panel(self) -> None:
        if self.busy or self.gantry_screen is not None:
            return
        if self.session is not None and self.session.phase != "finished":
            return   # mid-game the gantry belongs to the game
        self.gantry_problem = None
        self.gantry_confirm = False
        self.gantry_readout = None
        self.gantry_screen_owned = False
        if self.gantry is None:
            if GantryPlayer is None:
                self.gantry_problem = "The gantry software (pi_gantry) could not be loaded."
            elif not (os.environ.get("CHESS_GANTRY_PORT") or "").strip():
                self.gantry_problem = ("No gantry is set up: CHESS_GANTRY_PORT is not set "
                                       "(e.g. /dev/ttyUSB0, auto, or sim for the pretend board).")
            else:
                player = GantryPlayer.from_env()
                if player is None:
                    self.gantry_problem = ("The gantry is set up but could not be found: check the USB cable, "
                                           "CHESS_GANTRY_DIR and gantry_config.json (the log says which).")
                else:
                    self.gantry, self.gantry_screen_owned = player, True
        self.build_gantry_screen()
        if self.gantry is not None:
            self.run_gantry_action("Connecting to the gantry…", lambda gantry: gantry.status(), None)
        else:
            self.gantry_say(self.gantry_problem, self.RED)

    def close_gantry_panel(self) -> None:
        if self.busy or self.gantry_screen is None:
            return
        screen, self.gantry_screen = self.gantry_screen, None
        screen.destroy()
        if self.gantry_screen_owned and self.gantry is not None:
            # Opened for this screen only: let the port go (the laptop console may want it). The next game opens it again.
            gantry, self.gantry = self.gantry, None
            self.executor.submit(gantry.close)
        self.gantry_screen_owned = False
        self.say("Gantry screen closed.")
        self.render()

    def run_gantry_action(self, doing: str, work, done) -> None:
        """work(gantry) in the worker; done(status) -> words for the screen once it worked."""
        gantry = self.gantry
        if self.busy or gantry is None or self.gantry_screen is None:
            return
        self.busy = True
        self.gantry_done = done
        self.gantry_say(doing, self.GOLD)
        self.refresh_gantry_screen()
        self.submit("gantry_control", lambda: work(gantry))

    def handle_gantry_control(self, value, error) -> None:
        done, self.gantry_done = getattr(self, "gantry_done", None), None
        if error is not None:
            self.gantry_say(str(error), self.RED)
        else:
            self.gantry_readout = value
            words = done(value) if done is not None else (value.get("restore") or "Gantry connected.")
            self.gantry_say(words, self.GREEN)
        self.refresh_gantry_screen()

    def gantry_say(self, text: str, color: str | None = None) -> None:
        self.say(text, color)
        label = getattr(self, "gantry_message", None)
        if self.gantry_screen is not None and label is not None:
            label.configure(text=text, fg=color or self.MUTED)

    # the actions (each one press of a button)

    def gantry_step_mm(self) -> tuple[float, float]:
        if self.gantry_step == "square":
            return self.gantry.square_size_mm()
        step = float(self.gantry_step)
        return step, step

    def gantry_jog(self, files: int, ranks: int) -> None:
        if self.gantry is None:
            return
        sx, sy = self.gantry_step_mm()
        self.run_gantry_action("Jogging…", lambda gantry: gantry.jog(files * sx, ranks * sy), self.position_words)

    def gantry_center(self) -> None:
        self.run_gantry_action("Centering the gantry…", lambda gantry: gantry.center(),
                               lambda st: "Centered: the magnet is at the start (a8). " + self.position_words(st))

    def gantry_go_to(self, square: str) -> None:
        self.run_gantry_action(f"Going to {square}…", lambda gantry: gantry.go_to_square(square),
                               lambda st: f"At {square}. " + self.position_words(st))

    def gantry_set_home(self) -> None:
        self.run_gantry_action("Setting home here…", lambda gantry: gantry.set_home_here(),
                               lambda st: "Home set: this spot is home. " + self.position_words(st))

    def gantry_ask_home_spot(self) -> None:
        if self.busy:
            return
        self.gantry_confirm = True
        self.build_gantry_buttons()
        self.refresh_gantry_screen()

    def gantry_cancel_home_spot(self) -> None:
        self.gantry_confirm = False
        self.build_gantry_buttons()
        self.refresh_gantry_screen()

    def gantry_save_home_spot(self) -> None:
        self.gantry_confirm = False
        self.build_gantry_buttons()
        self.run_gantry_action(
            "Saving this spot as home…", lambda gantry: gantry.save_home_spot_here(),
            lambda st: ("Saved: home is {:.1f} mm along X and {:.1f} mm along Y from the corner. From now on put the "
                        "carriage here and tap Set home here.").format(*st["saved"]["home_spot_mm"]))

    def gantry_set_start(self) -> None:
        self.run_gantry_action("Saving the start…", lambda gantry: gantry.set_start_here(),
                               lambda st: "Saved: Center gantry and every finished move now end here (a8). "
                                          + self.position_words(st))

    def gantry_return_home(self) -> None:
        self.run_gantry_action("Returning home…", lambda gantry: gantry.return_home(),
                               lambda st: "Home. " + self.position_words(st))

    def gantry_arm(self, up: bool) -> None:
        self.run_gantry_action("Raising the arm…" if up else "Lowering the arm…", lambda gantry: gantry.arm(up),
                               lambda _st: "Arm up: the magnet is against the board." if up
                               else "Arm down: the magnet is clear of the board.")

    def gantry_motors_off(self) -> None:
        self.run_gantry_action("Switching the motors off…", lambda gantry: gantry.motors_off(),
                               lambda _st: "Motors off: the carriage can be pushed by hand. Set home before moving again.")

    def set_gantry_step(self, step: str) -> None:
        self.gantry_step = step
        self.refresh_gantry_screen()

    @staticmethod
    def position_words(st: dict) -> str:
        return f"X {st['x']:.1f}, Y {st['y']:.1f} mm."

    def gantry_readout_text(self) -> str:
        st = self.gantry_readout
        if self.gantry is None:
            return "No gantry"
        if st is None:
            return "Position: asking the gantry…"
        homed = "homed" if st["homed"] else "NOT homed"
        alarm = f" · {st['state']}: {st['reason']}" if st["state"] not in ("Idle", "Alarm") or (
            st["state"] == "Alarm" and st["reason"] not in ("", "not homed")) else ""
        arm = {"up": "  ·  arm up", "down": "  ·  arm down"}.get(st.get("servo"), "")
        return f"X {st['x']:.1f}   Y {st['y']:.1f} mm  ·  {homed}{arm}  ·  {st['memory']}{alarm}"

    # the layer itself

    def build_gantry_screen(self) -> None:
        compact = self.compact
        screen = tk.Frame(self.root, bg=self.BG)
        screen.place(x=0, y=0, relwidth=1, relheight=1)
        self.gantry_screen = screen
        big = ("DejaVu Sans", 12 if compact else 15, "bold")
        small = ("DejaVu Sans", 9 if compact else 11)
        top = tk.Frame(screen, bg=self.BG)
        top.pack(side="top", fill="x", padx=8, pady=(6, 2))
        tk.Label(top, text="GANTRY", bg=self.BG, fg=self.TEXT, font=big).pack(side="left")
        self.gantry_readout_label = tk.Label(top, text="", bg=self.BG, fg=self.MUTED, font=small, anchor="w")
        self.gantry_readout_label.pack(side="left", padx=10, fill="x", expand=True)
        self.gantry_message = tk.Label(screen, text="", bg=self.PANEL, fg=self.MUTED, font=small, anchor="w",
                                       justify="left", height=2, padx=8,
                                       wraplength=self.screen_width - 40)
        self.gantry_message.pack(side="bottom", fill="x", padx=8, pady=(2, 6))
        body = tk.Frame(screen, bg=self.BG)
        body.pack(side="top", fill="both", expand=True, padx=8)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=5, uniform="gantry")
        body.grid_columnconfigure(1, weight=4, uniform="gantry")
        body.grid_columnconfigure(2, weight=4, uniform="gantry")

        # Left: the little board, White at the bottom like the game's board; tap a square to go there.
        left = tk.Frame(body, bg=self.BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(left, text="Tap a square to go there", bg=self.BG, fg=self.MUTED, font=small).pack(side="top")
        self.gantry_board = tk.Canvas(left, bg=self.BG, highlightthickness=0)
        self.gantry_board.pack(side="top", fill="both", expand=True)
        self.gantry_board.bind("<Configure>", lambda _event: self.draw_gantry_board())
        self.gantry_board.bind("<Button-1>", self.tap_gantry_board)

        # Middle: Center gantry, the jog pad, the step.
        middle = tk.Frame(body, bg=self.BG)
        middle.grid(row=0, column=1, sticky="nsew", padx=4)
        self.gantry_buttons: list = []
        center = self.gantry_button(middle, "Center gantry", self.gantry_center, accent=True, font=big)
        center.pack(side="top", fill="x", pady=(0, 6), ipady=6 if compact else 12)
        pad = tk.Frame(middle, bg=self.BG)
        pad.pack(side="top", fill="both", expand=True)
        for index in range(3):
            pad.grid_rowconfigure(index, weight=1, uniform="pad")
            pad.grid_columnconfigure(index, weight=1, uniform="pad")
        places = {"▲": (0, 1), "◀": (1, 0), "▶": (1, 2), "▼": (2, 1)}
        for label, files, ranks in self.GANTRY_PAD:
            row, column = places[label]
            button = self.gantry_button(pad, label, lambda f=files, r=ranks: self.gantry_jog(f, r),
                                        font=("DejaVu Sans", 18 if compact else 26, "bold"))
            button.grid(row=row, column=column, sticky="nsew", padx=2, pady=2)
        tk.Label(pad, text="▲ rank 8\n▼ rank 1", bg=self.BG, fg=self.MUTED, font=small).grid(row=1, column=1)
        tk.Label(middle, text="As seen from your side (White)", bg=self.BG, fg=self.MUTED,
                 font=small).pack(side="top")
        steps = tk.Frame(middle, bg=self.BG)
        steps.pack(side="top", fill="x", pady=(2, 0))
        self.gantry_step_buttons = {}
        for index, (key, label) in enumerate(self.GANTRY_STEPS):
            steps.grid_columnconfigure(index, weight=1, uniform="step")
            button = self.gantry_button(steps, label, lambda k=key: self.set_gantry_step(k), font=small)
            button.grid(row=0, column=index, sticky="ew", padx=1)
            self.gantry_step_buttons[key] = button
        arm = tk.Frame(middle, bg=self.BG)
        arm.pack(side="top", fill="x", pady=(6, 0))
        for index, (label, up) in enumerate((("Arm up", True), ("Arm down", False))):
            arm.grid_columnconfigure(index, weight=1, uniform="arm")
            self.gantry_button(arm, label, lambda u=up: self.gantry_arm(u)).grid(
                row=0, column=index, sticky="ew", padx=1, ipady=4 if self.compact else 8)

        # Right: homing, start, motors, Back (or the "Make this spot home?" question).
        self.gantry_right = tk.Frame(body, bg=self.BG)
        self.gantry_right.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self.build_gantry_buttons()
        self.refresh_gantry_screen()

    def gantry_button(self, parent, label: str, command, *, accent: bool = False, font=None):
        button = tk.Button(parent, text=label, command=command, bd=0, relief="flat",
                           font=font or ("DejaVu Sans", 10 if self.compact else 12, "bold"),
                           bg=self.GREEN if accent else "#294943", fg=self.BG if accent else self.TEXT,
                           activebackground=self.GOLD, activeforeground=self.BG,
                           disabledforeground="#5f7a73", wraplength=200 if self.compact else 300)
        self.gantry_buttons.append(button)
        return button

    def build_gantry_buttons(self) -> None:
        right = self.gantry_right
        for child in right.winfo_children():
            if child in self.gantry_buttons:
                self.gantry_buttons.remove(child)
            child.destroy()
        if self.gantry_confirm:
            tk.Label(right, text="Make this spot home?", bg=self.BG, fg=self.TEXT,
                     font=("DejaVu Sans", 11 if self.compact else 14, "bold"), anchor="w").pack(fill="x")
            tk.Label(right, text="Only after homing with the carriage pushed into the true corner, then jogging to "
                                 "the spot you will home from. It is saved in gantry_config.json; from then on put "
                                 "the carriage there and tap Set home here.",
                     bg=self.BG, fg=self.MUTED, font=("DejaVu Sans", 9 if self.compact else 11), justify="left",
                     wraplength=200 if self.compact else 300, anchor="w").pack(fill="x", pady=(2, 6))
            entries = (("Yes, make this home", self.gantry_save_home_spot, True),
                       ("Cancel", self.gantry_cancel_home_spot, False))
        else:
            entries = (("Set home here", self.gantry_set_home, False),
                       ("Make this spot home", self.gantry_ask_home_spot, False),
                       ("Set start here", self.gantry_set_start, False),
                       ("Return home", self.gantry_return_home, False),
                       ("Motors off", self.gantry_motors_off, False),
                       ("Back", self.close_gantry_panel, True))
        for label, command, accent in entries:
            self.gantry_button(right, label, command, accent=accent).pack(
                side="top", fill="both", expand=True, pady=2)

    def refresh_gantry_screen(self) -> None:
        if self.gantry_screen is None:
            return
        usable = not self.busy and self.gantry is not None
        for button in self.gantry_buttons:
            label = button.cget("text")
            if label == "Back":
                enabled = not self.busy
            elif label == "Cancel":
                enabled = not self.busy
            else:
                enabled = usable
            button.configure(state="normal" if enabled else "disabled")
        for key, button in self.gantry_step_buttons.items():
            chosen = key == self.gantry_step
            button.configure(bg=self.GOLD if chosen else "#294943", fg=self.BG if chosen else self.TEXT)
        self.gantry_readout_label.configure(text=self.gantry_readout_text())
        self.draw_gantry_board()

    def gantry_board_geometry(self) -> tuple[float, float, float]:
        canvas = self.gantry_board
        width = max(int(canvas.winfo_width()), 160)
        height = max(int(canvas.winfo_height()), 160)
        size = min(width, height) - 24
        return (width - size) / 2 + 6, (height - size) / 2, size / 8

    def draw_gantry_board(self) -> None:
        canvas = getattr(self, "gantry_board", None)
        if self.gantry_screen is None or canvas is None:
            return
        canvas.delete("all")
        left, top, step = self.gantry_board_geometry()
        for row in range(8):
            for col in range(8):
                fill = "#e8ddc4" if (row + col) % 2 == 0 else "#678577"
                x, y = left + col * step, top + row * step
                canvas.create_rectangle(x, y, x + step, y + step, fill=fill, outline=fill)
        for row in range(8):
            canvas.create_text(left - 8, top + (row + .5) * step, text=str(8 - row), fill=self.MUTED,
                               font=("DejaVu Sans", 8))
        for col, file in enumerate("abcdefgh"):
            canvas.create_text(left + (col + .5) * step, top + 8 * step + 9, text=file, fill=self.MUTED,
                               font=("DejaVu Sans", 8))
        st = self.gantry_readout
        if st and st.get("units") is not None:
            ux, uy = st["units"]
            cx, cy = left + (ux + .5) * step, top + (7 - uy + .5) * step
            r = max(5.0, step * .28)
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#d04a3c", width=3)
            canvas.create_line(cx - r * 1.4, cy, cx + r * 1.4, cy, fill="#d04a3c", width=2)
            canvas.create_line(cx, cy - r * 1.4, cx, cy + r * 1.4, fill="#d04a3c", width=2)

    def tap_gantry_board(self, event) -> None:
        if self.busy or self.gantry is None:
            return
        left, top, step = self.gantry_board_geometry()
        col, row = int((event.x - left) // step), int((event.y - top) // step)
        if 0 <= col < 8 and 0 <= row < 8:
            self.gantry_go_to(f"{'abcdefgh'[col]}{8 - row}")

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
            if (session is not None and session.layout_known and session.phase != "finished" and
                    not self.resetting):
                self.executor.submit(lambda: (session.put_pieces_back(), gantry.close()))
            else:
                self.executor.submit(gantry.close)
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
