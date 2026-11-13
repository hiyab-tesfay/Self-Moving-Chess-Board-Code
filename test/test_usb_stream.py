"""Fresh-frame and resource checks with a stub webcam, without camera or motors."""
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from pi_camera import USBStream, USB_BURST_FRAMES, CameraError


class Webcam:
    def __init__(self):
        self.count = 0
        self.released = threading.Event()

    def isOpened(self):
        return True

    def set(self, *_args):
        return True

    def read(self):
        self.released.wait(.002)
        self.count += 1
        return True, np.full((2, 2, 3), self.count, dtype=np.int32)

    def release(self):
        self.released.set()


class USBStreamTests(unittest.TestCase):
    def test_reuses_camera_but_each_burst_is_entirely_fresh(self):
        webcam = Webcam()
        with patch("pi_camera.cv2.VideoCapture", return_value=webcam) as opened, \
                patch("pi_camera.select_stable_frame", side_effect=lambda frames: frames[0]):
            stream = USBStream(0)
            try:
                first = int(stream.capture()[0, 0, 0])
                second = int(stream.capture()[0, 0, 0])
                self.assertGreaterEqual(second - first, USB_BURST_FRAMES)
                opened.assert_called_once_with(0)
                self.assertLessEqual(len(stream.frames), USB_BURST_FRAMES)
            finally:
                stream.close()
            self.assertTrue(webcam.released.is_set())
            self.assertFalse(stream.thread.is_alive())

    def test_disconnection_fails_instead_of_reusing_old_frames(self):
        webcam = Mock()
        webcam.isOpened.return_value = True
        webcam.read.return_value = (False, None)
        with patch("pi_camera.cv2.VideoCapture", return_value=webcam):
            stream = USBStream(0)
            try:
                with self.assertRaises(CameraError):
                    stream.capture()
            finally:
                stream.close()
            webcam.release.assert_called_once()
