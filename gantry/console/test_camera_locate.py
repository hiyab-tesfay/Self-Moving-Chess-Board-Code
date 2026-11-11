"""Tests for camera_locate with synthetic camera photos (no camera, no network).

Photos are rendered from the board frame outward: the real empty_warp.png is
un-rotated and un-warped into a "camera" image through known corners, and an
ArUco id-10 token is drawn at an exact board position, so the truth is known
to well under a pixel. Chess pieces (target="piece") are shaded 3D cylinders
projected through a pinhole camera posed like the real one (solvePnP on the
real calibration corners), painted over the same empty board. The app dir is $CHESS_APP_DIR, else ~/chess-board-pi-only,
else the read-only snapshot in ~/duen-gantry/pi-app-snapshot.

Run: cd code/console && python -m unittest test_camera_locate
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import camera_locate as cl  # noqa: E402

SNAPSHOT = Path("/Users/kennakao/duen-gantry/pi-app-snapshot")


def _app_dir() -> Path:
    env = os.environ.get("CHESS_APP_DIR")
    if env:
        return Path(env).expanduser()
    default = Path("~/chess-board-pi-only").expanduser()
    return default if (default / "board_vision.py").is_file() else SNAPSHOT


APP = _app_dir()
CAL = json.loads((APP / "calibration" / "calibration.json").read_text())
EMPTY_WARP = cv2.imread(str(APP / "calibration" / "empty_warp.png"), cv2.IMREAD_COLOR)
PHOTO_SIZE = (2592, 1944)            # the Pi camera's still size (width, height)
N = cl.BOARD_PX
DST = np.float32([[0, 0], [N, 0], [N, N], [0, N]])
DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
UNROTATE = {"none": None, "cw": cv2.ROTATE_90_COUNTERCLOCKWISE, "ccw": cv2.ROTATE_90_CLOCKWISE, "180": cv2.ROTATE_180}


def unrotate_point(u, v, rotation):
    """Inverse of board_vision.rotate_board for a point (written independently of camera_locate)."""
    if rotation == "none":
        return u, v
    if rotation == "cw":      # rotated (u, v) came from (v, N - u)
        return v, N - u
    if rotation == "ccw":     # rotated (u, v) came from (N - v, u)
        return N - v, u
    return N - u, N - v


def board_to_oriented(x_mm, y_mm, sx, sy):
    """board_mm (origin a1 centre, +x to h, +y to 8) -> pixel in the app's rotated 800x800 board."""
    return (x_mm / sx + 0.5) * N / 8, (7.5 - y_mm / sy) * N / 8


def _paste(photo, source, quad, mask_only=False):
    """Draw source (BGR) into photo so its outer pixel edges land exactly on quad (TL, TR, BR, BL)."""
    h, w = source.shape[:2]
    src = np.float32([[-0.5, -0.5], [w - 0.5, -0.5], [w - 0.5, h - 0.5], [-0.5, h - 0.5]])
    transform = cv2.getPerspectiveTransform(src, np.float32(quad))
    size = (photo.shape[1], photo.shape[0])
    warped = cv2.warpPerspective(source.astype(np.float32), transform, size, flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    mask = cv2.warpPerspective(np.ones((h, w), np.float32), transform, size, flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=0)[:, :, None]
    photo[:] = photo * (1 - mask) + warped * mask


def _marker(marker_id, cells_px=16):
    image = cv2.aruco.generateImageMarker(DICT, marker_id, 6 * cells_px)
    return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR).astype(np.float32)


