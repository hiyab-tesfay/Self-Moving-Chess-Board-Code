#!/bin/bash
# Opens the gantry terminal on the Raspberry Pi over SSH. The Pi talks to the ESP32 over USB.
#   bash pi_terminal.sh                    the ESP32 the Pi finds on USB (/dev/ttyUSB0)
#   bash pi_terminal.sh --port sim         the pretend board on the Pi (nothing moves)
#   PI=user@host bash pi_terminal.sh       another Pi
PI="${PI:-smart@chessboard.local}"   # if chessboard.local does not resolve: PI=smart@10.0.0.63 bash pi_terminal.sh
ARGS=""
for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done
exec ssh -t "$PI" "cd ~/chess-gantry/gantry/console && exec ~/chess-board-pi-only/.venv/bin/python gantry_term.py$ARGS"
