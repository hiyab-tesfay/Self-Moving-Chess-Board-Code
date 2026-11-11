#!/bin/bash
# Opens the DUEN gantry console: a page on this Mac that drives the gantry over USB.
#   bash console.sh          the real board: plug the ESP32 in, pick its port on the page, Connect
#   bash console.sh sim      the pretend board: nothing real moves
# The first run makes a Python environment next to this script (python-chess, pyserial).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GANTRY="$(dirname "$HERE")"
VENV="$HERE/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for candidate in /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
                   /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$(command -v "$candidate")"; break; fi
  done
  [ -n "$PY" ] || { echo "No python3 found: brew install python"; exit 1; }
  echo "== first run: making a Python environment with python-chess and pyserial ($("$PY" --version))"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet chess pyserial
fi
if [ ! -x "$GANTRY/firmware/host/virtual_gantry" ]; then
  echo "== building the pretend board"
  make -C "$GANTRY/firmware/host" virtual_gantry >/dev/null
fi
ARGS=()
if [ "${1:-}" = "sim" ]; then ARGS=(--port sim); fi
(sleep 2; open "http://127.0.0.1:8788") >/dev/null 2>&1 &
exec "$VENV/bin/python" "$HERE/console_server.py" ${ARGS[@]+"${ARGS[@]}"}
