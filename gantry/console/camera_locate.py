"""Find the gantry's calibration piece (or printed token) on the board with the Pi's chess camera.

Two targets:

* target="piece" (default): an ordinary chess piece that the gantry magnet
  drags around under the board. The board is otherwise EMPTY. The photo is
  compared with a photo of the empty board (the "reference", best taken with
  capture_reference() just before the piece is put down); the one changed blob
  is the piece. A tall piece seen from an oblique camera projects onto the
  board displaced away from the point straight under the camera (the nadir),
  so the blob centre is corrected for parallax with a camera pose estimated
  from the board corners (see CameraLocator and _camera_pose).
* target="token": a flat disc carrying ArUco marker id 10 (DICT_4X4_50). The
  marker is found in the ORIGINAL photo (sub-pixel corner refinement) and its
  centre is the intersection of the marker's diagonals.

Either way the point is mapped through exactly the homography and rotation the
chess app's board_vision uses, so the answer is in the same frame the app
uses to name squares.

Board frame used by board_vision (verified from its code and real photos):
warp_board maps the photo's board corners TL, TR, BR, BL to warped pixels
(0,0), (799,0), (799,799), (0,799); rotate_board then turns the 800x800 image
so a8 is at the top left. Squares are named f"{'abcdefgh'[col]}{8 - row}" with
col = x // 100, row = y // 100, so after rotation +x runs a->h and +y runs
rank 8 -> rank 1.

Result convention (board_mm): origin at a1's centre, +x toward the h-file,
+y toward rank 8, so h8's centre is (7*sx, 7*sy). Heights (z) are up from the
board surface.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np


TOKEN_ID = 10
TOKEN_MARKER_MM = 20.0
BOARD_PX = 799.0               # warped board spans pixel coordinates 0..799 (board_vision.warp_board)
DEFAULT_APP_DIR = Path("~/chess-board-pi-only")
KEEP_PHOTOS = 20               # older camera_cal photos are deleted
ANNOTATE_PAD = 60              # pixels of margin around the board in the annotated picture
_ROTATIONS = ("none", "cw", "ccw", "180")
TARGETS = ("piece", "token")

# Piece detection works on the board warped to PIECE_PX pixels (about 0.27 mm per
# pixel on a 424 mm board) plus half a square of margin all round: a tall piece
# near the edge leans outward in the photo and must not be cut off.
PIECE_PX = 1600
PIECE_PAD = PIECE_PX // 16
PIECE_MIN_MM2 = math.pi * 6.0 ** 2      # smaller than a 12 mm disc is not a piece
PIECE_MAX_SQUARES = 2.5                 # larger than 2.5 squares is a hand, a box, ...
SECOND_BLOB_RATIO = 0.35                # a second blob this big (of the first) means two things

# Horizontal field of view of the Pi cameras at their full-resolution still size
# (rpicam-still's default output, which pi_camera uses): width -> degrees.
_HFOV_BY_WIDTH = {
    2592: 53.5,    # Camera Module v1 (OV5647), 2592x1944
    3280: 62.2,    # Camera Module v2 (IMX219), 3280x2464
    4608: 66.0,    # Camera Module 3 (IMX708), 4608x2592
    2304: 66.0,    # Camera Module 3, 2x2 binned 2304x1296
}
DEFAULT_HFOV = 66.0


class LocateError(Exception):
    """The piece/token or the board could not be found. str() is plain words for the screen."""


def find_app_dir(explicit: str | None = None) -> Path | None:
    """The chess camera app's folder: explicit, else $CHESS_APP_DIR, else ~/chess-board-pi-only.

    A path given explicitly (or by the environment variable) that is not a
    chess app folder gives None rather than silently falling back to another.
    """
    for candidate in (explicit, os.environ.get("CHESS_APP_DIR")):
        if candidate:
            path = Path(candidate).expanduser()
            return path if (path / "board_vision.py").is_file() else None
    path = DEFAULT_APP_DIR.expanduser()
    return path if (path / "board_vision.py").is_file() else None


def default_hfov(width: int) -> float:
    """The likely horizontal field of view (degrees) of a Pi camera photo this many pixels wide."""
    return _HFOV_BY_WIDTH.get(int(width), DEFAULT_HFOV)


def _square_xy(square_mm) -> tuple[float, float]:
    if isinstance(square_mm, (list, tuple)):
        if len(square_mm) != 2:
            raise ValueError("square_mm must be a number or an (x, y) pair")
        sx, sy = float(square_mm[0]), float(square_mm[1])
    else:
        sx = sy = float(square_mm)
    if not (sx > 0 and sy > 0):
        raise ValueError("square_mm must be positive")
    return sx, sy


def rotate_point(x: float, y: float, rotation: str, n: float = BOARD_PX) -> tuple[float, float]:
    """Where cv2.rotate (as used by board_vision.rotate_board) sends pixel (x, y) of an (n+1)-wide square image."""
    if rotation == "none":
        return x, y
    if rotation == "cw":        # ROTATE_90_CLOCKWISE: (x, y) -> (n - y, x)
        return n - y, x
    if rotation == "ccw":       # ROTATE_90_COUNTERCLOCKWISE: (x, y) -> (y, n - x)
        return y, n - x
    if rotation == "180":
        return n - x, n - y
    raise LocateError(f"Unknown board rotation '{rotation}' in the camera calibration.")


def _rotate_image(image: np.ndarray, rotation: str) -> np.ndarray:
    codes = {"cw": cv2.ROTATE_90_CLOCKWISE, "ccw": cv2.ROTATE_90_COUNTERCLOCKWISE, "180": cv2.ROTATE_180}
    return cv2.rotate(image, codes[rotation]) if rotation in codes else image


def _line_intersection(p1, p2, p3, p4) -> np.ndarray:
    """Intersection of line p1-p2 with line p3-p4 (the marker's two diagonals)."""
    a = np.array([[p2[0] - p1[0], p3[0] - p4[0]], [p2[1] - p1[1], p3[1] - p4[1]]], dtype=np.float64)
    b = np.array([p3[0] - p1[0], p3[1] - p1[1]], dtype=np.float64)
    t = np.linalg.solve(a, b)[0]
    return np.asarray(p1, dtype=np.float64) + t * (np.asarray(p2, dtype=np.float64) - np.asarray(p1, dtype=np.float64))


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def _read(image, what: str) -> np.ndarray:
    """A BGR uint8 array from an array or an image path."""
    if isinstance(image, (str, Path)):
        path = image
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise LocateError(f"Could not read the {what} {path}.")
    if image is None or not isinstance(image, np.ndarray) or image.ndim < 2:
        raise LocateError(f"No {what} to look at.")
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    return image


class CameraLocator:
    """Measure where the calibration piece (or token) sits on the board.

    square_mm: one square's size in mm, a float or an (x_mm, y_mm) pair.
    app_dir: the chess camera app (board_vision, pi_camera, calibration/); default find_app_dir().
    token_id: the token's ArUco id (target="token").
    camera: any object with .capture(path) -> path; None builds pi_camera.Camera
        from the app's calibration/camera_settings.json, as pi_touch does.
    photo_dir: where raw, reference and annotated photos go (default: camera_cal/ next to this file).
    target: "piece" (a chess piece on an otherwise empty board, the default) or
        "token" (the printed ArUco token).
    piece_height_mm: MEASURE THIS: the height of the calibration piece from the
        board surface to its top, in mm (a pawn is ~35-45 mm, a rook ~40-50 mm).
        It sets the parallax correction (see centroid_height_frac). The console's
        closed loop (drag, look, correct) cancels a CONSTANT camera error near
        each square only if this model is right; a wrong height leaves an error
        that grows with the distance from the nadir (about distance * dz / H,
        e.g. 300 mm * 10 mm / 580 mm ~ 5 mm at the far corner for a 20 mm
        height error), so it matters.
    token_height_mm: height of the token's top (target="token"). Unused: the token
        is flat (a few mm), so parallax is ignored for it.
    token_marker_mm: the token's black marker side, used for its confidence.
    reference: a photo of the EMPTY board (a BGR array or a path), as the camera
        takes it. Default: the chess app's calibration/empty.jpg when it is a
        photo of this camera's size, else calibration/empty_warp.png (the app's
        straightened empty board). capture_reference() replaces it with a fresh
        photo, which is much better: lighting drifts and the camera settles.
    hfov_deg: the camera's horizontal field of view in degrees, used for the
        solvePnP intrinsics (default 66, Camera Module 3). None guesses it from
        the photo width (2592 px: Camera Module v1 53.5 deg, 3280: v2 62.2,
        4608/2304: Camera Module 3 66; otherwise 66). A 2592x1944 photo is what
        a Camera Module v1 gives by default: then 53.5 is the right value.
    camera_height_mm: optional measured height of the lens above the board. When
        given, the focal length is adjusted so the estimated pose has this height
        (the field-of-view guess is then not needed). Recommended if the camera
        model is unknown: the parallax correction scales with 1/height.
    centroid_height_frac: how the blob centroid is corrected for parallax.
        None (default): the piece is modelled as an upright cylinder of
        piece_height_mm whose radius is chosen so its projected silhouette has
        the blob's area; the foot is moved until the model silhouette's centroid
        equals the blob's (a few fixed-point steps). For a straight-sided piece
        this is exact; it behaves like seeing the centroid at a little over half
        the height. A number f instead takes the centroid to be seen at
        f * piece_height_mm and corrects with p = n + (q - n) (H - z) / H
        (n nadir, H camera height). A piece that is wide at the base and slim at
        the top looks lower than a cylinder: f ~ 0.4 may suit it better.
    """

    def __init__(self, square_mm, app_dir: Path | None = None, token_id: int = TOKEN_ID,
                 camera=None, photo_dir: Path | None = None, *, target: str = "piece",
                 piece_height_mm: float = 45.0, token_height_mm: float = 0.0,
                 token_marker_mm: float = TOKEN_MARKER_MM, reference=None,
                 hfov_deg: float | None = DEFAULT_HFOV, camera_height_mm: float | None = None,
                 centroid_height_frac: float | None = None):
        self.sx, self.sy = _square_xy(square_mm)
        if target not in TARGETS:
            raise ValueError(f"target must be one of {TARGETS}")
        self.target = target
        self.app_dir = Path(app_dir).expanduser() if app_dir is not None else find_app_dir()
        if self.app_dir is None or not (self.app_dir / "board_vision.py").is_file():
            raise LocateError("The chess camera app was not found. Set CHESS_APP_DIR to its folder "
                              "(normally ~/chess-board-pi-only).")
        self.token_id = int(token_id)
        self.token_height_mm = float(token_height_mm)   # unused: flat token, parallax ignored
        self.token_marker_mm = float(token_marker_mm)
        self.piece_height_mm = float(piece_height_mm)
        if not (0.0 <= self.piece_height_mm < 300.0):
            raise ValueError("piece_height_mm must be between 0 and 300")
        self.centroid_height_frac = None if centroid_height_frac is None else float(centroid_height_frac)
        self.hfov_deg = None if hfov_deg is None else float(hfov_deg)
        if self.hfov_deg is not None and not (10.0 < self.hfov_deg < 170.0):
            raise ValueError("hfov_deg must be between 10 and 170 degrees")
        self.camera_height_mm = None if camera_height_mm is None else float(camera_height_mm)
        self.photo_dir = Path(photo_dir) if photo_dir is not None else Path(__file__).resolve().parent / "camera_cal"
        self.last_photo: str | None = None
        self._reference = reference           # array, path, or None (the app's empty board)
        self._reference_from = "given" if reference is not None else None
        self._pose_cache: dict = {}
        self._bv = self._import("board_vision")
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        params.cornerRefinementWinSize = 5
        params.cornerRefinementMaxIterations = 50
        params.cornerRefinementMinAccuracy = 0.01
        self._detector = cv2.aruco.ArucoDetector(dictionary, params)
        self.camera = camera if camera is not None else self._default_camera()

    # ----- setup -------------------------------------------------------------------------
    def _import(self, name: str):
        path = str(self.app_dir)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            return __import__(name)
        except ImportError as exc:
            raise LocateError(f"Could not load the chess camera app's {name}: {exc}") from exc

    def _default_camera(self):
        pi_camera = self._import("pi_camera")
        settings = None
        try:
            data = json.loads((self.app_dir / "calibration" / "camera_settings.json").read_text())
            settings = pi_camera.CameraSettings(**data)
        except (OSError, ValueError, TypeError):
            settings = None
        return pi_camera.Camera("pi", 0, settings or pi_camera.CameraSettings())

    def _calibration(self) -> dict:
        try:
            return json.loads((self.app_dir / "calibration" / "calibration.json").read_text())
        except (OSError, ValueError):
            return {}

    def _capture(self, prefix: str, stamp: str) -> np.ndarray:
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        raw = self.photo_dir / f"{prefix}{stamp}.jpg"
        try:
            path = self.camera.capture(raw)
        except Exception as exc:   # pi_camera.CameraError and anything a fake camera raises
            raise LocateError(f"The camera could not take a photo: {exc}") from exc
        path = Path(path) if path is not None else raw
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise LocateError("The camera photo could not be read.")
        return image

    # ----- public ------------------------------------------------------------------------
    def capture_reference(self) -> str:
        """Photograph the EMPTY board now and use it as the reference for target="piece".

        Call it with the board completely clear (no pieces, no hands, gantry
        parked out of view) right before the piece is put down. Returns the
        saved photo's path. The board corners must be findable in it.
        """
        stamp = _stamp()
        image = self._capture("reference_", stamp)
        calibration = self._calibration()
        self._board_corners(image, self._detect(image), calibration.get("corners"))   # LocateError if unusable
        self._reference = image
        self._reference_from = "captured"
        self._prune()
        return str(self.photo_dir / f"reference_{stamp}.jpg")

    def locate(self) -> dict:
        """Take a photo with the camera, then locate_image on it."""
        stamp = _stamp()
        image = self._capture("raw_", stamp)
        try:
            return self.locate_image(image, _stamp=stamp)
        finally:
            self._prune()

    def locate_image(self, image, *, _stamp: str | None = None) -> dict:
        """Find the piece (or token) in one photo (a BGR array or an image path).

        Returns {"board_mm": [x, y], "square": "e4", "confidence": 0..1,
        "photo": annotated jpg path, "px": [u, v], "target": "piece"|"token"}
        plus extras: "offset_mm" (position minus nearest square centre),
        "on_board", "corners_from" ("markers" or "saved"), "rotation", "raw_px"
        (the point in the photo). px is the point in the app's warped, rotated
        800x800 board image.

        target="piece" adds: "blob_area_px" (in the 1600 px board image),
        "blob_area_mm2", "parallax_mm" (length of the correction applied),
        "parallax_vec_mm", "raw_board_mm" (uncorrected blob centroid),
        "base_edge_mm" (independent estimate from the blob's nadir-side edge,
        or None), "edge_check_mm" (distance between the two), "clipped" (blob
        touches the photo edge), "reference" (where the empty board came from),
        "camera": {"height_mm", "nadir_mm", "hfov_deg", "focal_px", "reproj_px"}.
        target="token" adds "marker_side_mm".
        """
        image = _read(image, "photo")
        calibration = self._calibration()
        rotation = calibration.get("rotation", "none")
        if rotation not in _ROTATIONS:
            raise LocateError(f"Unknown board rotation '{rotation}' in the camera calibration.")
        detected = self._detect(image)
        corners, corners_from = self._board_corners(image, detected, calibration.get("corners"))
        transform = cv2.getPerspectiveTransform(
            corners, np.float32([[0, 0], [BOARD_PX, 0], [BOARD_PX, BOARD_PX], [0, BOARD_PX]]))
        if self.target == "token":
            return self._locate_token(image, detected, transform, rotation, corners_from, _stamp)
        return self._locate_piece(image, corners, transform, rotation, corners_from, calibration, _stamp)

    # ----- token -------------------------------------------------------------------------
    def _locate_token(self, image, detected, transform, rotation, corners_from, stamp) -> dict:
        marker = self._find_token(detected)                    # 4x2 photo pixels, ArUco order TL TR BR BL
        centre = _line_intersection(marker[0], marker[2], marker[1], marker[3])
        warped = cv2.perspectiveTransform(np.vstack([centre, marker]).reshape(-1, 1, 2).astype(np.float64),
                                          transform.astype(np.float64)).reshape(-1, 2)
        oriented = np.array([rotate_point(x, y, rotation) for x, y in warped])
        u, v = oriented[0]
        x_mm, y_mm = self._to_mm(u, v)

        corner_mm = np.array([self._to_mm(px, py) for px, py in oriented[1:]])
        sides = np.linalg.norm(corner_mm - np.roll(corner_mm, -1, axis=0), axis=1)
        diagonals = (np.linalg.norm(corner_mm[0] - corner_mm[2]), np.linalg.norm(corner_mm[1] - corner_mm[3]))
        side = float(np.mean(sides))
        confidence = self._confidence(sides, diagonals, side, corners_from)

        result = self._base_result("token", x_mm, y_mm, u, v, confidence, corners_from, rotation, centre)
        result["marker_side_mm"] = round(side, 2)

        def draw(board, at):
            outline = np.array([at(pu, pv) for pu, pv in oriented[1:]], dtype=np.int32)
            cv2.polylines(board, [outline], True, (0, 200, 255), 2, cv2.LINE_AA)

        result["photo"] = self._annotate(image, transform, rotation, result, stamp, draw,
                                         [f"token {result['square']}: {result['offset_mm'][0]:+.1f} mm toward h, "
                                          f"{result['offset_mm'][1]:+.1f} mm toward 8"])
        self.last_photo = result["photo"]
        return result

    def _find_token(self, detected) -> np.ndarray:
        found = detected.get(self.token_id, [])
        if not found:
            raise LocateError("The calibration token was not seen. Check it sits flat, face up, "
                              "inside the camera view and is not hidden by a hand or the gantry.")
        if len(found) > 1:
            raise LocateError(f"The calibration token's marker was seen {len(found)} times. "
                              "Leave only one token on the board.")
        return found[0]

    def _confidence(self, sides, diagonals, side, corners_from) -> float:
        """1.0 for a crisp, square marker of the expected size seen through markers-found corners.

        The marker's corners, mapped to the board, should form a square of
        token_marker_mm. Shape distortion means poor corners or a tilted token;
        a wrong size means stale board corners or the wrong print scale.
        """
        mean = max(side, 1e-6)
        shape = float(np.std(sides) / mean) + abs(diagonals[0] - diagonals[1]) / (mean * np.sqrt(2))
        size = abs(side / self.token_marker_mm - 1.0) if self.token_marker_mm > 0 else 0.0
        confidence = np.exp(-shape / 0.05) * np.exp(-max(0.0, size - 0.03) / 0.10)
        if corners_from == "saved":
            confidence *= 0.9   # saved corners are only right while the camera has not moved
        return float(np.clip(confidence, 0.0, 1.0))

    # ----- piece -------------------------------------------------------------------------
    def _canvas_transform(self, corners: np.ndarray, rotation: str) -> np.ndarray:
        """Homography photo -> the piece canvas: the oriented board (a8 top left) at PIECE_PX plus PIECE_PAD."""
        k = PIECE_PX / BOARD_PX
        warp = [(0, 0), (BOARD_PX, 0), (BOARD_PX, BOARD_PX), (0, BOARD_PX)]
        dst = np.float32([[PIECE_PAD + k * u, PIECE_PAD + k * v] for u, v in
                          (rotate_point(x, y, rotation) for x, y in warp)])
        return cv2.getPerspectiveTransform(np.float32(corners), dst)

    def _canvas_to_mm(self, cu, cv_):
        """Piece-canvas pixels -> board_mm (vectorised)."""
        k = BOARD_PX / PIECE_PX
        u = (np.asarray(cu, dtype=np.float64) - PIECE_PAD) * k
        v = (np.asarray(cv_, dtype=np.float64) - PIECE_PAD) * k
        return self._to_mm(u, v)

    def _mm_to_oriented(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        squares = BOARD_PX / 8.0
        return (x_mm / self.sx + 0.5) * squares, (7.5 - y_mm / self.sy) * squares

    def _reference_canvas(self, shape, rotation, calibration) -> tuple[np.ndarray, np.ndarray, str]:
        """The empty board on the piece canvas: (BGR image, valid mask, where it came from)."""
        size = PIECE_PX + 2 * PIECE_PAD
        source = self._reference_from
        reference = self._reference
        if reference is None:
            empty = self.app_dir / "calibration" / "empty.jpg"
            image = cv2.imread(str(empty), cv2.IMREAD_COLOR) if empty.is_file() else None
            if image is not None and image.shape[:2] == tuple(shape[:2]):
                reference, source = image, "empty.jpg"
            else:
                board = self.app_dir / "calibration" / "empty_warp.png"
                image = cv2.imread(str(board), cv2.IMREAD_COLOR) if board.is_file() else None
                if image is None:
                    raise LocateError("There is no photo of the empty board to compare with. Clear the board "
                                      "and take one (capture_reference) before putting the piece down.")
                reference, source = image, "empty_warp.png"
        reference = _read(reference, "empty-board photo")
        if reference.shape[:2] == tuple(shape[:2]):
            ref_corners, _ = self._board_corners(reference, self._detect(reference), calibration.get("corners"))
            transform = self._canvas_transform(ref_corners, rotation)
            canvas = cv2.warpPerspective(reference, transform, (size, size), flags=cv2.INTER_LINEAR,
                                         borderMode=cv2.BORDER_REPLICATE)
            valid = cv2.warpPerspective(np.full(reference.shape[:2], 255, np.uint8), transform, (size, size),
                                        flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            return canvas, valid, source
        if reference.shape[0] == reference.shape[1] and reference.shape[0] in (800, int(BOARD_PX) + 1):
            # An already straightened, rotated board (board_vision's 800x800 frame, like empty_warp.png).
            k = PIECE_PX / BOARD_PX
            affine = np.float32([[k, 0, PIECE_PAD], [0, k, PIECE_PAD]])
            canvas = cv2.warpAffine(reference, affine, (size, size), flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_REPLICATE)
            valid = cv2.warpAffine(np.full(reference.shape[:2], 255, np.uint8), affine, (size, size),
                                   flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            return canvas, valid, source or "board image"
        raise LocateError("The empty-board photo is a different size from the camera photo. Clear the board and "
                          "take a new one (capture_reference) before putting the piece down.")

    def _camera_pose(self, corners: np.ndarray, rotation: str, shape) -> dict:
        """The camera centre in board mm from the four board corners (cached per corners set).

        cv2.solvePnP (IPPE, planar) with the board corners at z=0 and a pinhole
        camera whose focal length comes from the horizontal field of view (or
        is scaled until the height equals camera_height_mm). Principal point at
        the image centre, no lens distortion. With a nearly overhead camera the
        height scales with the focal length guess, so the field of view (or a
        measured height) matters for the parallax correction; the nadir hardly
        depends on it.
        """
        h, w = shape[:2]
        key = (tuple(np.round(np.asarray(corners, np.float64).ravel(), 3)), rotation, w, h, self.sx, self.sy,
               self.hfov_deg, self.camera_height_mm)
        if key in self._pose_cache:
            return self._pose_cache[key]
        hfov = self.hfov_deg if self.hfov_deg is not None else default_hfov(w)
        warp = [(0, 0), (BOARD_PX, 0), (BOARD_PX, BOARD_PX), (0, BOARD_PX)]
        board = np.array([[*self._to_mm(*rotate_point(x, y, rotation)), 0.0] for x, y in warp], dtype=np.float64)
        image_points = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
        focal = (w / 2.0) / math.tan(math.radians(hfov) / 2.0)
        for _ in range(8 if self.camera_height_mm else 1):
            pose = self._solve_pose(board, image_points, focal, w, h)
            if not self.camera_height_mm:
                break
            focal *= self.camera_height_mm / pose["height_mm"]
            hfov = math.degrees(2.0 * math.atan((w / 2.0) / focal))
            pose = self._solve_pose(board, image_points, focal, w, h)
        pose["hfov_deg"] = round(hfov, 2)
        self._pose_cache = {key: pose}
        return pose

    @staticmethod
    def _solve_pose(board, image_points, focal, w, h) -> dict:
        matrix = np.array([[focal, 0, w / 2.0], [0, focal, h / 2.0], [0, 0, 1]], dtype=np.float64)
        try:
            ok, rvec, tvec = cv2.solvePnP(board, image_points, matrix, None, flags=cv2.SOLVEPNP_IPPE)
        except cv2.error as exc:
            raise LocateError(f"The camera's position could not be worked out from the board corners: {exc}") from exc
        if not ok:
            raise LocateError("The camera's position could not be worked out from the board corners.")
        rmat = cv2.Rodrigues(rvec)[0]
        centre = (-rmat.T @ tvec).ravel()
        if centre[2] < 0:   # board points given in mirror order: the camera came out under the board
            raise LocateError("The board corners give a camera below the board. Check the corner order and rotation.")
        projected = cv2.projectPoints(board, rvec, tvec, matrix, None)[0].reshape(-1, 2)
        return {"height_mm": float(centre[2]), "nadir_mm": [float(centre[0]), float(centre[1])],
                "focal_px": float(focal), "reproj_px": float(np.max(np.linalg.norm(projected - image_points, axis=1))),
                "rvec": rvec, "tvec": tvec, "matrix": matrix}

    def _register(self, reference: np.ndarray, observed: np.ndarray, valid: np.ndarray,
                  exclude: np.ndarray) -> tuple[np.ndarray, float, float]:
        """Warp the reference onto the observed canvas to cancel camera wobble (small affine, grid edges).

        As board_vision._register_grid: edges of the grey images at half
        resolution, phase correlation then ECC (affine) restricted to the grid
        lines and away from the changed area. Returns (aligned reference,
        correlation, largest corner displacement in canvas px). A failed or
        implausible registration leaves the reference unchanged.
        """
        scale = 0.5
        size = reference.shape[1], reference.shape[0]
        small = (round(size[0] * scale), round(size[1] * scale))

        def edges(image):
            gray = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), small, interpolation=cv2.INTER_AREA)
            gray = cv2.GaussianBlur(gray.astype(np.float32), (5, 5), 0)
            edge = cv2.magnitude(cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1))
            return np.minimum(edge, max(float(np.percentile(edge, 98)), 1.0))

        template, current = edges(reference), edges(observed)
        mask = np.zeros(small[::-1], np.uint8)
        step = PIECE_PX / 8.0 * scale
        for line in range(9):
            p = round(PIECE_PAD * scale + line * step)
            mask[max(p - 8, 0):p + 9, :] = 255
            mask[:, max(p - 8, 0):p + 9] = 255
        keep = cv2.resize(valid, small, interpolation=cv2.INTER_NEAREST)
        keep = cv2.erode(keep, np.ones((9, 9), np.uint8))
        bad = cv2.resize(exclude, small, interpolation=cv2.INTER_NEAREST)
        mask[(keep == 0) | (bad > 0)] = 0
        warp = np.eye(2, 3, dtype=np.float32)
        try:
            shift, response = cv2.phaseCorrelate(template, current, cv2.createHanningWindow(small, cv2.CV_32F))
            if response > 0.1 and max(abs(value) for value in shift) <= 10:
                warp[:, 2] = shift
            correlation, warp = cv2.findTransformECC(
                template, current, warp, cv2.MOTION_AFFINE,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5), mask, 3)
        except cv2.error:
            return reference, 0.0, 0.0
        warp[:, 2] /= scale
        box = np.float32([[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]])
        moved = float(np.max(np.linalg.norm(cv2.transform(box[None], warp)[0] - box, axis=1)))
        if not np.isfinite(warp).all() or correlation < 0.5 or moved > 60 or (moved > 20 and correlation < 0.8):
            return reference, float(correlation), moved
        # reference(x) ~ observed(W x): warpAffine with the forward map W puts the reference in observed's frame.
        aligned = cv2.warpAffine(reference, warp, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return aligned, float(correlation), moved

    @staticmethod
    def _photometric(reference: np.ndarray, observed: np.ndarray, valid: np.ndarray,
                     exclude: np.ndarray | None = None) -> np.ndarray:
        """LAB reference adjusted to the observed photo's exposure and lighting.

        Per channel a robust gain/offset (most of the board is unchanged). With
        `exclude` (the area that changed, from a first rough pass) also a smooth
        residual for uneven lighting changes: the remaining difference averaged
        over ~15 mm cells, the changed area left out, spread by normalised
        Gaussian smoothing (sigma ~20 mm) so the piece itself never leaks into it.
        """
        use = valid > 0 if exclude is None else (valid > 0) & (exclude == 0)
        ys, xs = np.nonzero(use[::4, ::4])
        ref_s = reference[::4, ::4][ys, xs]
        obs_s = observed[::4, ::4][ys, xs]
        out = reference.copy()
        for c in range(3):
            x, y = ref_s[:, c].astype(np.float64), obs_s[:, c].astype(np.float64)
            gain, offset = 1.0, float(np.median(y - x))
            if float(np.std(x)) >= 3:
                keep = np.ones(len(x), dtype=bool)
                for _ in range(4):
                    gain, offset = np.linalg.lstsq(np.column_stack((x[keep], np.ones(keep.sum()))), y[keep],
                                                   rcond=None)[0]
                    error = y - (gain * x + offset)
                    centre = np.median(error[keep])
                    keep = np.abs(error - centre) <= max(4.0, 3.5 * 1.4826 * float(np.median(np.abs(error - centre))))
                if not 0.6 <= gain <= 1.6:
                    gain = float(np.clip(gain, 0.6, 1.6))
                    offset = float(np.median(y - gain * x))
            out[:, :, c] = gain * reference[:, :, c] + offset
        if exclude is None:
            return out
        cell = 20
        h, w = out.shape[:2]
        weight = use.astype(np.float32)
        residual = (observed - out) * weight[:, :, None]
        small_w = cv2.resize(weight, (w // cell, h // cell), interpolation=cv2.INTER_AREA)
        small_r = cv2.resize(residual, (w // cell, h // cell), interpolation=cv2.INTER_AREA)
        spread_w = cv2.GaussianBlur(small_w, (0, 0), 4.0)
        spread_r = cv2.GaussianBlur(small_r, (0, 0), 4.0)
        smooth_small = spread_r / np.maximum(spread_w, 1e-3)[:, :, None]
        smooth_small[spread_w < 0.05] = 0.0
        smooth = cv2.resize(smooth_small, (w, h), interpolation=cv2.INTER_LINEAR)
        return out + smooth

    def _locate_piece(self, image, corners, transform, rotation, corners_from, calibration, stamp) -> dict:
        size = PIECE_PX + 2 * PIECE_PAD
        to_canvas = self._canvas_transform(corners, rotation)
        observed = cv2.warpPerspective(image, to_canvas, (size, size), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_REPLICATE)
        in_photo = cv2.warpPerspective(np.full(image.shape[:2], 255, np.uint8), to_canvas, (size, size),
                                       flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        reference, ref_valid, ref_from = self._reference_canvas(image.shape, rotation, calibration)
        valid = cv2.erode(cv2.bitwise_and(in_photo, ref_valid), np.ones((9, 9), np.uint8))
        valid[:6, :] = valid[-6:, :] = 0
        valid[:, :6] = valid[:, -6:] = 0
        if cv2.countNonZero(valid) < 0.5 * PIECE_PX * PIECE_PX:
            raise LocateError("Too little of the board is in view. Check the camera and the board corners.")

        def difference(ref_bgr, exclude=None):
            ref_lab = cv2.cvtColor(cv2.GaussianBlur(ref_bgr, (0, 0), 1.2), cv2.COLOR_BGR2LAB).astype(np.float32)
            adjusted = self._photometric(ref_lab, obs_lab, valid, exclude)
            delta = obs_lab - adjusted
            diff = np.sqrt((0.6 * delta[:, :, 0]) ** 2 + delta[:, :, 1] ** 2 + delta[:, :, 2] ** 2)
            diff = cv2.GaussianBlur(diff, (0, 0), 1.5)
            diff[valid == 0] = 0
            return diff

        obs_lab = cv2.cvtColor(cv2.GaussianBlur(observed, (0, 0), 1.2), cv2.COLOR_BGR2LAB).astype(np.float32)
        rough = difference(reference)
        rough_mask = (rough > max(12.0, self._noise_threshold(rough, valid)[1])).astype(np.uint8) * 255
        # thin lines (a misregistered grid) are what registration must see: keep only solid areas out
        rough_mask = cv2.morphologyEx(rough_mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21)))
        rough_mask = cv2.dilate(rough_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))
        reference, grid_correlation, moved = self._register(reference, observed, valid, rough_mask)
        diff = difference(reference, rough_mask)

        high, low, noise = self._noise_threshold(diff, valid)
        # Edges at half the local contrast: a blurred edge is cut at its middle whether the piece is
        # strongly or weakly different from the board there, so bright sides do not bloat the blob.
        local = cv2.dilate(diff, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
        mask = ((diff > low) & (diff > 0.5 * local)).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        mm2_per_px = (self.sx * 8 / PIECE_PX) * (self.sy * 8 / PIECE_PX)
        min_px = PIECE_MIN_MM2 / mm2_per_px
        max_px = PIECE_MAX_SQUARES * self.sx * self.sy / mm2_per_px
        blobs = []
        for i in range(1, count):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < 0.25 * min_px:
                continue
            peak = float(diff[labels == i].max())
            if peak < high:          # hysteresis: a real object has a strong core somewhere
                continue
            blobs.append((area, i, peak))
        blobs.sort(reverse=True)
        if not blobs:
            raise LocateError("No chess piece was found on the board. Put one piece on the board, keep hands "
                              "out of view, and check the empty-board photo was taken with the board clear.")
        area, label, peak = blobs[0]
        if len(blobs) > 1 and blobs[1][0] >= SECOND_BLOB_RATIO * area:
            raise LocateError("More than one thing on the board: clear it, leaving only the one piece "
                              "(and keep hands out of view).")
        if area < min_px:
            raise LocateError(f"The changed area on the board is too small for a chess piece "
                              f"({area * mm2_per_px:.0f} mm2). Use a bigger piece or check the lighting.")
        if area > max_px:
            raise LocateError(f"The changed area on the board is too large for one chess piece "
                              f"({area * mm2_per_px:.0f} mm2): clear the board, keep hands out of view, "
                              f"and retake the empty-board photo if the lighting changed.")

        blob = (labels == label).astype(np.uint8)
        filled = blob.copy()
        contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(filled, contours, -1, 1, thickness=cv2.FILLED)    # fill holes (shiny tops etc.)
        ys, xs = np.nonzero(filled)
        area = int(len(xs))
        clipped = bool(np.any(cv2.dilate(filled, np.ones((5, 5), np.uint8))[valid == 0]))
        mx, my = self._canvas_to_mm(xs + 0.0, ys + 0.0)
        q = np.array([float(np.mean(mx)), float(np.mean(my))])

        pose = self._camera_pose(corners, rotation, image.shape)
        nadir, height = np.array(pose["nadir_mm"]), pose["height_mm"]
        tall = float(np.clip(self.piece_height_mm, 0.0, 0.9 * height))
        radius = math.sqrt(area * mm2_per_px / math.pi)
        if self.centroid_height_frac is None:
            foot, radius = self._cylinder_foot(q, area * mm2_per_px, nadir, height, tall)
        else:
            z = float(np.clip(self.centroid_height_frac * self.piece_height_mm, 0.0, 0.9 * height))
            foot = nadir + (q - nadir) * (height - z) / height
            radius = self._cylinder_radius(foot, area * mm2_per_px, nadir, height, tall)
        parallax = q - foot

        base_edge, edge_check = self._base_edge(np.column_stack((mx, my)), q, nadir, radius)
        if base_edge is not None:
            edge_check = float(np.linalg.norm(base_edge - foot))

        x_mm, y_mm = float(foot[0]), float(foot[1])
        u, v = self._mm_to_oriented(x_mm, y_mm)
        warped = np.array(rotate_point(u, v, _inverse_rotation(rotation)))
        raw_px = cv2.perspectiveTransform(warped.reshape(1, 1, 2).astype(np.float64),
                                          np.linalg.inv(transform).astype(np.float64)).reshape(2)

        solidity = area / max(float(cv2.contourArea(cv2.convexHull(np.column_stack((xs, ys)).astype(np.int32)))), 1.0)
        confidence = float(np.clip((peak - noise) / max(3.0 * (high - noise), 1e-6), 0.0, 1.0))
        confidence *= float(np.clip(solidity / 0.85, 0.0, 1.0))
        if len(blobs) > 1:
            confidence *= 1.0 - blobs[1][0] / area
        if edge_check is not None:
            confidence *= math.exp(-max(0.0, edge_check - 3.0) / 6.0)
        if clipped:
            confidence *= 0.7
        if corners_from == "saved":
            confidence *= 0.9
        if ref_from not in ("captured", "given"):
            confidence *= 0.9   # an empty-board photo from setup: the lighting may have drifted since

        result = self._base_result("piece", x_mm, y_mm, u, v, confidence, corners_from, rotation, raw_px)
        result.update({
            "blob_area_px": area,
            "blob_area_mm2": round(area * mm2_per_px, 1),
            "parallax_mm": round(float(np.linalg.norm(parallax)), 2),
            "parallax_vec_mm": [round(float(parallax[0]), 2), round(float(parallax[1]), 2)],
            "raw_board_mm": [round(float(q[0]), 2), round(float(q[1]), 2)],
            "base_edge_mm": None if base_edge is None else [round(float(base_edge[0]), 2), round(float(base_edge[1]), 2)],
            "edge_check_mm": None if edge_check is None else round(edge_check, 2),
            "piece_height_mm": self.piece_height_mm,
            "clipped": clipped,
            "reference": ref_from,
            "grid_correlation": round(grid_correlation, 3),
            "registration_px": round(moved, 2),
            "camera": {"height_mm": round(height, 1), "nadir_mm": [round(v_, 1) for v_ in pose["nadir_mm"]],
                       "hfov_deg": pose["hfov_deg"], "focal_px": round(pose["focal_px"], 1),
                       "reproj_px": round(pose["reproj_px"], 2)},
        })

        def draw(board, at):
            k = BOARD_PX / PIECE_PX
            for contour in contours:
                pts = np.array([at((cx - PIECE_PAD) * k, (cy - PIECE_PAD) * k) for cx, cy in contour.reshape(-1, 2)],
                               dtype=np.int32)
                cv2.polylines(board, [pts], True, (0, 200, 255), 2, cv2.LINE_AA)
            ru, rv = self._mm_to_oriented(*q)
            cv2.circle(board, at(ru, rv), 4, (0, 255, 255), -1, cv2.LINE_AA)            # raw centroid
            if base_edge is not None:
                eu, ev = self._mm_to_oriented(*base_edge)
                cv2.drawMarker(board, at(eu, ev), (255, 120, 0), cv2.MARKER_DIAMOND, 12, 2, cv2.LINE_AA)
            nu, nv = self._mm_to_oriented(*nadir)
            if -ANNOTATE_PAD < nu < BOARD_PX + ANNOTATE_PAD and -ANNOTATE_PAD < nv < BOARD_PX + ANNOTATE_PAD:
                cv2.drawMarker(board, at(nu, nv), (255, 0, 255), cv2.MARKER_STAR, 18, 1, cv2.LINE_AA)

        dx, dy = result["offset_mm"]
        result["photo"] = self._annotate(image, transform, rotation, result, stamp, draw,
                                         [f"piece {result['square']}: {dx:+.1f} mm toward h, {dy:+.1f} mm toward 8   "
                                          f"parallax {result['parallax_mm']:.1f} mm"])
        self.last_photo = result["photo"]
        return result

    @staticmethod
    def _noise_threshold(diff: np.ndarray, valid: np.ndarray) -> tuple[float, float, float]:
        """(high, low, typical) difference levels from the unchanged board's own noise."""
        values = diff[::3, ::3][valid[::3, ::3] > 0]
        median = float(np.median(values))
        spread = 1.4826 * float(np.median(np.abs(values - median)))
        high = max(14.0, median + 10.0 * spread)
        low = max(7.0, median + 5.0 * spread)
        return high, low, median

    @staticmethod
    def _silhouette(foot, radius, nadir, height, tall) -> tuple[np.ndarray, float]:
        """(centroid, area) on the board plane of an upright cylinder's shadow cast from the camera centre.

        A disc at height z projects from the camera (nadir n, height H) onto
        the board as a disc centred n + (p - n) H / (H - z) with radius
        r H / (H - z); a solid cylinder projects to the convex hull of its
        bottom and top discs.
        """
        scale = height / (height - tall)
        angles = np.linspace(0.0, 2.0 * math.pi, 72, endpoint=False)
        ring = np.column_stack((np.cos(angles), np.sin(angles)))
        top = nadir + (foot - nadir) * scale
        points = np.vstack((foot + radius * ring, top + radius * scale * ring)).astype(np.float32)
        hull = cv2.convexHull(points)
        moments = cv2.moments(hull)
        if moments["m00"] <= 0:
            return np.asarray(foot, dtype=np.float64), 0.0
        return np.array([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]]), float(moments["m00"])

    def _cylinder_radius(self, foot, area_mm2, nadir, height, tall) -> float:
        radius = math.sqrt(area_mm2 / math.pi)
        for _ in range(12):
            _, model_area = self._silhouette(foot, radius, nadir, height, tall)
            if model_area <= 0:
                break
            radius *= math.sqrt(area_mm2 / model_area)
        return radius

    def _cylinder_foot(self, centroid, area_mm2, nadir, height, tall) -> tuple[np.ndarray, float]:
        """Foot centre and radius of the cylinder whose silhouette has the blob's centroid and area."""
        foot = np.asarray(centroid, dtype=np.float64).copy()
        radius = math.sqrt(area_mm2 / math.pi)
        for _ in range(20):
            model_centroid, model_area = self._silhouette(foot, radius, nadir, height, tall)
            if model_area <= 0:
                break
            radius *= math.sqrt(area_mm2 / model_area)
            step = centroid - model_centroid
            foot = foot + step
            if float(np.linalg.norm(step)) < 1e-3:
                break
        return foot, radius

    @staticmethod
    def _base_edge(points: np.ndarray, centroid: np.ndarray, nadir: np.ndarray, radius: float):
        """The foot centre from the blob's nadir-side edge, which is the base's rim at z = 0.

        The edge point nearest the nadir along the nadir->centroid direction is
        on the base (no parallax there); the foot is one base radius (the
        model's, from the blob area) further along, centred across the blob's
        width within that radius of the edge.
        """
        direction = centroid - nadir
        distance = float(np.linalg.norm(direction))
        if distance < 15.0:     # piece almost under the camera: no clear nadir side
            return None, None
        u = direction / distance
        across = np.array([-u[1], u[0]])
        rel = points - nadir
        along, side = rel @ u, rel @ across
        near = float(np.percentile(along, 0.5))
        band = along <= near + radius
        if band.sum() < 20:
            return None, None
        middle = float(np.median(side[band]))
        edge = nadir + u * (near + radius) + across * middle
        return edge, 0.0

    # ----- shared ------------------------------------------------------------------------
    def _base_result(self, target, x_mm, y_mm, u, v, confidence, corners_from, rotation, raw) -> dict:
        file = int(np.clip(round(x_mm / self.sx), 0, 7))
        rank = int(np.clip(round(y_mm / self.sy), 0, 7))
        offset = (x_mm - file * self.sx, y_mm - rank * self.sy)
        on_board = (-0.5 * self.sx <= x_mm <= 7.5 * self.sx) and (-0.5 * self.sy <= y_mm <= 7.5 * self.sy)
        return {
            "board_mm": [round(float(x_mm), 2), round(float(y_mm), 2)],
            "square": f"{'abcdefgh'[file]}{rank + 1}",
            "confidence": round(float(confidence), 3),
            "photo": None,
            "px": [round(float(u), 2), round(float(v), 2)],
            "target": target,
            "offset_mm": [round(float(offset[0]), 2), round(float(offset[1]), 2)],
            "on_board": bool(on_board),
            "corners_from": corners_from,
            "rotation": rotation,
            "raw_px": [round(float(raw[0]), 2), round(float(raw[1]), 2)],
        }

    def _detect(self, image: np.ndarray) -> dict[int, list[np.ndarray]]:
        """All DICT_4X4_50 markers in the photo, sub-pixel refined: {id: [4x2 corners, ...]}."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = self._detector.detectMarkers(gray)
        found: dict[int, list[np.ndarray]] = {}
        for c, i in zip(corners, [] if ids is None else ids.flatten()):
            found.setdefault(int(i), []).append(c.reshape(4, 2).astype(np.float64))
        return found

    def _board_corners(self, image: np.ndarray, detected, saved) -> tuple[np.ndarray, str]:
        """The app's corner source: ArUco corner markers 0-3, else calibration.json corners.

        With the markers in view this applies board_vision.board_corners' own
        rule (marker inner corner pushed out by the sticker margin) to the
        sub-pixel refined marker corners; refinement cuts the corner error
        from ~1.5 px to ~0.2 px. board_vision.board_corners itself is the
        next fallback, then the saved corners.
        """
        bv = self._bv
        if all(len(detected.get(i, [])) == 1 for i in bv.MARKER_IDS):
            points = []
            for marker_id, corner, (first, second) in zip(bv.MARKER_IDS, bv.INNER_MARKER_CORNERS,
                                                          bv.INNER_MARKER_NEIGHBORS):
                marker = detected[marker_id][0]
                points.append(marker[corner] + bv.MARKER_MARGIN_RATIO * (
                    2 * marker[corner] - marker[first] - marker[second]))
            points = np.float32(points)
            if cv2.isContourConvex(points) and cv2.contourArea(points, oriented=True) >= 10000:
                return points, "markers"
        try:
            return np.float32(bv.board_corners(image)), "markers"
        except Exception:
            pass
        if saved is None:
            raise LocateError("The board corners were not found: corner markers 0-3 are not all visible "
                              "and the camera calibration has no saved corners. Run the chess app's setup.")
        points = np.asarray(saved, dtype=np.float32)
        height, width = image.shape[:2]
        if (points.shape != (4, 2) or not np.isfinite(points).all() or
                np.any(points[:, 0] < -1) or np.any(points[:, 0] > width) or
                np.any(points[:, 1] < -1) or np.any(points[:, 1] > height)):
            raise LocateError("The saved board corners do not fit this photo. Recalibrate the chess camera.")
        if not cv2.isContourConvex(points) or cv2.contourArea(points, oriented=True) < 10000:
            raise LocateError("The saved board corners are not a usable outline. Recalibrate the chess camera.")
        return points, "saved"

    def _to_mm(self, u, v):
        squares = BOARD_PX / 8.0
        return (u / squares - 0.5) * self.sx, (7.5 - v / squares) * self.sy

    def _annotate(self, image, transform, rotation, result, stamp, draw, lines) -> str:
        pad = ANNOTATE_PAD
        size = int(BOARD_PX) + 1 + 2 * pad
        shift = np.array([[1, 0, pad], [0, 1, pad], [0, 0, 1]], dtype=np.float64)
        board = _rotate_image(cv2.warpPerspective(image, shift @ transform, (size, size),
                                                  borderMode=cv2.BORDER_CONSTANT, borderValue=(40, 40, 40)), rotation)
        squares = BOARD_PX / 8.0

        def at(u, v):
            return int(round(u + pad)), int(round(v + pad))

        for k in range(9):
            p = k * squares
            cv2.line(board, at(p, 0), at(p, BOARD_PX), (200, 200, 200), 1, cv2.LINE_AA)
            cv2.line(board, at(0, p), at(BOARD_PX, p), (200, 200, 200), 1, cv2.LINE_AA)
        for row in range(8):
            for col in range(8):
                for colour, width in (((30, 30, 30), 3), ((235, 235, 235), 1)):
                    cv2.putText(board, f"{'abcdefgh'[col]}{8 - row}", at(col * squares + 4, row * squares + 16),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, width, cv2.LINE_AA)

        draw(board, at)
        file, rank = "abcdefgh".index(result["square"][0]), int(result["square"][1]) - 1
        cu, cv_ = (file + 0.5) * squares, (7 - rank + 0.5) * squares
        cv2.drawMarker(board, at(cu, cv_), (0, 200, 0), cv2.MARKER_CROSS, 40, 2, cv2.LINE_AA)
        tu, tv = result["px"]
        cv2.line(board, at(cu, cv_), at(tu, tv), (0, 200, 0), 1, cv2.LINE_AA)
        cv2.drawMarker(board, at(tu, tv), (0, 0, 255), cv2.MARKER_TILTED_CROSS, 30, 2, cv2.LINE_AA)
        cv2.circle(board, at(tu, tv), 6, (0, 0, 255), 2, cv2.LINE_AA)

        x, y = result["board_mm"]
        lines = list(lines) + [f"board ({x:.1f}, {y:.1f}) mm from a1   conf {result['confidence']:.2f}   "
                               f"corners: {result['corners_from']}"]
        for i, text in enumerate(lines):
            org = (8, size - 30 + i * 22 - (22 if len(lines) > 1 else 0))
            cv2.putText(board, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(board, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)

        self.photo_dir.mkdir(parents=True, exist_ok=True)
        stamp = stamp or _stamp()
        path = self.photo_dir / f"locate_{stamp}.jpg"
        if not cv2.imwrite(str(path), board, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise LocateError(f"Could not save the annotated photo in {self.photo_dir}.")
        return str(path)

    def _prune(self) -> None:
        for prefix in ("raw_", "locate_", "reference_"):
            photos = sorted(self.photo_dir.glob(f"{prefix}*.jpg"))
            keep = 3 if prefix == "reference_" else KEEP_PHOTOS
            for old in photos[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass


def _inverse_rotation(rotation: str) -> str:
    return {"cw": "ccw", "ccw": "cw"}.get(rotation, rotation)