def render(tokens, corners, rotation, sx=53.0, sy=None, corner_markers=False, seed=1, pieces=(), gain=1.0):
    """A synthetic camera photo.

    tokens: list of (x_mm, y_mm[, angle_deg]) token centres in board_mm.
    corners: photo pixels of the board's warp corners TL, TR, BR, BL (board_vision order).
    pieces: draw_cylinder argument tuples (3D pieces seen through POSE).
    gain: brightness factor applied to the whole photo (lighting change).
    """
    sy = sy or sx
    to_photo = cv2.getPerspectiveTransform(DST, np.float32(corners))

    def photo_pts(warp_pts):
        return cv2.perspectiveTransform(np.float64(warp_pts).reshape(-1, 1, 2), to_photo.astype(np.float64)).reshape(-1, 2)

    background = EMPTY_WARP if UNROTATE[rotation] is None else cv2.rotate(EMPTY_WARP, UNROTATE[rotation])
    photo = cv2.warpPerspective(background, to_photo, PHOTO_SIZE, borderMode=cv2.BORDER_CONSTANT,
                                borderValue=(110, 170, 200)).astype(np.float32)

    if corner_markers:   # markers 0-3 placed the way board_vision.board_corners expects them
        s = 70.0
        m = 0.1 * s
        boxes = {0: (-m - s, -m - s), 1: (N + m, -m - s), 2: (N + m, N + m), 3: (-m - s, N + m)}
        for marker_id, (x0, y0) in boxes.items():
            pad = 1.2 * m
            white = np.full((8, 8, 3), 255, np.float32)
            _paste(photo, white, photo_pts([[x0 - pad, y0 - pad], [x0 + s + pad, y0 - pad],
                                            [x0 + s + pad, y0 + s + pad], [x0 - pad, y0 + s + pad]]))
            _paste(photo, _marker(marker_id), photo_pts([[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s]]))

    for token in tokens:
        x_mm, y_mm = token[0], token[1]
        angle = math.radians(token[2] if len(token) > 2 else 0.0)
        u, v = board_to_oriented(x_mm, y_mm, sx, sy)
        kx, ky = N / 8 / sx, N / 8 / sy            # oriented pixels per mm

        def quad(half_mm):
            pts = []
            for cx, cy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                dx, dy = cx * half_mm, cy * half_mm
                rx, ry = dx * math.cos(angle) - dy * math.sin(angle), dx * math.sin(angle) + dy * math.cos(angle)
                pts.append(unrotate_point(u + rx * kx, v + ry * ky, rotation))
            return photo_pts(pts)

        disc = [unrotate_point(u + 15 * kx * math.cos(t), v + 15 * ky * math.sin(t), rotation)
                for t in np.linspace(0, 2 * math.pi, 72, endpoint=False)]
        cv2.fillPoly(photo, [np.round(photo_pts(disc) * 16).astype(np.int32)], (40, 60, 150), cv2.LINE_AA, shift=4)
        _paste(photo, np.full((8, 8, 3), 245, np.float32), quad(13.5))     # white sticker
        _paste(photo, _marker(cl.TOKEN_ID) * 0.92 + 12, quad(10.0))        # 20 mm black marker

    for piece in pieces:
        draw_cylinder(photo, *piece)

    rng = np.random.default_rng(seed)
    photo = cv2.GaussianBlur(photo, (0, 0), 0.8) * gain + rng.normal(0, 3.0, photo.shape)
    photo = np.clip(photo, 0, 255).astype(np.uint8)
    ok, jpg = cv2.imencode(".jpg", photo, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return cv2.imdecode(jpg, cv2.IMREAD_COLOR)


# --- a 3D camera for chess pieces -----------------------------------------------------------
SQ = 53.0
HFOV = 66.0                          # the locator's default field of view


def oriented_to_mm(u, v, sx=SQ, sy=SQ):
    return (u / (N / 8) - 0.5) * sx, (7.5 - v / (N / 8)) * sy


def camera_from_corners(corners, rotation="cw", hfov=HFOV, sx=SQ, sy=SQ):
    """A pinhole camera (K, rvec, tvec) placed like the real one: solvePnP on the real board corners."""
    w, h = PHOTO_SIZE
    f = (w / 2) / math.tan(math.radians(hfov) / 2)
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], dtype=np.float64)
    warp = [(0, 0), (N, 0), (N, N), (0, N)]
    rot = {"none": lambda x, y: (x, y), "cw": lambda x, y: (N - y, x),
           "ccw": lambda x, y: (y, N - x), "180": lambda x, y: (N - x, N - y)}[rotation]
    board = np.array([[*oriented_to_mm(*rot(x, y), sx, sy), 0.0] for x, y in warp])
    ok, rvec, tvec = cv2.solvePnP(board, np.float64(corners), K, None, flags=cv2.SOLVEPNP_IPPE)
    assert ok
    # the photo corners this camera really sees, so the board-plane homography is the camera's
    seen = cv2.projectPoints(board, rvec, tvec, K, None)[0].reshape(-1, 2)
    centre = (-cv2.Rodrigues(rvec)[0].T @ tvec).ravel()
    return {"K": K, "rvec": rvec, "tvec": tvec, "corners": seen.tolist(), "centre": centre}


