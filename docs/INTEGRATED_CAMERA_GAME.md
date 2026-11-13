# Camera + gantry game

You play **White by hand**. After moving a piece on the physical board, lift your
hands away and press **Confirm move** on the Pi touchscreen or laptop. The Pi
checks two camera photos, matches a legal move, asks its local Stockfish engine
for Black's reply, and moves Black with the gantry. Press **Confirm Black's move**
to verify that physical position before taking your next turn. Nothing advances
just because a screen square is clicked, and the laptop has no separate game.

The Pi touchscreen is sufficient for the whole game. The laptop is optional.

## Install on the Pi when ready

Use Raspberry Pi OS Desktop, a connected Pi camera, the touchscreen, and the ESP32
on USB. From this repository on branch `zain-fully-integragted`:

```bash
bash scripts/install_integrated_pi.sh
```

This installs the Python dependencies, Stockfish and camera tools, creates a
**Camera Chess** desktop shortcut, and registers the integrated app with labwc
desktop startup. The Pi must boot to its desktop; enable Desktop Auto Login in
Raspberry Pi OS if the app should open without a keyboard. The installer does not
reboot or begin a game. Open the desktop shortcut to launch it, or run:

```bash
.venv/bin/python pi_standalone.py
```

For a USB webcam, use `--camera usb --device 0`; install its autostart with
`.venv/bin/python scripts/install_touch_autostart.py --standalone --camera usb`.

## Set up entirely on the touchscreen

1. Open **Home gantry at e2**. Press **Motors off**, position the magnet beneath
   the centre of e2, then press **Set home here — e2** yourself. The carriage stays
   there. Press **Back** to retain this connection for the game.
2. Open **Calibrate camera**. Take the empty-board and starting-position photos,
   select the four outer playing-area corners if no markers are present, and
   confirm the board orientation. Keep the camera fixed after calibration.
3. Start and set Black's target Elo slider. Set up all 32 pieces and press **Confirm
   starting board**. An old photo alone never verifies the current starting setup.
4. Move White physically and press **Confirm move**. If a position is unclear,
   illegal, or still changing between the two photos, correct it and confirm
   again. The engine and gantry wait for a verified move.
5. The gantry plays Black. Press **Confirm Black's move** after it finishes.

For your captures, remove the Black piece and place your White piece before
confirming. For Black's captures, the app first asks you to remove the captured
White piece. Press **Confirm piece removed**; the camera must verify the square
is empty before the gantry carries Black there. En passant and castling are
handled by the existing move planner. Promotions require you to replace the
pawn; choose White's promotion on either display, or acknowledge Black's with
**Pawn replaced — Confirm**.

The camera tracks occupancy and visual changes from a known position, rather
than independently recognizing every piece's identity. It cannot prove which
promotion piece you placed. Hands, lighting, alignment, piece contrast and
calibration still require checks with the actual board and camera.

## Optional laptop Confirm button

White is always moved by the human; recording White's move does not command
the gantry. Stockfish chooses Black's replies and the gantry carries them.
After starting a game, use **Black bot · target Elo** on the touchscreen or
laptop before confirming the starting board. You can also adjust it during
White's turn; it applies to the next Black reply. The two screens share the
same value. It is an approximate engine strength, not a guaranteed human rating.
The slider uses the installed Stockfish build's advertised `UCI_Elo` bounds
and enables `UCI_LimitStrength`, rather than assigning invented ratings to
the legacy difficulty presets. See the [official Stockfish options](https://github.com/official-stockfish/Stockfish/blob/master/src/engine.cpp).

While Camera Chess is running on the Pi, forward its local companion page:

```bash
ssh -N -L 8789:127.0.0.1:8789 smart@chessboard.local
```

Open <http://localhost:8789/>. It shows the Pi's game, instructions, promotion
choices, **Confirm** and **Stop gantry**. Both Confirm buttons invoke the same
camera-check routine. Busy, duplicate and stale requests cannot play another
move. Losing the laptop connection does not end the Pi's game.

The companion listens only on the Pi's loopback interface. It does not expose
gantry control to the Wi-Fi network directly. Do not bind it publicly.

## Motion ownership and recovery

The integrated launcher temporarily stops the existing `zain-gantry.service`
dashboard after checking that it is idle. Only one application owns the USB
controller. Closing Camera Chess restores that dashboard. If a move is running,
the launcher refuses to interrupt it. Other desktop, web and camera-only entry
points remain available.

Every new controller connection needs a fresh manual e2 home. Saved coordinates
are never silently restored in this mode. A controller restart or **Motors off**
requires homing again. **STOP** is available on both displays and bypasses the
camera worker; a stopped game must be ended and set up again. Ending or closing
the integrated game does not automatically move pieces back to their starting
squares.

Calibration and the integrated motion configuration live in
`~/.local/state/zain-camera-chess/`. Existing measured geometry and console speeds
are copied on first launch; the manual dashboard's configuration is preserved.
To remove automatic launch, run
`.venv/bin/python scripts/install_touch_autostart.py --remove`.

## Software checks

```bash
.venv/bin/python -m unittest test.test_touch_companion test.test_integrated_touch test.test_gantry_manual_home_guard test.test_pi_touch test.test_pi_touch_app test.test_autostart
```

These checks use recorded observations and stub protocols. They do not connect
to a controller or build/run the motor simulator. They verify the software flow;
they do not establish real camera accuracy or physical motion accuracy.
