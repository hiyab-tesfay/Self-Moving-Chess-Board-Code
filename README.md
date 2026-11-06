# Raspberry Pi chessboard camera game

This branch contains the code to run the camera-based chess game on a Raspberry Pi. You play White against a local Stockfish engine. The touch screen compares each photo with an empty-board calibration; `python-chess` tracks piece identities and checks move legality. After Stockfish announces Black's move, move that piece on the physical board and let the camera verify the result.

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

## Command-line diagnostics (older empty-board method)

These command-line diagnostic commands also use an empty-board photo, followed by a photo of the starting position.

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

The first screen is **Empty board**. Remove all pieces and tap **Take Empty Photo**. The app uses ArUco markers or specified corner coordinates when available; otherwise, tap the four playing-area corners in the order shown. Then place all 32 pieces in the standard starting position and tap **Take Setup Photo**. If White is not at the bottom of the setup preview, use **Flip 180°** or **Rotate 90°** before tapping **Check Board**. Brightness, contrast, and color warmth sliders update the captured setup photo. The same adjustments are applied to the empty photo and later scans. Retake blurred or clipped photos.

The app compares the empty and setup photos to learn which squares are occupied. After calibration, choose a difficulty and tap **Start Game**. Tap **Scan Starting Board** once to verify the pieces, then make White's move and tap **Scan Move**. The app retries up to three photos if a scan is unclear. It uses the saved orientation for move-check previews, corner taps, and highlighted squares. A legal move appears in green; an illegal or multiple-piece change appears in a large red panel. Choose a promotion on screen when prompted, move Stockfish's piece on the physical board, and tap **Verify Computer Move**. **End Game** and **New Game** return you to the menu.

Keep the camera fixed and hands off the board during each exposure. Lighting changes, shadows, glare, and blur can still make detection uncertain. The camera tracks square occupancy rather than piece identity, so keep the physical pieces in sync with the displayed board. Existing empty-board calibrations can be reused with **Use Saved Calibration**. A calibration saved by the previous setup-photo-only method must be replaced with a new empty and setup photo pair.

### Open the touch screen automatically when the Pi starts

On the Pi, use Raspberry Pi OS **Desktop** with the **labwc** window manager. In **Preferences → Control Centre → System**, set **Boot** to **To desktop** and turn on **Desktop Auto Login**. In the **Display** tab, turn off **Screen Blanking** if the menu should stay visible while idle. Raspberry Pi's [configuration guide](https://www.raspberrypi.com/documentation/computers/configuration.html) describes these settings. Test `pi_touch.py` manually first, then from this project directory run:

```sh
.venv/bin/python scripts/install_touch_autostart.py
sudo reboot
```

After the desktop loads, the calibration screen opens full screen on the connected display. For a USB camera, install with `.venv/bin/python scripts/install_touch_autostart.py --camera usb --device 0` (change the device number if necessary). The installer adds one entry to `~/.config/labwc/autostart`, preserving any other entries. It follows Raspberry Pi's [labwc autostart guidance](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/). If the app does not open, check `~/.local/state/chess-board-pi-only/touchscreen.log`.

To stop automatic launch, run `.venv/bin/python scripts/install_touch_autostart.py --remove` on the Pi and reboot. This autostart setup opens the app after the desktop loads; it does not replace the Pi's early boot splash screen.

The `vision_cli.py` commands provide offline calibration, image inspection, and debugging with saved photos. Run `.venv/bin/python vision_cli.py --help` for options.