def _inset(points, factor):
    points = np.float64(points)
    centre = points.mean(axis=0)
    return (centre + factor * (points - centre)).tolist()


# The pinhole camera cannot reproduce the real (hand-tapped) corners exactly; posed on corners pulled
# in by 5 % its own view of the board stays inside the photo.
POSE = camera_from_corners(_inset(CAL["corners"], 0.95))
GOLD = (40, 165, 215)                # BGR, like the real gold pieces
PURPLE = (120, 40, 85)


def draw_cylinder(photo, x_mm, y_mm, radius=12.0, height=45.0, colour=GOLD, pose=None):
    """Paint a shaded solid vertical cylinder standing at board_mm (x, y) as the camera sees it."""
    pose = pose or POSE
    K, rvec, tvec, cam = pose["K"], pose["rvec"], pose["tvec"], pose["centre"]
    steps = 96
    angles = np.linspace(0, 2 * math.pi, steps, endpoint=False)
    ring = np.column_stack((x_mm + radius * np.cos(angles), y_mm + radius * np.sin(angles)))
    bottom = np.column_stack((ring, np.zeros(steps)))
    top = np.column_stack((ring, np.full(steps, height)))
    project = lambda pts: cv2.projectPoints(np.float64(pts), rvec, tvec, K, None)[0].reshape(-1, 2)
    pb, pt = project(bottom), project(top)
    light = np.array([-0.3, 0.4, 0.85])
    light /= np.linalg.norm(light)
    base = np.float64(colour)
    facets = []
    for i in range(steps):
        j = (i + 1) % steps
        mid = (bottom[i] + bottom[j] + top[i] + top[j]) / 4
        normal = np.array([math.cos(angles[i] + math.pi / steps), math.sin(angles[i] + math.pi / steps), 0.0])
        if normal @ (cam - mid) <= 0:
            continue                        # facing away from the camera
        shade = 0.35 + 0.65 * max(0.0, float(normal @ light))
        facets.append((float(np.linalg.norm(cam - mid)), [pb[i], pb[j], pt[j], pt[i]], base * shade))
    for _, quad, col in sorted(facets, key=lambda item: -item[0]):
        cv2.fillPoly(photo, [np.round(np.float64(quad) * 16).astype(np.int32)], col.tolist(), cv2.LINE_AA, shift=4)
    cv2.fillPoly(photo, [np.round(pt * 16).astype(np.int32)], (base * (0.35 + 0.65 * light[2]) * 1.1).tolist(),
                 cv2.LINE_AA, shift=4)


def shrunk_corners(factor=0.8):
    """The real calibration corners pulled toward their centre so corner markers fit in the frame."""
    points = np.float64(CAL["corners"])
    centre = points.mean(axis=0)
    return (centre + factor * (points - centre)).tolist()


