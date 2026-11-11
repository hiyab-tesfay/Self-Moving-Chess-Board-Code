"""Offline, model-free square occupancy detection from still board photos."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    if (not np.isfinite(source).all() or not cv2.isContourConvex(source) or
            cv2.contourArea(source, oriented=True) < 10000):
        raise VisionError("Select the four corners clockwise: top-left, top-right, bottom-right, bottom-left.")
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


def _aligned_lab_images(empty_board: np.ndarray, observed_board: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if empty_board.shape != (SIZE, SIZE, 3) or observed_board.shape != empty_board.shape:
        raise VisionError("Reference and observed boards must both be aligned 800x800 color images.")
    reference = cv2.GaussianBlur(cv2.cvtColor(empty_board, cv2.COLOR_BGR2LAB), (5, 5), 0).astype(np.float32)
    observed = cv2.GaussianBlur(cv2.cvtColor(observed_board, cv2.COLOR_BGR2LAB), (5, 5), 0).astype(np.float32)
    return reference, observed


def legacy_square_scores(empty_board: np.ndarray, observed_board: np.ndarray) -> dict[str, float]:
    """Original raw photo difference, retained for existing saved calibrations."""
    reference, observed = _aligned_lab_images(empty_board, observed_board)
    difference = np.linalg.norm(observed - reference, axis=2)
    scores = {}
    for rank in range(8):
        for file in range(8):
            # Discard grid lines and the edges where a neighboring piece can intrude.
            patch = difference[rank * SQUARE + 18:(rank + 1) * SQUARE - 18,
                               file * SQUARE + 18:(file + 1) * SQUARE - 18]
            scores[f"{'abcdefgh'[file]}{8 - rank}"] = float(np.mean(patch))
    return scores


def square_scores(empty_board: np.ndarray, observed_board: np.ndarray) -> dict[str, float]:
    """Measure pieces after correcting each square for local lighting change.

    The small corner patches normally show bare board even when a piece sits in
    the middle. Their median color shift estimates the exposure/light change
    between photos at this square. Only the unexplained center change counts.
    """
    reference, observed = _aligned_lab_images(empty_board, observed_board)
    scores = {}
    for row in range(8):
        for col in range(8):
            top, left = row * SQUARE, col * SQUARE
            before = reference[top:top + SQUARE, left:left + SQUARE]
            after = observed[top:top + SQUARE, left:left + SQUARE]
            change = after - before
            corners = np.concatenate([
                change[:15, :15].reshape(-1, 3),
                change[:15, -15:].reshape(-1, 3),
                change[-15:, :15].reshape(-1, 3),
                change[-15:, -15:].reshape(-1, 3),
            ])
            lighting_shift = np.median(corners, axis=0)
            center = change[18:SQUARE - 18, 18:SQUARE - 18] - lighting_shift
            scores[f"{'abcdefgh'[col]}{8 - row}"] = float(np.mean(np.linalg.norm(center, axis=2)))
    return scores


def _register_grid(reference: np.ndarray, observed: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    """Correct small corner-tap errors using the grid, away from piece centers.

    This is deliberately a small correction. A moved camera still needs current
    board corners; a large transform or an unreadable grid is never guessed.
    """
    _aligned_lab_images(reference, observed)  # Validate image sizes first.
    scale = 0.5
    shape = (round(SIZE * scale), round(SIZE * scale))

    def grid_edges(image: np.ndarray) -> np.ndarray:
        gray = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), shape,
                          interpolation=cv2.INTER_AREA).astype(np.float32)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        edge = cv2.magnitude(cv2.Sobel(gray, cv2.CV_32F, 1, 0),
                             cv2.Sobel(gray, cv2.CV_32F, 0, 1))
        return np.minimum(edge, max(float(np.percentile(edge, 98)), 1.0))

    template, current = grid_edges(reference), grid_edges(observed)
    mask = np.zeros(shape, dtype=np.uint8)
    for line in range(1, 8):
        position = round(line * SQUARE * scale)
        mask[position - 6:position + 7, 10:-10] = 255
        mask[10:-10, position - 6:position + 7] = 255
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        shift, response = cv2.phaseCorrelate(template, current,
                                             cv2.createHanningWindow(shape, cv2.CV_32F))
        if response > 0.1 and max(abs(value) for value in shift) <= 10:
            warp[:, 2] = shift
        correlation, warp = cv2.findTransformECC(
            template, current, warp, cv2.MOTION_AFFINE,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 55, 1e-4), mask, 3)
    except cv2.error as exc:
        raise VisionError("The board grid is unclear. Keep the camera still, remove hands, and check the corners.") from exc
    warp[:, 2] /= scale
    corners = np.float32([[0, 0], [SIZE - 1, 0], [SIZE - 1, SIZE - 1], [0, SIZE - 1]])
    transformed = cv2.transform(corners[None, :, :], warp)[0]
    displacement = float(np.max(np.linalg.norm(transformed - corners, axis=1)))
    if not np.isfinite(warp).all() or correlation < 0.70 or displacement > 14:
        raise VisionError("The photo does not line up with calibration. Adjust the photo corners or recalibrate.")
    aligned = cv2.warpAffine(observed, warp, (SIZE, SIZE),
                             flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                             borderMode=cv2.BORDER_REPLICATE)
    return aligned, {"grid_correlation": float(correlation), "alignment_pixels": displacement}


def robust_square_scores(empty_board: np.ndarray, observed_board: np.ndarray) -> dict[str, float]:
    """Foreground evidence after exposure and local illumination correction.

    Unlike an average of the whole square, the strongest supported center
    residual still detects a small pawn. Fine detail helps separate a physical
    object from smooth lighting changes. Grid registration happens in observe.
    """
    reference, observed = _aligned_lab_images(empty_board, observed_board)
    corner_slices = ((slice(8, 22), slice(8, 22)), (slice(8, 22), slice(-22, -8)),
                     (slice(-22, -8), slice(8, 22)), (slice(-22, -8), slice(-22, -8)))
    reference_samples, observed_samples = [], []
    for row in range(8):
        for col in range(8):
            before = reference[row * SQUARE:(row + 1) * SQUARE, col * SQUARE:(col + 1) * SQUARE]
            after = observed[row * SQUARE:(row + 1) * SQUARE, col * SQUARE:(col + 1) * SQUARE]
            for region in corner_slices:
                reference_samples.append(np.median(before[region], axis=(0, 1)))
                observed_samples.append(np.median(after[region], axis=(0, 1)))
    before_samples = np.asarray(reference_samples)
    after_samples = np.asarray(observed_samples)
    gains = np.ones(3, dtype=np.float32)
    for channel in range(3):
        x, y = before_samples[:, channel], after_samples[:, channel]
        if float(np.std(x)) < 4:
            continue
        keep = np.ones(len(x), dtype=bool)
        for _ in range(3):
            gain, offset = np.linalg.lstsq(np.column_stack((x[keep], np.ones(keep.sum()))),
                                           y[keep], rcond=None)[0]
            error = y - (gain * x + offset)
            center = np.median(error)
            tolerance = max(5.0, float(np.median(np.abs(error - center))) * 3.5)
            keep = np.abs(error - center) <= tolerance
        gains[channel] = np.clip(gain, 0.65, 1.5)
    scores = {}
    yy, xx = np.mgrid[:SQUARE, :SQUARE].astype(np.float32)
    # Corner patch centers are at 14.5 and 84.5 pixels.
    tx = np.clip((xx - 14.5) / 70, 0, 1)[:, :, None]
    ty = np.clip((yy - 14.5) / 70, 0, 1)[:, :, None]
    center_mask = (xx - 49.5) ** 2 + (yy - 49.5) ** 2 <= 36 ** 2
    for row in range(8):
        for col in range(8):
            before = reference[row * SQUARE:(row + 1) * SQUARE, col * SQUARE:(col + 1) * SQUARE]
            after = observed[row * SQUARE:(row + 1) * SQUARE, col * SQUARE:(col + 1) * SQUARE]
            change = after - before * gains
            offsets = [np.median(change[region], axis=(0, 1)) for region in corner_slices]
            illumination = ((1 - ty) * ((1 - tx) * offsets[0] + tx * offsets[1]) +
                            ty * ((1 - tx) * offsets[2] + tx * offsets[3]))
            residual = change - illumination
            color = np.linalg.norm(residual, axis=2)
            detail = np.linalg.norm(residual - cv2.GaussianBlur(residual, (0, 0), 9), axis=2)
            bare_noise = np.median(np.concatenate([color[region].ravel() for region in corner_slices]))
            values = color[center_mask]
            details = detail[center_mask]
            count = max(1, len(values) // 4)
            signal = max(0.0, float(np.mean(np.partition(values, -count)[-count:])) - 1.5 * bare_noise)
            texture = max(0.0, float(np.mean(np.partition(details, -count)[-count:])) - bare_noise)
            scores[f"{'abcdefgh'[col]}{8 - row}"] = float(np.sqrt(signal * texture))
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
    rotation: str = "none"
    scoring: str = "local_lighting_v1"

    def to_json(self) -> dict:
        return {"corners": self.corners, "thresholds": self.thresholds,
                "margins": self.margins, "rotation": self.rotation, "scoring": self.scoring}

    @classmethod
    def from_json(cls, data: dict) -> "Calibration":
        rotation = data.get("rotation", "none")
        if rotation not in ("none", "cw", "ccw", "180"):
            raise VisionError(f"Invalid calibration rotation: {rotation}")
        scoring = data.get("scoring", "legacy")
        if scoring not in ("legacy", "local_lighting_v1", "temporal_v1", "robust_occupancy_v2"):
            raise VisionError(f"Invalid calibration scoring method: {scoring}")
        return cls(data["corners"], tuple(data["thresholds"]), tuple(data["margins"]),
                   rotation, scoring)


def calibrate(empty: np.ndarray, starting: np.ndarray, corners: list[list[float]] | None = None,
              rotation: str = "none", *,
              starting_corners: list[list[float]] | None = None) -> tuple[Calibration, np.ndarray, dict[str, float]]:
    """Calibrate from separately aligned photos; save starting corners for later scans."""
    reference = rotate_board(warp_board(empty, corners), rotation)
    scan_corners = starting_corners if starting_corners is not None else corners
    setup = rotate_board(warp_board(starting, scan_corners), rotation)
    setup, _quality = _register_grid(reference, setup)
    scores = robust_square_scores(reference, setup)
    occupied = frozenset(chess.square_name(square) for square in chess.Board().piece_map())
    thresholds = []
    margins = []
    for group in (0, 1):
        empty_max = max(score for square, score in scores.items() if _group(square) == group and square not in occupied)
        occupied_min = min(score for square, score in scores.items() if _group(square) == group and square in occupied)
        if occupied_min - empty_max < 5:
            weak = sorted(square for square, score in scores.items()
                          if _group(square) == group and square in occupied and score < empty_max + 5)
            raise VisionError(
                f"Calibration failed for {'light' if group else 'dark'} squares: "
                f"empty max {empty_max:.1f}, occupied min {occupied_min:.1f}. "
                + ("Pieces are unclear on " + ", ".join(weak) + ". " if weak else "") +
                "Check corners, focus, glare, and the starting pieces before taking the setup photo again."
            )
        thresholds.append((empty_max + occupied_min) / 2)
        margins.append(max(1.5, (occupied_min - empty_max) * 0.2))
    return Calibration(scan_corners, tuple(thresholds), tuple(margins), rotation,
                       "robust_occupancy_v2"), reference, scores


@dataclass(frozen=True)
class Observation:
    occupied: frozenset[str]
    uncertain: tuple[str, ...]
    scores: dict[str, float]
    confidence: dict[str, float] = field(default_factory=dict)
    quality: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TemporalObservation:
    """Squares that visibly changed since the last verified board photo."""

    changed: tuple[str, ...]
    scores: dict[str, float]


def temporal_changes(scores: dict[str, float]) -> TemporalObservation:
    if set(scores) != set(chess.SQUARE_NAMES):
        raise VisionError("Expected one change score for every chess square.")
    ordered = sorted(scores.items(), key=lambda item: -item[1])
    baseline = float(np.median(list(scores.values())))
    gaps = [(ordered[index - 1][1] - ordered[index][1], index)
            for index in range(1, min(9, len(ordered)))
            if ordered[index - 1][1] >= max(12.0, baseline + 8.0) and
            ordered[index - 1][1] - ordered[index][1] >= 8.0]
    count = max(gaps)[1] if gaps else 0
    return TemporalObservation(tuple(sorted(square for square, _ in ordered[:count])), scores)


def observe_temporal(reference: np.ndarray, photo: np.ndarray, calibration: Calibration,
                     corners: list[list[float]] | None = None) -> tuple[TemporalObservation, np.ndarray]:
    if calibration.scoring != "temporal_v1":
        raise VisionError("Temporal observation requires setup-only calibration.")
    aligned = rotate_board(warp_board(photo, corners if corners is not None else calibration.corners),
                           calibration.rotation)
    return temporal_changes(square_scores(reference, aligned)), aligned


def observe(reference: np.ndarray, photo: np.ndarray, calibration: Calibration,
            corners: list[list[float]] | None = None) -> tuple[Observation, np.ndarray]:
    if calibration.scoring == "temporal_v1":
        raise VisionError("Setup-only calibration needs the touchscreen's last verified photo.")
    aligned = rotate_board(warp_board(photo, corners if corners is not None else calibration.corners),
                           calibration.rotation)
    quality = {}
    if calibration.scoring == "robust_occupancy_v2":
        aligned, quality = _register_grid(reference, aligned)
        scoring = robust_square_scores
    else:
        scoring = legacy_square_scores if calibration.scoring == "legacy" else square_scores
    scores = scoring(reference, aligned)
    occupied = frozenset(square for square, score in scores.items() if score > calibration.thresholds[_group(square)])
    uncertain = tuple(sorted(square for square, score in scores.items()
                             if abs(score - calibration.thresholds[_group(square)]) <= calibration.margins[_group(square)]))
    confidence = {square: float(1 - np.exp(-abs(score - calibration.thresholds[_group(square)]) /
                                         max(calibration.margins[_group(square)], 0.1)))
                  for square, score in scores.items()}
    return Observation(occupied, uncertain, scores, confidence, quality), aligned


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
