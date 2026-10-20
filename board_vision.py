"""Offline, model-free square occupancy detection from still board photos."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import chess
import cv2
import numpy as np


SIZE = 800
SQUARE = SIZE // 8
MARKER_IDS = (0, 1, 2, 3)  # top left, top right, bottom right, bottom left
INNER_MARKER_CORNERS = (2, 3, 0, 1)
MARKER_MARGIN_RATIO = 0.1  # white sticker margin / black marker side length
INNER_MARKER_NEIGHBORS = ((3, 1), (2, 0), (1, 3), (0, 2))


class VisionError(ValueError):
    """An image is missing, cannot be aligned, or cannot be classified safely."""


def read_image(path: str | Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise VisionError(f"Could not read image: {path}")
    return image


def board_corners(image: np.ndarray, manual: list[list[float]] | None = None) -> np.ndarray:
    if manual is not None:
        points = np.asarray(manual, dtype=np.float32)
        if points.shape != (4, 2):
            raise VisionError("Corners must be four x,y points in top-left, top-right, bottom-right, bottom-left order.")
        return points

    if not hasattr(cv2, "aruco"):
        raise VisionError("This OpenCV build lacks ArUco support; install opencv-contrib-python-headless.")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    corners, ids, _ = detector.detectMarkers(image)
    found = {} if ids is None else {int(marker_id): marker_corners[0] for marker_id, marker_corners in zip(ids.flatten(), corners)}
    missing = set(MARKER_IDS) - set(found)
    if missing:
        raise VisionError(f"Missing ArUco corner markers: {sorted(missing)}. Keep all four visible or provide --corners.")
    points = []
    for marker_id, corner, neighbors in zip(MARKER_IDS, INNER_MARKER_CORNERS, INNER_MARKER_NEIGHBORS):
        marker = found[marker_id]
        # Project from the detected black marker corner to the white sticker's
        # nearest outer corner, which is aligned with the board's outer corner.
        first, second = neighbors
        point = marker[corner] + MARKER_MARGIN_RATIO * (
            2 * marker[corner] - marker[first] - marker[second]
        )
        points.append(point)
    return np.float32(points)


def warp_board(image: np.ndarray, manual: list[list[float]] | None = None) -> np.ndarray:
    source = board_corners(image, manual)
    destination = np.float32([[0, 0], [SIZE - 1, 0], [SIZE - 1, SIZE - 1], [0, SIZE - 1]])
    if cv2.contourArea(source) < 10000:
        raise VisionError("Board corners cover too little area or are in the wrong order.")
    transform = cv2.getPerspectiveTransform(source, destination)
    return cv2.warpPerspective(image, transform, (SIZE, SIZE))


def rotate_board(image: np.ndarray, rotation: str) -> np.ndarray:
    """Orient the straightened board so a8 is at top left."""
    modes = {
        "none": None,
        "cw": cv2.ROTATE_90_CLOCKWISE,
        "ccw": cv2.ROTATE_90_COUNTERCLOCKWISE,
        "180": cv2.ROTATE_180,
    }
    if rotation not in modes:
        raise VisionError(f"Unknown board rotation: {rotation}")
    return cv2.rotate(image, modes[rotation]) if modes[rotation] is not None else image


def square_scores(empty_board: np.ndarray, observed_board: np.ndarray) -> dict[str, float]:
    if empty_board.shape != (SIZE, SIZE, 3) or observed_board.shape != empty_board.shape:
        raise VisionError("Reference and observed boards must both be aligned 800x800 color images.")
    reference = cv2.GaussianBlur(cv2.cvtColor(empty_board, cv2.COLOR_BGR2LAB), (5, 5), 0).astype(np.float32)
    observed = cv2.GaussianBlur(cv2.cvtColor(observed_board, cv2.COLOR_BGR2LAB), (5, 5), 0).astype(np.float32)
    difference = np.linalg.norm(observed - reference, axis=2)
    scores = {}
    for rank in range(8):
        for file in range(8):
            # Discard grid lines and the edges where a neighboring piece can intrude.
            patch = difference[rank * SQUARE + 18:(rank + 1) * SQUARE - 18,
                               file * SQUARE + 18:(file + 1) * SQUARE - 18]
            scores[f"{'abcdefgh'[file]}{8 - rank}"] = float(np.mean(patch))
    return scores


def _group(square: str) -> int:
    return (ord(square[0]) - ord('a') + int(square[1]) - 1) % 2


def objectness_scores(aligned: np.ndarray) -> dict[str, float]:
    """Measure how unlike the square's own bare corners its center looks."""
    if aligned.shape != (SIZE, SIZE, 3):
        raise VisionError("Board must be an aligned 800x800 color image.")
    lab = cv2.cvtColor(aligned, cv2.COLOR_BGR2LAB).astype(np.float32)
    yy, xx = np.ogrid[:SQUARE, :SQUARE]
    center = (xx - SQUARE / 2) ** 2 + (yy - SQUARE / 2) ** 2 < 32 ** 2
    scores = {}
    for row in range(8):
        for col in range(8):
            patch = lab[row * SQUARE:(row + 1) * SQUARE, col * SQUARE:(col + 1) * SQUARE]
            corners = np.concatenate([
                patch[:15, :15].reshape(-1, 3),
                patch[:15, -15:].reshape(-1, 3),
                patch[-15:, :15].reshape(-1, 3),
                patch[-15:, -15:].reshape(-1, 3),
            ])
            bare_color = np.median(corners, axis=0)
            difference = np.linalg.norm(patch - bare_color, axis=2)
            scores[f"{'abcdefgh'[col]}{8 - row}"] = float(np.mean(difference[center]))
    return scores