def make_app(root: Path, rotation: str, corners, empty_warp: bool = False) -> Path:
    """A throwaway app dir: the real board_vision/pi_camera and a calibration.json of our choosing."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("board_vision.py", "pi_camera.py"):
        os.symlink(APP / name, root / name)
    (root / "calibration").mkdir()
    if empty_warp:
        os.symlink(APP / "calibration" / "empty_warp.png", root / "calibration" / "empty_warp.png")
    data = {"rotation": rotation}
    if corners is not None:
        data["corners"] = corners
    (root / "calibration" / "calibration.json").write_text(json.dumps(data))
    return root


class FakeCamera:
    def __init__(self, image):
        self.image, self.calls = image, 0

    def capture(self, path):
        self.calls += 1
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), self.image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return path


class TokenLocateTest(unittest.TestCase):
    TOL = 0.7

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="camera_locate_test_"))
        cls.photos = cls.tmp / "photos"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def locator(self, app=None, square=53.0, camera=FakeCamera(None)):
        return cl.CameraLocator(square, app_dir=app or APP, camera=camera, photo_dir=self.photos, target="token")

    def assertNear(self, result, x, y, square=None):
        got = result["board_mm"]
        err = math.hypot(got[0] - x, got[1] - y)
        self.assertLess(err, self.TOL, f"board_mm {got} vs truth ({x:.2f}, {y:.2f}): {err:.3f} mm")
        if square:
            self.assertEqual(result["square"], square)
        return err

    # --- accuracy on the real calibration (saved corners, rotation cw) ---------------------
    def test_real_calibration_points(self):
        self.assertEqual(CAL.get("rotation"), "cw")
        sq = 53.0
        cases = [((0, 0), "a1"), ((7 * sq, 7 * sq), "h8"), ((3 * sq, 3 * sq), "d4"),
                 ((7 * sq, 0), "h1"), ((0, 7 * sq), "a8"), ((4 * sq, 3 * sq), "e4"),
                 ((3.3 * sq, 2.7 * sq), "d4"), ((5 * sq + 17.0, 6 * sq - 21.0, 33), "f7"),
                 ((1 * sq - 9.5, 4 * sq + 12.25, -70), "b5")]
        locator = self.locator()
        errors = []
        for (point, square) in cases:
            with self.subTest(square=square, point=point):
                result = locator.locate_image(render([point], CAL["corners"], "cw"))
                errors.append(self.assertNear(result, point[0], point[1], square))
                self.assertEqual(result["corners_from"], "saved")
                self.assertGreater(result["confidence"], 0.6)
                self.assertTrue(Path(result["photo"]).is_file())
                self.assertEqual(locator.last_photo, result["photo"])
                file, rank = "abcdefgh".index(square[0]), int(square[1]) - 1
                self.assertAlmostEqual(result["offset_mm"][0], result["board_mm"][0] - file * sq, places=1)
                self.assertAlmostEqual(result["offset_mm"][1], result["board_mm"][1] - rank * sq, places=1)
        print(f"\n  saved corners, cw: max error {max(errors):.3f} mm, mean {np.mean(errors):.3f} mm")

    def test_px_is_the_app_warped_frame(self):
        """Cross-check against board_vision itself: warp+rotate the photo with the app's own
        functions and find the black marker there; it must sit at result['px'] and in the
        square board_vision's naming gives that pixel."""
        sys.path.insert(0, str(APP))
        import board_vision as bv
        for point, square in (((2 * 53.0, 5 * 53.0), "c6"), ((6 * 53.0 + 10, 1 * 53.0 - 12), "g2")):
            with self.subTest(square=square):
                photo = render([point], CAL["corners"], "cw")
                result = self.locator().locate_image(photo)
                board = bv.rotate_board(bv.warp_board(photo, CAL["corners"]), CAL["rotation"])
                empty = bv.rotate_board(bv.warp_board(render([], CAL["corners"], "cw"), CAL["corners"]), CAL["rotation"])
                dark = (cv2.cvtColor(board, cv2.COLOR_BGR2GRAY).astype(int) <
                        cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY).astype(int) - 60)
                ys, xs = np.nonzero(dark)
                u, v = xs.mean(), ys.mean()
                self.assertLess(math.hypot(u - result["px"][0], v - result["px"][1]), 3.0)
                col, row = int(u // bv.SQUARE), int(v // bv.SQUARE)
                self.assertEqual(f"{'abcdefgh'[col]}{8 - row}", square)    # board_vision's own naming
                self.assertEqual(result["square"], square)

    # --- corner markers ---------------------------------------------------------------------
    def test_corner_markers_used_first(self):
        corners = shrunk_corners()
        app = make_app(self.tmp / "app_markers", "cw", CAL["corners"])   # saved corners are now wrong
        locator = self.locator(app)
        for point, square in (((0, 0), "a1"), ((7 * 53.0, 7 * 53.0), "h8"), ((3 * 53.0 + 11, 3 * 53.0 - 8), "d4")):
            with self.subTest(square=square):
                result = locator.locate_image(render([point], corners, "cw", corner_markers=True))
                self.assertEqual(result["corners_from"], "markers")
                self.assertNear(result, point[0], point[1], square)

    # --- rotation handling ------------------------------------------------------------------
    def test_every_rotation(self):
        corners = CAL["corners"]
        for rotation in ("none", "cw", "ccw", "180"):
            app = make_app(self.tmp / f"app_rot_{rotation}", rotation, corners)
            locator = self.locator(app)
            for point, square in (((1 * 53.0 + 5, 6 * 53.0 - 7), "b7"), ((6 * 53.0, 0), "g1")):
                with self.subTest(rotation=rotation, square=square):
                    result = locator.locate_image(render([point], corners, rotation))
                    self.assertEqual(result["rotation"], rotation)
                    self.assertNear(result, point[0], point[1], square)
            # the same photo read with the wrong rotation lands somewhere else
            wrong = "none" if rotation != "none" else "cw"
            photo = render([(1 * 53.0 + 5, 6 * 53.0 - 7)], corners, wrong)
            self.assertNotEqual(locator.locate_image(photo)["square"], "b7")

    def test_rectangular_squares(self):
        square = (53.0, 51.5)
        point = (5 * 53.0 + 6, 2 * 51.5 - 4)
        result = self.locator(square=square).locate_image(render([point], CAL["corners"], "cw", sx=53.0, sy=51.5))
        self.assertNear(result, *point, square="f3")

    # --- failures ---------------------------------------------------------------------------
    def test_missing_token(self):
        with self.assertRaises(cl.LocateError) as caught:
            self.locator().locate_image(render([], CAL["corners"], "cw"))
        self.assertIn("not seen", str(caught.exception))

    def test_two_tokens(self):
        photo = render([(53.0, 53.0), (5 * 53.0, 5 * 53.0)], CAL["corners"], "cw")
        with self.assertRaises(cl.LocateError) as caught:
            self.locator().locate_image(photo)
        self.assertIn("2 times", str(caught.exception))

    def test_no_corners(self):
        app = make_app(self.tmp / "app_no_corners", "cw", None)
        with self.assertRaises(cl.LocateError) as caught:
            self.locator(app).locate_image(render([(53.0, 53.0)], CAL["corners"], "cw"))
        self.assertIn("corners", str(caught.exception))

    # --- camera path ------------------------------------------------------------------------
    def test_fake_camera_locate(self):
        point = (4 * 53.0 - 6, 4 * 53.0 + 3)
        camera = FakeCamera(render([point], CAL["corners"], "cw"))
        locator = cl.CameraLocator([53, 53], app_dir=APP, camera=camera, photo_dir=self.tmp / "cam", target="token")
        result = locator.locate()
        self.assertEqual(camera.calls, 1)
        self.assertNear(result, *point, square="e5")
        self.assertEqual(locator.last_photo, result["photo"])
        annotated = cv2.imread(result["photo"])
        self.assertEqual(annotated.shape[:2], (800 + 2 * cl.ANNOTATE_PAD,) * 2)
        self.assertTrue(list((self.tmp / "cam").glob("raw_*.jpg")))
        json.dumps(result)   # the console sends it as JSON

    def test_camera_failure_is_locate_error(self):
        class Broken:
            def capture(self, path):
                raise RuntimeError("rpicam-still was not found")
        with self.assertRaises(cl.LocateError):
            cl.CameraLocator(53, app_dir=APP, camera=Broken(), photo_dir=self.photos, target="token").locate()

    def test_find_app_dir(self):
        old = os.environ.pop("CHESS_APP_DIR", None)
        try:
            self.assertEqual(cl.find_app_dir(str(APP)), APP)
            self.assertIsNone(cl.find_app_dir(str(self.tmp / "nowhere")))
            os.environ["CHESS_APP_DIR"] = str(APP)
            self.assertEqual(cl.find_app_dir(), APP)
        finally:
            os.environ.pop("CHESS_APP_DIR", None)
            if old is not None:
                os.environ["CHESS_APP_DIR"] = old


class SequenceCamera:
    """A fake camera returning the given photos in turn (the last one repeats)."""

    def __init__(self, *images):
        self.images, self.calls = list(images), 0

    def capture(self, path):
        image = self.images[min(self.calls, len(self.images) - 1)]
        self.calls += 1
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return path


class PieceLocateTest(unittest.TestCase):
    """A chess piece (a shaded 3D cylinder, r 12 mm, h 45 mm) on the empty board, seen by a camera
    posed like the real one (solvePnP on the real calibration corners, 66 deg field of view)."""
    TOL = 1.0

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="camera_locate_piece_"))
        cls.photos = cls.tmp / "photos"
        cls.corners = POSE["corners"]
        cls.app = make_app(cls.tmp / "app", "cw", cls.corners, empty_warp=True)
        cls.empty = render([], cls.corners, "cw", seed=7)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def photo(self, *pieces, seed=1, gain=1.0):
        return render([], self.corners, "cw", seed=seed, gain=gain, pieces=[tuple(p) for p in pieces])

    def locator(self, reference="empty", camera=None, **kw):
        ref = self.empty if isinstance(reference, str) and reference == "empty" else reference
        return cl.CameraLocator(SQ, app_dir=self.app, camera=camera or FakeCamera(None), photo_dir=self.photos,
                                reference=ref, **kw)

    def check(self, result, x, y, square=None, tol=None):
        got = result["board_mm"]
        err = math.hypot(got[0] - x, got[1] - y)
        self.assertLess(err, tol or self.TOL, f"board_mm {got} vs truth ({x:.2f}, {y:.2f}): {err:.3f} mm")
        if square:
            self.assertEqual(result["square"], square)
        return err

    def test_pose_from_real_corners(self):
        locator = self.locator()
        pose = locator._camera_pose(np.float32(CAL["corners"]), "cw", (PHOTO_SIZE[1], PHOTO_SIZE[0]))
        centre = camera_from_corners(CAL["corners"])["centre"]
        self.assertAlmostEqual(pose["height_mm"], centre[2], delta=0.5)
        self.assertAlmostEqual(pose["nadir_mm"][0], centre[0], delta=0.5)
        self.assertAlmostEqual(pose["nadir_mm"][1], centre[1], delta=0.5)
        self.assertTrue(300 < pose["height_mm"] < 900)
        print(f"\n  real corners, hfov {pose['hfov_deg']}: camera {pose['height_mm']:.0f} mm above the board, "
              f"nadir ({pose['nadir_mm'][0]:.0f}, {pose['nadir_mm'][1]:.0f}) mm, reproj {pose['reproj_px']:.1f} px")
        # a measured height overrides the field-of-view guess
        tall = cl.CameraLocator(SQ, app_dir=self.app, camera=FakeCamera(None), photo_dir=self.photos,
                                camera_height_mm=600.0)
        self.assertAlmostEqual(tall._camera_pose(np.float32(CAL["corners"]), "cw",
                                                 (PHOTO_SIZE[1], PHOTO_SIZE[0]))["height_mm"], 600.0, delta=1.0)

    def test_piece_points(self):
        cases = [((0, 0), "a1"), ((7 * SQ, 7 * SQ), "h8"), ((7 * SQ, 0), "h1"), ((0, 7 * SQ), "a8"),
                 ((3 * SQ, 3 * SQ), "d4"), ((3.3 * SQ, 2.7 * SQ), "d4"), ((5 * SQ + 17.0, 6 * SQ - 21.0), "f7"),
                 ((1 * SQ - 9.5, 4 * SQ + 12.25), "b5"), ((6 * SQ + 8, 1 * SQ - 14), "g2")]
        locator = self.locator()
        errors, raw_errors = [], []
        for point, square in cases:
            with self.subTest(square=square, point=point):
                result = locator.locate_image(self.photo(point))
                errors.append(self.check(result, point[0], point[1], square))
                raw = result["raw_board_mm"]
                raw_errors.append(math.hypot(raw[0] - point[0], raw[1] - point[1]))
                self.assertEqual(result["target"], "piece")
                self.assertGreater(result["confidence"], 0.5)
                self.assertGreater(result["blob_area_px"], 1000)
                self.assertTrue(Path(result["photo"]).is_file())
                self.assertEqual(locator.last_photo, result["photo"])
                self.assertAlmostEqual(result["parallax_mm"], math.hypot(*result["parallax_vec_mm"]), delta=0.02)
                if result["base_edge_mm"] is not None:
                    edge = result["base_edge_mm"]
                    self.assertLess(math.hypot(edge[0] - point[0], edge[1] - point[1]), 3.0)
                json.dumps(result)
        print(f"\n  piece, corrected: max error {max(errors):.2f} mm, mean {np.mean(errors):.2f} mm; "
              f"uncorrected centroid: max {max(raw_errors):.1f} mm, mean {np.mean(raw_errors):.1f} mm")

    def test_parallax_is_corrected(self):
        """Far from the nadir the raw blob centroid is several mm out; the corrected point is not."""
        locator = self.locator()
        for point in ((0, 0), (7 * SQ, 0), (0, 7 * SQ)):
            with self.subTest(point=point):
                result = locator.locate_image(self.photo(point))
                raw = result["raw_board_mm"]
                self.assertGreater(math.hypot(raw[0] - point[0], raw[1] - point[1]), 4.0)
                self.assertGreater(result["parallax_mm"], 4.0)
                self.check(result, *point)
        # a wrong piece height leaves an error: the model is really what corrects it
        flat = self.locator(piece_height_mm=0.0).locate_image(self.photo((0, 0)))
        self.assertGreater(math.hypot(*flat["board_mm"]), 4.0)
        self.assertEqual(flat["parallax_mm"], 0.0)

    def test_purple_piece_and_other_sizes(self):
        locator = self.locator(piece_height_mm=35.0)
        for point in ((2 * SQ, 6 * SQ), (5 * SQ, 2 * SQ)):
            with self.subTest(point=point):
                result = locator.locate_image(self.photo((*point, 10.0, 35.0, PURPLE)))
                self.check(result, *point)

    def test_two_pieces(self):
        with self.assertRaises(cl.LocateError) as caught:
            self.locator().locate_image(self.photo((SQ, SQ), (5 * SQ, 5 * SQ)))
        self.assertIn("more than one thing", str(caught.exception).lower())

    def test_empty_board(self):
        with self.assertRaises(cl.LocateError) as caught:
            self.locator().locate_image(self.photo(seed=3))
        self.assertIn("no chess piece", str(caught.exception).lower())

    def test_brighter_lighting(self):
        locator = self.locator()
        for point in ((2 * SQ + 5, 3 * SQ - 6), (7 * SQ, 7 * SQ)):
            with self.subTest(point=point):
                self.check(locator.locate_image(self.photo(point, gain=1.15)), *point)

    def test_camera_wobble(self):
        """The reference taken with the camera 1.5 px off: registration cancels it."""
        moved = (np.float64(self.corners) + [1.5, -1.0]).tolist()
        reference = render([], moved, "cw", seed=9)
        point = (4 * SQ - 6, 3 * SQ + 9)
        result = self.locator(reference=reference).locate_image(self.photo(point))
        self.check(result, *point)

    def test_capture_reference(self):
        point = (4 * SQ - 6, 4 * SQ + 3)
        camera = SequenceCamera(self.empty, self.photo(point))
        locator = cl.CameraLocator(SQ, app_dir=self.app, camera=camera, photo_dir=self.tmp / "cam")
        path = locator.capture_reference()
        self.assertTrue(Path(path).is_file())
        self.assertTrue(Path(path).name.startswith("reference_"))
        result = locator.locate()
        self.assertEqual(camera.calls, 2)
        self.assertEqual(result["reference"], "captured")
        self.check(result, *point, square="e5")
        self.assertEqual(cv2.imread(result["photo"]).shape[:2], (800 + 2 * cl.ANNOTATE_PAD,) * 2)
        json.dumps(result)

    def test_default_reference_is_the_apps_empty_board(self):
        """No reference given: the app's empty.jpg if it fits, else its straightened empty_warp.png."""
        point = (2 * SQ + 4, 5 * SQ - 3)
        result = self.locator(reference=None).locate_image(self.photo(point))
        self.assertEqual(result["reference"], "empty_warp.png")
        self.check(result, *point, tol=1.5)

    def test_no_reference_at_all(self):
        app = make_app(self.tmp / "app_noref", "cw", self.corners)
        with self.assertRaises(cl.LocateError) as caught:
            cl.CameraLocator(SQ, app_dir=app, camera=FakeCamera(None),
                             photo_dir=self.photos).locate_image(self.photo((SQ, SQ)))
        self.assertIn("empty board", str(caught.exception))

    def test_no_corners(self):
        app = make_app(self.tmp / "app_piece_no_corners", "cw", None)
        with self.assertRaises(cl.LocateError) as caught:
            cl.CameraLocator(SQ, app_dir=app, camera=FakeCamera(None), photo_dir=self.photos,
                             reference=self.empty).locate_image(self.photo((SQ, SQ)))
        self.assertIn("corners", str(caught.exception))

    def test_bad_target(self):
        with self.assertRaises(ValueError):
            cl.CameraLocator(SQ, app_dir=self.app, camera=FakeCamera(None), target="disc")


if __name__ == "__main__":
    unittest.main()
