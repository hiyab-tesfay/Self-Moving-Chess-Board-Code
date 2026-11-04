"""Still-image capture for Raspberry Pi CSI cameras and USB webcams."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import cv2


class CameraError(RuntimeError):
    """The requested camera could not produce a readable image."""


class Camera:
    def __init__(self, kind: str = "pi", device: int = 0) -> None:
        if kind not in ("pi", "usb"):
            raise ValueError("Camera must be 'pi' or 'usb'.")
        self.kind = kind
        self.device = device

    def capture(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "pi":
            command = shutil.which("rpicam-still") or shutil.which("libcamera-still")
            if command is None:
                raise CameraError("rpicam-still was not found. Install Raspberry Pi camera apps or use --camera usb.")
            try:
                result = subprocess.run(
                    [command, "--nopreview", "--timeout", "1500", "--camera", str(self.device),
                     "--output", str(path)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CameraError(f"Camera capture failed: {exc}") from exc
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise CameraError(f"Camera capture failed: {detail or 'unknown rpicam error'}")
        else:
            camera = cv2.VideoCapture(self.device)
            try:
                if not camera.isOpened():
                    raise CameraError(f"Could not open USB camera device {self.device}.")
                frame = None
                for _ in range(8):
                    ok, image = camera.read()
                    if ok:
                        frame = image
                if frame is None or not cv2.imwrite(str(path), frame):
                    raise CameraError("USB camera did not produce a usable frame.")
            finally:
                camera.release()
        if cv2.imread(str(path), cv2.IMREAD_COLOR) is None:
            raise CameraError(f"Camera did not save a readable image at {path}.")
        return path