def calibrate_from_position(scores: dict[str, float], board: chess.Board) -> tuple[float, float]:
    """Learn occupied/empty thresholds from a photo of a known position."""
    if set(scores) != set(chess.SQUARE_NAMES):
        raise VisionError("Expected one score for every chess square.")
    occupied = frozenset(chess.square_name(square) for square in board.piece_map())
    thresholds = []
    bad_squares = []
    for group in (0, 1):
        empty_values = [score for square, score in scores.items() if _group(square) == group and square not in occupied]
        occupied_values = [score for square, score in scores.items() if _group(square) == group and square in occupied]
        if not empty_values or not occupied_values:
            raise VisionError("Calibration position needs occupied and empty squares of both colors.")
        empty_max, occupied_min = max(empty_values), min(occupied_values)
        if occupied_min > empty_max:
            threshold = (empty_max + occupied_min) / 2
        else:
            # A shifted phone view can make one piece resemble bare wood. Pick
            # the threshold with the fewest known-position errors, then reject
            # calibration if those errors are too numerous or too strong.
            values = sorted(set(empty_values + occupied_values))
            options = [candidate for left, right in zip(values, values[1:])
                       for candidate in ((left + right) / 2, left + 0.001, right - 0.001)]
            if not options:
                raise VisionError("Known-position photo has no separation between occupied and empty squares.")

            def error(candidate: float) -> tuple[int, float, float]:
                penalties = [max(0.0, value - candidate) for value in empty_values]
                penalties += [max(0.0, candidate - value) for value in occupied_values]
                return (sum(penalty > 0 for penalty in penalties),
                        sum(penalties),
                        -min(abs(value - candidate) for value in values))

            threshold = min(options, key=error)
        thresholds.append(threshold)
        bad_squares.extend((square, abs(score - threshold)) for square, score in scores.items()
                           if _group(square) == group and
                           ((square in occupied and score < threshold) or
                            (square not in occupied and score > threshold)))
    if len(bad_squares) > 2 or any(penalty > 15 for _, penalty in bad_squares):
        raise VisionError(
            "Known-position photo has too many uncertain squares: " +
            ", ".join(square for square, _ in bad_squares) +
            ". Check corners, rotation, lighting, and the supplied FEN or move history."
        )
    return tuple(thresholds)


@dataclass(frozen=True)
class SquareChanges:
    vacated: tuple[str, ...]
    filled: tuple[str, ...]
    uncertain: tuple[str, ...]


def detect_square_changes(
    before_scores: dict[str, float],
    after_scores: dict[str, float],
    before_occupied: frozenset[str],
    thresholds: tuple[float, float],
    *,
    min_change: float = 30.0,
) -> SquareChanges:
    """Report likely square changes without using chess move legality.

    A large center-to-background score change and a plausible final score
    must agree. Borderline threshold crossings are reported separately.
    """
    if set(before_scores) != set(chess.SQUARE_NAMES) or set(after_scores) != set(chess.SQUARE_NAMES):
        raise VisionError("Expected before and after scores for every chess square.")
    vacated, filled, uncertain = [], [], []
    for square in chess.SQUARE_NAMES:
        threshold = thresholds[_group(square)]
        before, after = before_scores[square], after_scores[square]
        if square in before_occupied:
            normal_vacancy = before - after >= min_change and after <= threshold + 5
            shadowed_vacancy = before - after >= min_change + 15 and after <= threshold + 20
            if normal_vacancy or shadowed_vacancy:
                vacated.append(square)
            elif after < threshold:
                uncertain.append(square)
        else:
            if after - before >= min_change and after >= threshold + 8:
                filled.append(square)
            elif after > threshold:
                uncertain.append(square)
    return SquareChanges(tuple(vacated), tuple(filled), tuple(uncertain))


