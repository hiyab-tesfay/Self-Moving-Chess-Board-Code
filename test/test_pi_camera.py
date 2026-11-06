import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from pi_camera import Camera, CameraError, CameraSettings, select_stable_frame
from pi_touch import (PhotoAdjustments, TouchApp, read_camera_settings,
                      read_photo_adjustments, write_camera_settings,
                      write_photo_adjustments)


class CameraSettingsTest(unittest.TestCase):
    def test_pi_capture_uses_selected_controls(self):
        settings = CameraSettings(ev=0.5, awb="indoor", contrast=1.25)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.jpg"
            def save_new_photo(command, **_kwargs):
                output = command[command.index("--output") + 1]
                cv2.imwrite(output, np.full((8, 8, 3), 100, dtype=np.uint8))
                return SimpleNamespace(returncode=0)

            with patch("pi_camera.shutil.which", return_value="/usr/bin/rpicam-still"), \
                    patch("pi_camera.subprocess.run", side_effect=save_new_photo) as run:
                Camera(settings=settings).capture(path)
            self.assertTrue(path.is_file())
            self.assertEqual(list(Path(directory).iterdir()), [path])
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--ev") + 1], "0.5")
            self.assertEqual(command[command.index("--awb") + 1], "indoor")
            self.assertEqual(command[command.index("--contrast") + 1], "1.25")

    def test_success_without_new_photo_does_not_reuse_old_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "starting.jpg"
            cv2.imwrite(str(path), np.full((8, 8, 3), 100, dtype=np.uint8))
            previous = path.read_bytes()
            with patch("pi_camera.shutil.which", return_value="/usr/bin/rpicam-still"), \
                    patch("pi_camera.subprocess.run", return_value=SimpleNamespace(returncode=0)):
                with self.assertRaisesRegex(CameraError, "new readable photo"):
                    Camera().capture(path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_usb_capture_waits_for_settled_view_and_releases_camera(self):
        frame = detailed_frame()
        source = Mock()
        source.isOpened.return_value = True
        source.read.return_value = (True, frame)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "move.png"
            with patch("pi_camera.cv2.VideoCapture", return_value=source):
                self.assertEqual(Camera(kind="usb").capture(path), path)
            self.assertTrue(np.array_equal(cv2.imread(str(path)), frame))
        source.release.assert_called_once()

    def test_usb_releases_camera_when_motion_does_not_settle(self):
        moving = moving_frames()
        source = Mock()
        source.isOpened.return_value = True
        source.read.side_effect = [(True, moving[index % 2]) for index in range(20)]
        with tempfile.TemporaryDirectory() as directory:
            with patch("pi_camera.cv2.VideoCapture", return_value=source):
                with self.assertRaisesRegex(CameraError, "still changing"):
                    Camera(kind="usb").capture(Path(directory) / "move.jpg")
        source.release.assert_called_once()

    def test_settings_round_trip_and_invalid_file(self):
        settings = CameraSettings(ev=-0.5, awb="fluorescent", contrast=1.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera_settings.json"
            write_camera_settings(path, settings)
            self.assertEqual(read_camera_settings(path), settings)
            path.write_text('{"ev": 99}')
            self.assertIsNone(read_camera_settings(path))

    def test_saved_calibration_requires_same_camera_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "calibration.json").write_text('{"scoring": "robust_occupancy_v2"}')
            (folder / "empty_warp.png").write_bytes(b"reference")
            app = TouchApp.__new__(TouchApp)
            app.args = SimpleNamespace(calibration=folder)
            app.camera_settings = CameraSettings()
            self.assertTrue(app.saved_calibration_available())
            app.camera_settings = CameraSettings(ev=0.5)
            self.assertFalse(app.saved_calibration_available())
            write_camera_settings(folder / "calibration_camera.json", app.camera_settings)
            self.assertTrue(app.saved_calibration_available())

    def test_photo_adjustments_are_saved_for_future_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo_adjustments.json"
            settings = PhotoAdjustments(brightness=20, contrast=120, warmth=-10)
            write_photo_adjustments(path, settings)
            self.assertEqual(read_photo_adjustments(path), settings)


def detailed_frame():
    yy, xx = np.indices((160, 160))
    values = np.where((xx // 20 + yy // 20) % 2, 150, 70).astype(np.uint8)
    return cv2.cvtColor(values, cv2.COLOR_GRAY2BGR)


def moving_frames():
    first, second = detailed_frame(), detailed_frame()
    cv2.circle(first, (45, 45), 14, (240, 240, 240), -1)
    cv2.circle(second, (110, 110), 14, (240, 240, 240), -1)
    return first, second


class StableFrameTest(unittest.TestCase):
    def test_sharpest_frame_selected_from_still_tail(self):
        sharp = detailed_frame()
        softer = cv2.GaussianBlur(sharp, (3, 3), .5)
        self.assertIs(select_stable_frame([softer, sharp, softer]), sharp)

    def test_waits_until_exposure_is_stable(self):
        bright = detailed_frame()
        dark = (bright // 2).astype(np.uint8)
        self.assertIs(select_stable_frame([dark, bright, bright, bright]), bright)

    def test_late_motion_is_rejected_instead_of_returning_earlier_view(self):
        still, moved = moving_frames()
        with self.assertRaisesRegex(CameraError, "still changing"):
            select_stable_frame([still, still, still, moved, still])

    def test_blank_frame_is_not_a_usable_chessboard_view(self):
        blank = np.full((160, 160, 3), 20, dtype=np.uint8)
        with self.assertRaisesRegex(CameraError, "too little detail"):
            select_stable_frame([blank, blank, blank])


if __name__ == "__main__":
    unittest.main()
