#!/bin/bash
# Puts "the gantry always plays Black" on the Raspberry Pi, building on what is already there:
#   in person:  the person is White, Stockfish is Black and the gantry carries its moves (already so)
#   website:    the person at the board is White (moves by hand, taps Done), the website player is
#               Black and the gantry carries their moves
# Steps (each backs up first):
#   1. the gantry code            -> ~/chess-gantry/gantry           (deploy_pi.sh)
#   2. the website game server    -> ~/chess-relay/backend           (chess-website working tree)
#   3. relay.env: CHESS_GANTRY=arduino, CHESS_GANTRY_DIR, CHESS_GANTRY_PORT (only keys not set yet)
#   4. the touch screen's Online files (pi_online.py, pi_gantry.py, pi_touch.py) -> ~/chess-board-pi-only,
#      ONLY if the Pi's pi_touch.py is still the copy we put there last time; otherwise it stops and
#      shows the difference, so Hiyab's newer edits are never overwritten.
#   bash deploy_online.sh [user@host]
set -euo pipefail
PI="${1:-smart@chessboard.local}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OLD=/private/tmp/claude-501/-Users-kennakao-Library-Application-Support-Claude-scratch-workspaces-5e2168a9-8580-4fd5-a8ee-2ed4de64effa-0834ba4c-a82b-4ff8-ace6-8d780e1bef24-scratch-2026-11-04-cdcbea/e4e14634-5243-4287-b671-df7540eb4a51/scratchpad
WEBSITE="$OLD/chess-website"
TOUCH="$OLD/handoff/DUEN-backup-2026-11-06/repos/working-trees/board-code__online-board-player__touch-screen"
LAST_TOUCH="$OLD/pi-copy/pi_touch.py"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "== 0. backups on the Pi (~/chess-backups/*-$STAMP)"
ssh "$PI" "mkdir -p ~/chess-backups && tar czf ~/chess-backups/relay-backend-$STAMP.tgz -C ~/chess-relay backend \
  && cp ~/.config/duen-chess/relay.env ~/chess-backups/relay.env-$STAMP \
  && tar czf ~/chess-backups/touch-online-$STAMP.tgz -C ~/chess-board-pi-only pi_touch.py \$(cd ~/chess-board-pi-only && ls pi_online.py pi_gantry.py 2>/dev/null) \
  && ls -lh ~/chess-backups | tail -3"

echo "== 1. gantry code"
bash "$HERE/deploy_pi.sh" "$PI"

echo "== 2. website game server (board person White, website Black)"
bash "$WEBSITE/scripts/deploy-pi.sh" "$PI"

echo "== 3. relay.env: run the board-game moves on the ESP32 through gantry_driver.py"
ssh "$PI" 'f=~/.config/duen-chess/relay.env
add() { grep -q "^$1=" "$f" || { echo "$1=$2" >> "$f"; echo "  added $1=$2"; }; }
add CHESS_GANTRY arduino
add CHESS_GANTRY_DIR "$HOME/chess-gantry/gantry/pi"
add CHESS_GANTRY_PORT /dev/ttyUSB0
grep "^CHESS_GANTRY" "$f"
systemctl --user restart duen-relay.service && sleep 2 && systemctl --user is-active duen-relay.service
journalctl --user -u duen-relay -n 5 --no-pager | grep -i gantry || true'

echo "== 4. touch screen Online files"
TMP="$(mktemp)"; scp -q "$PI:chess-board-pi-only/pi_touch.py" "$TMP"
if cmp -s "$TMP" "$LAST_TOUCH"; then
  scp -q "$TOUCH/pi_online.py" "$TOUCH/pi_gantry.py" "$TOUCH/pi_touch.py" "$PI:chess-board-pi-only/"
  scp -q "$TOUCH"/test/test_pi_online.py "$TOUCH"/test/test_pi_gantry.py "$PI:chess-board-pi-only/test/"
  ssh "$PI" "cd ~/chess-board-pi-only && .venv/bin/python -m unittest test.test_pi_online test.test_pi_gantry 2>&1 | tail -3"
  bash "$OLD/tools/restart_touch.sh"
else
  echo "  STOP: the Pi's pi_touch.py changed since we last put it there (Hiyab?). Not touching it."
  diff "$LAST_TOUCH" "$TMP" | head -40 || true
fi
rm -f "$TMP"
echo "== done"
