#!/bin/bash
# Run on the Pi when ready to deploy the integrated touchscreen game.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-tk stockfish
if ! command -v rpicam-still >/dev/null && ! command -v libcamera-still >/dev/null; then
  sudo apt-get install -y rpicam-apps
fi
python3 -m venv "$PROJECT/.venv"
"$PROJECT/.venv/bin/pip" install -r "$PROJECT/requirements.txt"
"$PROJECT/.venv/bin/python" "$PROJECT/scripts/install_touch_autostart.py" --standalone
mkdir -p "$HOME/Desktop"
cat > "$HOME/Desktop/Camera Chess.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Camera Chess
Comment=Play White on the physical board; the gantry plays Black
Exec="$PROJECT/.venv/bin/python" "$PROJECT/pi_standalone.py"
Path=$PROJECT
Terminal=false
Categories=Game;
EOF
chmod +x "$HOME/Desktop/Camera Chess.desktop"
echo 'Installed. Open Camera Chess on the Pi desktop, or log in to the desktop again.'
echo 'The touchscreen works on its own; the optional laptop Confirm page uses the SSH tunnel on port 8789.'
