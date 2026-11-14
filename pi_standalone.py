"""Run the camera + gantry touchscreen on the Pi without a companion computer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

PROJECT = Path(__file__).resolve().parent


def prepare_config(source: Path, destination: Path, settings: Path | None = None) -> Path:
    """Reuse the measured geometry, with a separate config for camera-controlled Black."""
    config = json.loads(source.read_text())
    config["travel_speed_mm_s"] = min(float(config.get("travel_speed_mm_s", 40)), 40)
    config["carry_speed_mm_s"] = min(float(config.get("carry_speed_mm_s", 25)), 25)
    config.update(home_square="e7", home_on_start=False, manual_home_on_connect=True,
                  manual_black_moves=False)
    if settings is not None and settings.is_file():
        values = json.loads(settings.read_text())
        for console_key, config_key in {
            "travel_speed": "travel_speed_mm_s", "carry_speed": "carry_speed_mm_s",
            "accel": "accel_mm_s2", "servo_settle": "servo_settle_s",
            "grab_settle": "grab_settle_s", "release_settle": "release_settle_s",
        }.items():
            if console_key in values:
                config[config_key] = float(values[console_key])
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A camera game's tuning is persistent. Refresh measured defaults only on first launch.
    if not destination.exists():
        destination.write_text(json.dumps(config, indent=2) + "\n")
    else:
        saved = json.loads(destination.read_text())
        saved.update(home_square="e7", home_on_start=False, manual_home_on_connect=True,
                     manual_black_moves=False)
        destination.write_text(json.dumps(saved, indent=2) + "\n")
    return destination


def release_dashboard() -> bool:
    """One serial owner at a time. Never interrupt a move to start the camera app."""
    active = subprocess.run(["systemctl", "is-active", "--quiet", "zain-gantry.service"],
                            capture_output=True).returncode == 0
    if not active:
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8789/api/state", timeout=3) as response:
            state = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cannot check the manual gantry dashboard. Close it before starting camera chess.") from exc
    status = state.get("status") or {}
    if state.get("task") or state.get("waiting") or status.get("state") not in (None, "Idle", "Alarm", "Hold"):
        raise RuntimeError("The gantry is busy. Finish or stop its current task before starting camera chess.")
    subprocess.run(["sudo", "-n", "systemctl", "stop", "zain-gantry.service"], check=True,
                   capture_output=True, text=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", choices=("pi", "usb"), default="pi")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--calibration", type=Path,
                        default=Path.home() / ".local/state/zain-camera-chess/calibration")
    parser.add_argument("--gantry-config", type=Path)
    parser.add_argument("--gantry-port", default="auto")
    parser.add_argument("--windowed", action="store_true")
    args = parser.parse_args(argv)
    state_dir = Path.home() / ".local/state/zain-camera-chess"
    state_dir.mkdir(parents=True, exist_ok=True)
    stopped_dashboard = False
    lock = None
    try:
        import fcntl
        lock = (state_dir / "app.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Camera chess is already running on the Pi.") from exc
        runtime = Path.home() / ".local/state/zain-gantry/gantry_config.json"
        source = args.gantry_config or (runtime if runtime.is_file() else PROJECT / "gantry/pi/gantry_config.json")
        config = prepare_config(source, state_dir / "gantry_config.json",
                                PROJECT / "gantry/console/console_settings.json")
        stopped_dashboard = release_dashboard()
        os.environ.update(CHESS_GANTRY_PORT=args.gantry_port,
                          CHESS_GANTRY_DIR=str(PROJECT / "gantry/pi"),
                          CHESS_GANTRY_CONFIG=str(config), GANTRY_STATE_DIR=str(state_dir))
        from pi_touch import main as touch_main
        command = ["--standalone", "--companion-port", "8789", "--camera", args.camera,
                   "--device", str(args.device), "--calibration", str(args.calibration)]
        if args.windowed:
            command.append("--windowed")
        return touch_main(command)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        from tkinter import Tk, messagebox
        root = Tk()
        root.withdraw()
        messagebox.showerror("Camera chess could not start", str(exc), parent=root)
        root.destroy()
        return 1
    finally:
        if stopped_dashboard:
            subprocess.run(["sudo", "-n", "systemctl", "start", "zain-gantry.service"], capture_output=True)
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
