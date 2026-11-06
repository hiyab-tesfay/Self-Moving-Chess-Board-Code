# Raspberry Pi chessboard camera game

This branch contains the code to run the camera-based chess game on a Raspberry Pi. You play White against a local Stockfish engine. The touch screen compares each photo with the last verified board photo; `python-chess` tracks piece identities and checks move legality. After Stockfish announces Black's move, move that piece on the physical board and let the camera verify the result.

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

These command-line diagnostic commands still use the older empty-board method. The touch screen setup below needs only the fully set starting board.

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

The touch screen now calibrates from the **fully set starting board**. Put all 32 pieces in their standard starting squares, then tap **Take Setup Photo**. The app suggests an orientation from the lighter and darker starting pieces, and the preview shows the **aligned and rotated board**, with **a1** at the lower left and **h1** at the lower right. These labels mark the target squares; the preview also reports which edge appears to hold White's pieces. Check the actual pieces before confirming: if White's back row appears at the top, tap **Flip 180°**. If it appears on a side, tap **Rotate 90°** until it is at the bottom. The screen also shows the standard starting squares for the queens (**d1**, **d8**) and kings (**e1**, **e8**). Adjust brightness, contrast, and color warmth if needed, then tap **Confirm White at Bottom**. This is a manual confirmation: the camera does not identify piece types in the setup image. The image becomes the reference for the first move; each verified move photo becomes the reference for the next. You do not need to clear the board or take an empty-board photo when changing locations or lighting. Image edits cannot recover blur or clipped detail, so retake a bad setup photo.

The app finds corners from visible ArUco markers or specified coordinates. Otherwise, the **Setup photo corners** screen shows the photo rotated with White along the bottom. Use **Flip 180°** or **Rotate 90°** there if needed, then tap the four playing-area corners **as they appear on that screen** in this order: top-left, top-right, bottom-right, bottom-left. The app keeps those taps aligned with later camera photos. Then confirm the starting board and choose a difficulty. **Use Saved Calibration** also remains available, including for calibrations created by the previous empty-board method. Saved empty-board calibrations keep their previous behavior until you take a new setup-only calibration photo.

If the setup-only scanner repeatedly reports **No clear move detected**, tap **Calibrate With Empty Board** on the first calibration screen. Take an empty-board photo, set up all 32 pieces, and take the setup photo. The app then compares later scans with the empty-board reference. The empty-board path uses the same orientation controls and saves its own calibration. You can switch back with **Use Setup Photo Only** before taking the empty photo.

Setup-only play compares visual changes on each square between consecutive verified photos, correcting for local lighting shifts. Hold the camera steady during each exposure and keep hands and shadows off the board. ArUco markers or a fixed camera make alignment more reliable. Strong shadows, glare, blur, or a subtle capture can still make a scan uncertain; the app retries up to three photos before asking for help.

Select Beginner, Intermediate, or Advanced, then tap **Start Game**. With setup-only calibration, White can move immediately; tap **Scan Move** after moving the physical piece. One tap takes up to three photos when the first one is unclear or does not match a verifiable move. Every photo tries visible markers first, then reuses the most recently verified corners. The move-check photo and its corner markers use the saved setup orientation; manual corner taps are mapped back to the camera image for verification. After three unsuccessful photos, the app shows the last photo and asks you to fix the board, retake it, or adjust corners. A legal move appears in green. A clearly detected illegal or multiple-piece change appears in a large red panel, with changed squares highlighted on the photo. The app names a piece's source and destination only when the photo supports that conclusion. Tap a white piece on the displayed board to see legal destination squares. If several legal moves look identical to the camera, choose the actual move on screen; this includes promotion choices. Follow Stockfish's displayed move on the physical board, then tap **Verify Computer Move**. If verification fails, the panel shows the expected computer move in green and the detected changes in red. **End Game** records a resignation; **New Game** returns to difficulty selection. Older saved empty-board calibrations still ask you to scan the starting board after choosing a difficulty.

The camera tracks square changes, not piece identities. It cannot independently prove which piece was placed on a square, and captures or promotions may require an on-screen choice. Keep the physical pieces in sync with the displayed board. The real LCD, touch coordinates, camera, board, and lighting still require a hardware test. The command-line tools described above still use the older empty-board method for offline diagnostics.

### Open the touch screen automatically when the Pi starts

On the Pi, use Raspberry Pi OS **Desktop** with the **labwc** window manager. In **Preferences → Control Centre → System**, set **Boot** to **To desktop** and turn on **Desktop Auto Login**. In the **Display** tab, turn off **Screen Blanking** if the menu should stay visible while idle. Raspberry Pi's [configuration guide](https://www.raspberrypi.com/documentation/computers/configuration.html) describes these settings. Test `pi_touch.py` manually first, then from this project directory run:

```sh
.venv/bin/python scripts/install_touch_autostart.py
sudo reboot
```

After the desktop loads, the calibration screen opens full screen on the connected display. For a USB camera, install with `.venv/bin/python scripts/install_touch_autostart.py --camera usb --device 0` (change the device number if necessary). The installer adds one entry to `~/.config/labwc/autostart`, preserving any other entries. It follows Raspberry Pi's [labwc autostart guidance](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/). If the app does not open, check `~/.local/state/chess-board-pi-only/touchscreen.log`.

To stop automatic launch, run `.venv/bin/python scripts/install_touch_autostart.py --remove` on the Pi and reboot. This autostart setup opens the app after the desktop loads; it does not replace the Pi's early boot splash screen.

The `vision_cli.py` commands provide offline calibration, image inspection, and debugging with saved photos. Run `.venv/bin/python vision_cli.py --help` for options.
