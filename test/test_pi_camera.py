import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from pi_camera import Camera, CameraSettings
from pi_touch import (PhotoAdjustments, TouchApp, read_camera_settings,
                      read_photo_adjustments, write_camera_settings,
                      write_photo_adjustments)


class CameraSettingsTest(unittest.TestCase):
    def test_pi_capture_uses_selected_controls(self):
        settings = CameraSettings(ev=0.5, awb="indoor", contrast=1.25)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.jpg"
            cv2.imwrite(str(path), np.full((8, 8, 3), 100, dtype=np.uint8))
            with patch("pi_camera.shutil.which", return_value="/usr/bin/rpicam-still"), \
                    patch("pi_camera.subprocess.run") as run:
                run.return_value.returncode = 0
                Camera(settings=settings).capture(path)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--ev") + 1], "0.5")
            self.assertEqual(command[command.index("--awb") + 1], "indoor")
            self.assertEqual(command[command.index("--contrast") + 1], "1.25")

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
            (folder / "calibration.json").write_text("{}")
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


if __name__ == "__main__":
    unittest.main()
