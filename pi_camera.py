"""Still-image capture for Raspberry Pi CSI cameras and USB webcams."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np


USB_WARMUP_FRAMES = 12
USB_BURST_FRAMES = 8


class CameraError(RuntimeError):
    """The requested camera could not produce a readable image."""


class USBStream:
    """Keep exposure warm; each request waits for an entirely new burst."""
    def __init__(self, device: int) -> None:
        self.device = device
        self.condition = threading.Condition()
        self.frames = deque(maxlen=USB_BURST_FRAMES)
        self.sequence = 0
        self.error = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._read, name="usb-camera", daemon=True)
        self.thread.start()

    def _read(self) -> None:
        camera = cv2.VideoCapture(self.device)
        try:
            if not camera.isOpened():
                raise CameraError(f"Could not open USB camera device {self.device}.")
            camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            warmup = USB_WARMUP_FRAMES
            while not self.stopped.is_set():
                ok, frame = camera.read()
                if not ok or not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
                    raise CameraError("The USB camera stopped producing readable frames. Reconnect it and try again.")
                if warmup:
                    warmup -= 1
                    continue
                with self.condition:
                    self.sequence += 1
                    self.frames.append(frame.copy())
                    self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()
        finally:
            camera.release()

    def capture(self) -> np.ndarray:
        with self.condition:
            target = self.sequence + USB_BURST_FRAMES
            ready = self.condition.wait_for(lambda: self.sequence >= target or self.error is not None or
                                           self.stopped.is_set(), timeout=8)
            if not ready or self.error is not None or self.stopped.is_set():
                raise CameraError(self.error or "Timed out waiting for fresh USB camera frames.")
            frames = list(self.frames)
        return select_stable_frame(frames)

    def close(self) -> None:
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=2)


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
                 settings: CameraSettings | None = None, *, keep_usb_open: bool = False) -> None:
        if kind not in ("pi", "usb"):
            raise ValueError("Camera must be 'pi' or 'usb'.")
        self.kind = kind
        self.device = device
        self.settings = settings or CameraSettings()
        self.keep_usb_open = keep_usb_open
        self.usb_stream = None

    def close(self) -> None:
        if self.usb_stream is not None:
            self.usb_stream.close()
            self.usb_stream = None

    def capture(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Empty and setup photos reuse their filenames. Never accept an old
        # photo when a camera command fails to create a new one.
        last_error = None
        for attempt in range(2 if self.kind == "pi" else 1):
            temporary = path.with_name(f".{path.stem}-{uuid4().hex}{path.suffix or '.jpg'}")
            try:
                detail = self._capture_new(temporary, timeout_ms=2000 if attempt == 0 else 5000)
                if (not temporary.is_file() or
                        cv2.imread(str(temporary), cv2.IMREAD_COLOR) is None):
                    message = "The camera did not save a new readable photo."
                    if detail:
                        message += " Camera output: " + detail.strip()[-300:]
                    raise CameraError(message)
                os.replace(temporary, path)
                return path
            except CameraError as exc:
                last_error = exc
            finally:
                temporary.unlink(missing_ok=True)
            if self.kind == "pi" and attempt == 0:
                time.sleep(0.5)
        raise CameraError(f"{last_error} Try again or check the camera connection.")

    def _capture_new(self, path: Path, *, timeout_ms: int = 2000) -> str | None:
        if self.kind == "pi":
            command = shutil.which("rpicam-still") or shutil.which("libcamera-still")
            if command is None:
                raise CameraError("rpicam-still was not found. Install Raspberry Pi camera apps or use --camera usb.")
            try:
                settings = self.settings
                result = subprocess.run(
                    [command, "--nopreview", "--timeout", str(timeout_ms), "--camera", str(self.device),
                     "--ev", str(settings.ev), "--awb", settings.awb,
                     "--contrast", str(settings.contrast), "--encoding", "jpg",
                     "--output", str(path)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CameraError(f"Camera capture failed: {exc}") from exc
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise CameraError(f"Camera capture failed: {detail or 'unknown rpicam error'}")
            return getattr(result, "stderr", "") or getattr(result, "stdout", "")
        else:
            if self.keep_usb_open:
                if self.usb_stream is None:
                    self.usb_stream = USBStream(self.device)
                try:
                    frame = self.usb_stream.capture()
                except CameraError:
                    if self.usb_stream.error is not None:
                        self.close()
                    raise
                if not cv2.imwrite(str(path), frame):
                    raise CameraError("USB camera did not produce a usable frame.")
                return None
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
