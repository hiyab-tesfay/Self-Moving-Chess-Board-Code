"""Register the touch UI with Raspberry Pi OS labwc desktop autostart."""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import tempfile
from pathlib import Path


BEGIN = "# BEGIN chess-board-pi-only touch screen"
END = "# END chess-board-pi-only touch screen"


def without_our_block(contents: str) -> str:
    lines = contents.splitlines(keepends=True)
    beginnings = [index for index, line in enumerate(lines) if line.rstrip("\n") == BEGIN]
    endings = [index for index, line in enumerate(lines) if line.rstrip("\n") == END]
    if not beginnings and not endings:
        return contents
    if len(beginnings) != 1 or len(endings) != 1 or beginnings[0] >= endings[0]:
        raise ValueError("Existing touch screen autostart block is incomplete; inspect it manually.")
    return "".join(lines[:beginnings[0]] + lines[endings[0] + 1:])


def autostart_entry(project: Path, log: Path, camera: str, device: int) -> str:
    command = [str(project / ".venv/bin/python"), str(project / "pi_touch.py"),
               "--calibration", str(project / "calibration"),
               "--camera", camera, "--device", str(device)]
    return (f"{BEGIN}\n"
            f"cd {shlex.quote(str(project))} && {shlex.join(command)} "
            f">> {shlex.quote(str(log))} 2>&1 &\n"
            f"{END}\n")


def update_autostart(path: Path, entry: str | None) -> bool:
    previous = path.read_text() if path.exists() else ""
    clean = without_our_block(previous)
    updated = clean
    if entry is not None:
        updated += ("" if not clean or clean.endswith("\n") else "\n") + entry
    if updated == previous:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".autostart-", delete=False) as temp:
        temp.write(updated)
        temporary_path = Path(temp.name)
    try:
        if path.exists():
            temporary_path.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", choices=("pi", "usb"), default="pi")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--remove", action="store_true", help="Remove this app from autostart")
    args = parser.parse_args(argv)

    project = Path(__file__).resolve().parents[1]
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    autostart = config_home / "labwc/autostart"
    if args.remove:
        changed = update_autostart(autostart, None)
        print(f"Removed touch screen autostart from {autostart}." if changed else
              "Touch screen autostart was not installed.")
        return 0

    python = project / ".venv/bin/python"
    if not python.is_file():
        parser.error(f"Project virtual environment is missing: {python}")
    log = state_home / "chess-board-pi-only/touchscreen.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    update_autostart(autostart, autostart_entry(project, log, args.camera, args.device))
    print(f"Touch screen will start at desktop login. Autostart: {autostart}")
    print(f"Startup log: {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
