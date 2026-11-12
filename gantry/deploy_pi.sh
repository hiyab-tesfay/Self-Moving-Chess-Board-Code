#!/bin/bash
# Copies this gantry code to the Raspberry Pi (smart@chessboard.local) into ~/chess-gantry/gantry, the gantry
# folder the Pi already has, after a backup of what is there. The Pi runs it with the touch app's Python
# (~/chess-board-pi-only/.venv: pyserial and python-chess are already in it).
#   bash deploy_pi.sh                 deploy to smart@chessboard.local
#   bash deploy_pi.sh user@host       somewhere else
#   bash deploy_pi.sh --sim [host]    also build the pretend board on the Pi (only needed for 'connect sim')
set -euo pipefail
SIM=0
if [ "${1:-}" = "--sim" ]; then SIM=1; shift; fi
PI="${1:-smart@chessboard.local}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "== backup of the Pi's current gantry code"
ssh "$PI" "mkdir -p ~/chess-gantry-backups && cd ~/chess-gantry && tar czf ~/chess-gantry-backups/gantry-before-$STAMP.tgz gantry && ls -lh ~/chess-gantry-backups/gantry-before-$STAMP.tgz"

# Tune answers made on the Pi live in its pi/gantry_config.json: once it has some, keep that file.
KEEP=()
if ssh "$PI" "grep -qE 'square_nudges_mm|calibrated_on_pi|\"start_mm\": \\[|\"home_spot_mm\": \\[' ~/chess-gantry/gantry/pi/gantry_config.json 2>/dev/null"; then
  echo "== the Pi's gantry_config.json has Tune / 'centre is here' answers or a saved start / home spot: keeping it"
  KEEP=(--exclude 'pi/gantry_config.json')
fi

echo "== copying the code"
rsync -az --delete "${KEEP[@]+"${KEEP[@]}"}" \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'firmware/host/test_motion' --exclude 'firmware/host/virtual_gantry' \
  --exclude 'deploy_pi.sh' --exclude 'pi_terminal.sh' --exclude 'console/camera_cal/' \
  "$HERE/" "$PI:chess-gantry/gantry/"

# A kept Pi config still gets the Mac's motion keys (axis swap, drag lag, mm per turn); the Pi's copy is backed up first.
if [ ${#KEEP[@]} -gt 0 ]; then
  echo "== merging the Mac's motion keys into the Pi's kept gantry_config.json"
  ssh "$PI" "cp ~/chess-gantry/gantry/pi/gantry_config.json ~/chess-gantry-backups/gantry_config-before-merge-$STAMP.json && python3 -c '
import json, os, sys
p = os.path.expanduser(\"~/chess-gantry/gantry/pi/gantry_config.json\")
mac = json.load(sys.stdin); pi = json.load(open(p))
for k in (\"swap_xy\", \"_swap_xy_comment\", \"drag_lag_mm\", \"_drag_lag_comment\", \"mm_per_turn\", \"_mm_per_turn_comment\", \"servo_deg\", \"servo_settle_ms\", \"_servo_deg_comment\"):
    if k in mac: pi[k] = mac[k]
json.dump(pi, open(p, \"w\"), indent=2); print(\"merged\")
'" < "$HERE/pi/gantry_config.json"
fi

if [ "$SIM" = 1 ]; then
  echo "== building the pretend board on the Pi (for 'connect sim')"
  ssh "$PI" 'cd ~/chess-gantry/gantry && (command -v g++ >/dev/null && make -s -C firmware/host virtual_gantry && echo "pretend board built" || echo "no g++: connect sim will not work, the real board will")'
fi
echo "== checking the Python side"
ssh "$PI" 'PY=~/chess-board-pi-only/.venv/bin/python
cd ~/chess-gantry/gantry/console && $PY -c "import serial, chess, console_server, gantry_term; print(\"python OK:\", serial.VERSION, chess.__version__)"
ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "no ESP32 on USB yet (plug it into the Pi)"'
echo "== done. Open the terminal with: bash $HERE/pi_terminal.sh"
