"""Still-image capture for Raspberry Pi CSI cameras and USB webcams."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np


USB_WARMUP_FRAMES = 12
USB_BURST_FRAMES = 8


class CameraError(RuntimeError):
    """The requested camera could not produce a readable image."""


def _preview_gray(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    scale = min(1.0, 320 / max(gray.shape))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32)


def _frames_are_still(before: np.ndarray, after: np.ndarray) -> bool:
    if before.shape != after.shape:
        return False
    difference = np.abs(after - before)
    # Check small regions as well as the whole frame. A moving pawn or hand
    # must not disappear into the average of an otherwise unchanged image.
    tiles = [float(tile.mean()) for row in np.array_split(difference, 12)
             for tile in np.array_split(row, 16, axis=1) if tile.size]
    return float(difference.mean()) <= 3.0 and max(tiles) <= 12.0


def select_stable_frame(frames: list[np.ndarray]) -> np.ndarray:
    """Choose a sharp exposure only after the view settles for three frames."""
    if len(frames) < 3:
        raise CameraError("The camera did not produce enough readable frames. Try again.")
    previews = [_preview_gray(frame) for frame in frames]
    current = [0]
    for index in range(1, len(frames)):
        if _frames_are_still(previews[index - 1], previews[index]):
            current.append(index)
        else:
            current = [index]
    if len(current) < 3:
        raise CameraError("The camera view is still changing. Keep the board steady and lift your hands away.")
    best = max(current, key=lambda index: (
        float(cv2.Laplacian(previews[index], cv2.CV_32F).var()), index))
    if float(previews[best].std()) < 1.0:
        raise CameraError("The camera image has too little detail. Check the lens and lighting.")
    return frames[best]


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
        # Empty and setup photos reuse their filenames. Never accept an old
        # photo when a camera command fails to create a new one.
        temporary = path.with_name(f".{path.stem}-{uuid4().hex}{path.suffix or '.jpg'}")
        try:
            self._capture_new(temporary)
            if (not temporary.is_file() or
                    cv2.imread(str(temporary), cv2.IMREAD_COLOR) is None):
                raise CameraError("The camera did not save a new readable photo. Try again.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _capture_new(self, path: Path) -> None:
        if self.kind == "pi":
            command = shutil.which("rpicam-still") or shutil.which("libcamera-still")
            if command is None:
                raise CameraError("rpicam-still was not found. Install Raspberry Pi camera apps or use --camera usb.")
            try:
                settings = self.settings
                result = subprocess.run(
                    [command, "--nopreview", "--timeout", "2000", "--camera", str(self.device),
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
                for _ in range(USB_WARMUP_FRAMES):
                    camera.read()
                frames = []
                for _ in range(USB_BURST_FRAMES):
                    ok, image = camera.read()
                    if (ok and isinstance(image, np.ndarray) and image.ndim == 3 and
                            image.shape[2] == 3 and image.size):
                        frames.append(image.copy())
                frame = select_stable_frame(frames)
                if not cv2.imwrite(str(path), frame):
                    raise CameraError("USB camera did not produce a usable frame.")
            finally:
                camera.release()