@dataclass(frozen=True)
class Calibration:
    corners: list[list[float]] | None
    thresholds: tuple[float, float]
    margins: tuple[float, float]

    def to_json(self) -> dict:
        return {"corners": self.corners, "thresholds": self.thresholds, "margins": self.margins}

    @classmethod
    def from_json(cls, data: dict) -> "Calibration":
        return cls(data["corners"], tuple(data["thresholds"]), tuple(data["margins"]))


def calibrate(empty: np.ndarray, starting: np.ndarray, corners: list[list[float]] | None = None) -> tuple[Calibration, np.ndarray, dict[str, float]]:
    reference = warp_board(empty, corners)
    setup = warp_board(starting, corners)
    scores = square_scores(reference, setup)
    occupied = frozenset(chess.square_name(square) for square in chess.Board().piece_map())
    thresholds = []
    margins = []
    for group in (0, 1):
        empty_max = max(score for square, score in scores.items() if _group(square) == group and square not in occupied)
        occupied_min = min(score for square, score in scores.items() if _group(square) == group and square in occupied)
        if occupied_min <= empty_max:
            raise VisionError(
                f"Calibration failed for {'light' if group else 'dark'} squares: "
                f"empty max {empty_max:.1f}, occupied min {occupied_min:.1f}. "
                "Check corner order, alignment, lighting, and that the setup photo has the standard starting position."
            )
        thresholds.append((empty_max + occupied_min) / 2)
        margins.append((occupied_min - empty_max) * 0.2)
    return Calibration(corners, tuple(thresholds), tuple(margins)), reference, scores


@dataclass(frozen=True)
class Observation:
    occupied: frozenset[str]
    uncertain: tuple[str, ...]
    scores: dict[str, float]


def observe(reference: np.ndarray, photo: np.ndarray, calibration: Calibration) -> tuple[Observation, np.ndarray]:
    aligned = warp_board(photo, calibration.corners)
    scores = square_scores(reference, aligned)
    occupied = frozenset(square for square, score in scores.items() if score > calibration.thresholds[_group(square)])
    uncertain = tuple(sorted(square for square, score in scores.items()
                             if abs(score - calibration.thresholds[_group(square)]) <= calibration.margins[_group(square)]))
    return Observation(occupied, uncertain, scores), aligned


def save_calibration(directory: Path, calibration: Calibration, reference: np.ndarray) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(directory / "empty_warp.png"), reference):
        raise VisionError("Could not write empty_warp.png")
    (directory / "calibration.json").write_text(json.dumps(calibration.to_json(), indent=2) + "\n")


def load_calibration(directory: Path) -> tuple[Calibration, np.ndarray]:
    try:
        data = json.loads((directory / "calibration.json").read_text())
    except (OSError, ValueError, KeyError) as exc:
        raise VisionError(f"Could not load calibration from {directory}: {exc}") from exc
    return Calibration.from_json(data), read_image(directory / "empty_warp.png")


def annotated_board(aligned: np.ndarray, observation: Observation) -> np.ndarray:
    output = aligned.copy()
    for row in range(8):
        for col in range(8):
            square = f"{'abcdefgh'[col]}{8 - row}"
            left, top = col * SQUARE, row * SQUARE
            color = (0, 165, 255) if square in observation.uncertain else ((0, 200, 0) if square in observation.occupied else (220, 220, 220))
            cv2.rectangle(output, (left, top), (left + SQUARE - 1, top + SQUARE - 1), color, 2)
            cv2.putText(output, square, (left + 5, top + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    return output


def annotated_changes(aligned: np.ndarray, changes: SquareChanges) -> np.ndarray:
    output = aligned.copy()
    for row in range(8):
        for col in range(8):
            square = f"{'abcdefgh'[col]}{8 - row}"
            if square in changes.vacated:
                color = (0, 0, 220)  # red
            elif square in changes.filled:
                color = (0, 200, 0)  # green
            elif square in changes.uncertain:
                color = (0, 165, 255)  # orange
            else:
                color = (220, 220, 220)
            left, top = col * SQUARE, row * SQUARE
            cv2.rectangle(output, (left, top), (left + SQUARE - 1, top + SQUARE - 1), color, 2)
            cv2.putText(output, square, (left + 5, top + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    return output
