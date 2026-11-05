# Raspberry Pi chessboard camera game

This branch contains the code to run the camera-based chess game on a Raspberry Pi. You play White against a local Stockfish engine. The camera detects which squares are occupied; `python-chess` tracks piece identities and checks move legality. After Stockfish announces Black's move, move that piece on the physical board and let the camera verify the result.

The program does not control motors or identify piece types from images. Moves with identical occupancy patterns, including promotions and some captures, require a manual choice. The camera and calibration still need to be tested with your actual board and lighting.

## Install on the Pi

Use 64-bit Raspberry Pi OS, Python 3.10 or newer, and a Pi camera or USB webcam. Install Stockfish and, for a Pi camera, the camera tools:

```sh
sudo apt update
sudo apt install python3-venv python3-tk stockfish rpicam-apps
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For a USB webcam, `rpicam-apps` is unnecessary. Pass `--camera usb` to the commands below and, if needed, `--device 1`. If Stockfish is outside `PATH`, set `STOCKFISH_PATH` to its executable.

## Calibrate and check the board

Keep the camera, board, and lighting steady. Set up four ArUco markers using `.venv/bin/python vision_cli.py markers --out markers`; put markers 0, 1, 2, 3 at the top-left, top-right, bottom-right, and bottom-left corners of the playing area. Keep them upright and visible in every photo. If you cannot use markers, supply the four playing-area corners with `--corners TL_X TL_Y TR_X TR_Y BR_X BR_Y BL_X BL_Y`. Use `--rotate cw`, `ccw`, or `180` during setup if square a8 is not at the top left after alignment.

```sh
.venv/bin/python pi_game.py setup
.venv/bin/python pi_game.py check
```

`setup` photographs the empty board and then the standard 32-piece starting position. `check` compares a new photo with the starting occupancy. To test move detection, move the pawn from e2 to e4 and run:

```sh
.venv/bin/python pi_game.py scan
.venv/bin/python pi_game.py check --moves e2e4
```

The scan should show `e2e4 (e4)`. For later positions, pass every previous UCI move with `--moves`, or pass a known position with `--fen`. An unambiguous match or successful check exits with code 0; an uncertain, ambiguous, or mismatched result exits with code 2. Captured photos are saved under `calibration/captures/`.

If the camera shifts between the two setup photos, pass both `--empty-corners` and `--starting-corners` with each photo's own eight corner coordinates. For later photos from a moving camera, pass that photo's `--corners` to `check` or `scan`. ArUco markers allow automatic alignment of each photo and are preferable for routine play.

## Play

Once `check` and `scan` work with the physical board:

```sh
.venv/bin/python pi_game.py play --level beginner
```

The program verifies the starting setup, scans each White move, announces Stockfish's Black move, and verifies the board after you make it. It asks for a new photo when the observation is uncertain or disagrees with the expected position. Enter `q` to quit. A new session starts from the standard position.

## Touch screen play

Connect the LCD touch screen and make sure Raspberry Pi OS shows its desktop on that display and touch input works. From a desktop terminal, launch the full-screen touch interface:

```sh
.venv/bin/python pi_touch.py
```

For a USB camera, use `--camera usb` (and `--device 1` if needed). For development in a normal window, add `--windowed`. The `--corners TL_X TL_Y TR_X TR_Y BR_X BR_Y BL_X BL_Y` option is available when corners are already known; touch calibration can also find or select corners without it.

The first screen after launch is **Calibrate Board**. If this camera and board already have a valid saved calibration, tap **Use Saved Calibration**. Otherwise, take one empty-board photo, place the starting pieces, and take one setup photo. The captured setup photo stays on screen with brightness, contrast, and color warmth sliders. Moving a slider changes that photo immediately. Tap **Check Board** when the pieces are clear on both square colors. These edits are applied to the stored empty photo and every later game scan, so slider changes do not require new photos. Image edits cannot recover detail that was blurred or clipped when the photo was taken; retake such a photo.

Remove every piece and tap **Take Empty Photo**. If all four ArUco markers are visible, the app finds the empty-board corners automatically. Otherwise, tap the four outer corners in this order: top-left, top-right, bottom-right, bottom-left. Place all 32 pieces in the standard starting position, with White at the bottom of the photo, and tap **Take Setup Photo**. The app tries markers first and then reuses the empty photo's corners. If alignment fails, use **Redo Setup Corners** to correct that photo. After **Check Board** succeeds, tap **Choose Difficulty**. You can recalibrate from the difficulty menu whenever the camera or lighting changes.

New calibrations measure the lighting shift at each square's bare corners before deciding whether its center contains a piece. This tolerates gradual brightness and exposure changes. A handheld camera can be aligned photo by photo with markers or manual corner taps, but hold it steady during each exposure and keep hands and shadows off the board. Strong local shadows, glare, and blur can still make a scan uncertain. Calibrations created before this lighting correction continue using the older scoring method; retake both calibration photos to enable the new method.

Select Beginner, Intermediate, or Advanced, then tap **Start Game**. Place the pieces in the starting position and tap **Scan Starting Board**. Move White on the physical board and tap **Scan Move**. Every scan tries visible markers first, then reuses the most recently verified corners. The app asks for manual corners only when none are available or a scan cannot be verified. Reused corners work best with a fixed camera; if the camera moves, use **Adjust Photo Corners** when prompted. A legal move appears in green. A clearly detected illegal move appears in a large red panel naming the piece and its source and destination, with the squares highlighted on the photo. If the image is unclear, the panel says the move could not be verified instead of guessing. Tap a white piece on the displayed board to see its legal destination squares. If several legal moves look identical to the camera, choose the actual move on screen; this includes choosing Queen, Rook, Bishop, or Knight for promotion. Follow Stockfish's displayed move on the physical board, then tap **Verify Computer Move**. **End Game** records a resignation, and the result screen offers **New Game**. Press Escape to exit the app when using a keyboard.

If only one or two squares are hard to see during setup, the app highlights them and asks you to confirm they match the physical board. During play, unclear squares outside the move can be treated as unchanged; a scan still stops if the move touches an unclear square or if too many squares are unclear.

The camera observes occupied squares, not piece identities. It cannot independently prove which piece was placed on a square or distinguish promotions and some captures from the image alone. Keep the physical pieces in sync with the displayed board and use the on-screen choice when prompted. The real LCD, touch coordinates, camera, board, and lighting still require a hardware test. The command-line `check` and `scan` commands remain useful when diagnosing a difficult camera view.

### Open the touch screen automatically when the Pi starts

On the Pi, use Raspberry Pi OS **Desktop** with the **labwc** window manager. In **Preferences → Control Centre → System**, set **Boot** to **To desktop** and turn on **Desktop Auto Login**. In the **Display** tab, turn off **Screen Blanking** if the menu should stay visible while idle. Raspberry Pi's [configuration guide](https://www.raspberrypi.com/documentation/computers/configuration.html) describes these settings. Test `pi_touch.py` manually first, then from this project directory run:

```sh
.venv/bin/python scripts/install_touch_autostart.py
sudo reboot
```

After the desktop loads, the calibration screen opens full screen on the connected display. For a USB camera, install with `.venv/bin/python scripts/install_touch_autostart.py --camera usb --device 0` (change the device number if necessary). The installer adds one entry to `~/.config/labwc/autostart`, preserving any other entries. It follows Raspberry Pi's [labwc autostart guidance](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/). If the app does not open, check `~/.local/state/chess-board-pi-only/touchscreen.log`.

To stop automatic launch, run `.venv/bin/python scripts/install_touch_autostart.py --remove` on the Pi and reboot. This autostart setup opens the app after the desktop loads; it does not replace the Pi's early boot splash screen.

The `vision_cli.py` commands provide offline calibration, image inspection, and debugging with saved photos. Run `.venv/bin/python vision_cli.py --help` for options.
