"""Still-image capture for Raspberry Pi CSI cameras and USB webcams."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2


class CameraError(RuntimeError):
    """The requested camera could not produce a readable image."""


@dataclass(frozen=True)
class CameraSettings:
    """Pi camera controls used for every reference and game photo."""

    ev: float = 0.0
    awb: str = "auto"
    contrast: float = 1.0

    def __post_init__(self) -> None:
        if self.ev not in (-1.0, -0.5, 0.0, 0.5, 1.0, 1.5):
            raise ValueError("Exposure must be between -1.0 and +1.5 in half-stop steps.")
        if self.awb not in ("auto", "indoor", "fluorescent", "daylight"):
            raise ValueError("Unsupported white balance mode.")
        if self.contrast not in (1.0, 1.25, 1.5):
            raise ValueError("Contrast must be 1.0, 1.25, or 1.5.")


class Camera:
    def __init__(self, kind: str = "pi", device: int = 0,
                 settings: CameraSettings | None = None) -> None:
        if kind not in ("pi", "usb"):
            raise ValueError("Camera must be 'pi' or 'usb'.")
        self.kind = kind
        self.device = device
        self.settings = settings or CameraSettings()

    def capture(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "pi":
            command = shutil.which("rpicam-still") or shutil.which("libcamera-still")
            if command is None:
                raise CameraError("rpicam-still was not found. Install Raspberry Pi camera apps or use --camera usb.")
            try:
                settings = self.settings
                result = subprocess.run(
                    [command, "--nopreview", "--timeout", "1500", "--camera", str(self.device),
                     "--ev", str(settings.ev), "--awb", settings.awb,
                     "--contrast", str(settings.contrast), "--output", str(path)],
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
