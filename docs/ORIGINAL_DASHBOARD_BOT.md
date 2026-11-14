# Original gantry dashboard with Black bot

This uses the existing `zain-gantry-changes` console layout and its full manual,
homing, tuning, diagnostic, speed and recovery controls. It adds a small bot panel.
Camera detection is not used in this mode.

```bash
.venv/bin/python gantry/console/console_server.py --http 8789 --bot-black --config /path/to/gantry_config.json
```

Connect to the gantry, release motors, place the magnet beneath the centre of
**e7**, and explicitly press **Set home here — e7**. Set up the physical pieces
and use **New game (pieces set up)** to reset the displayed position if needed.

Move White on the physical board, including removing captured Black pieces.
Select White's FROM and TO squares in the on-screen **Chess moves** mode and
confirm that you moved it. Recording White never commands a carry. Stockfish
chooses Black's legal reply at the selected target Elo and the gantry executes
it automatically. The rating is approximate engine strength. For Black captures,
the existing console asks you to remove the captured piece and press **Done**.
Promotion and recovery instructions use the original prompts.

**Pause bot for manual controls** permits Free carry between bot operations.
Resume it before recording another White move. **Play Black reply** retries a
Black turn after a connection or recovery issue; it does nothing on White's turn.
Stop remains immediate; a late engine result cannot start a move after Stop.
Running the console without `--bot-black` retains its previous manual behavior.

No camera app should own the same USB controller at the same time. The Pi's
`zain-gantry.service` runs this dashboard; the previous camera touchscreen app's
autostart is removed while using this mode.
