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

## Command-line diagnostics

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

The app opens on a simple **Start Game** screen. Choose a difficulty, then **Set Up Board**. Setup begins with an empty-board photo. Remove all pieces and tap **Take Empty Photo**. The app uses ArUco markers or specified coordinates when available; otherwise, tap the four corners of the playing squares, excluding the wooden border. Place all 32 pieces and tap **Take Setup Photo**. Use **Flip 180°** or **Rotate 90°** until White is at the bottom, then tap **Check Board**. **Image Settings** expands brightness, contrast, and warmth controls; changes update the captured image and apply to the empty photo and later scans. The side panel scrolls when expanded controls need more room.

After a new calibration, tap **Start Game**. The verified setup photo starts the game directly, so make White's move and tap **Scan Move**. When starting a later game with saved calibration, place all 32 pieces and tap **Scan Starting Board**. The app compares those scans with the saved setup photo as well as the empty-board calibration. One scan action takes at least two independent photos and uses agreement between them to verify square occupancy, with a third photo if needed. Squares that disagree remain uncertain, and an affected uncertain square prevents a move from being committed. Captures also require visible change at the destination in two photos, so removing the source piece alone does not verify a capture. The saved orientation is used for photo previews, corner taps, and highlights. Illegal moves and multiple-piece changes appear in a large red panel. Choose a promotion when prompted, move Stockfish's piece, then tap **Verify Computer Move**. **End Game** asks for confirmation before resigning.

When a king is in check, a red alert names its square and each attacking piece. The drawn board highlights the king and outlines the attackers. Follow the instructions to protect your king, then scan your move. Double check asks you to move the king. Checkmate ends play and displays **Good game**, the winner, and a **New Game** button. A computer mating move must be completed on the physical board and verified first. **New Game** returns to difficulty selection; starting again requires arranging all 32 pieces and scanning the starting board.

The improved scanner registers small alignment errors against the board grid, corrects exposure gain and smooth local lighting changes, and measures supported foreground detail inside each square. Larger camera shifts or an unreadable grid are rejected. USB capture waits for a stable burst and selects a sharp frame; Pi capture retries once with a longer warm-up if the first attempt fails. All cameras must write a fresh readable image before a capture can replace an old file. Touchscreen scans reuse `calibration/captures/latest.jpg` so photos do not accumulate each turn; a camera failure appears in a large panel with a retry button. Keep the camera mounted and remove hands during scans. Strong glare, occlusion, blur, or pieces that visually blend into the board can still require intervention. This is square-occupancy detection, not piece-type recognition.

Take a **new empty and setup photo pair after updating** to enable the new scoring method. Earlier calibration files remain readable by the diagnostic tools, while the touchscreen asks for the new calibration. Reuse the new saved setup while the board and camera remain fixed. Each scan writes `calibration/last_scan.jpg` and `calibration/last_scan.json` with scores, uncertainty, alignment quality, corners, and the expected position for troubleshooting.

To copy this update from your Mac to the Pi, transfer all three runtime files:

```sh
rsync -av pi_touch.py board_vision.py pi_camera.py smart@chessboard.local:/home/smart/chess-board-pi-only/
ssh smart@chessboard.local 'sudo reboot'
```

### Open the touch screen automatically when the Pi starts

On the Pi, use Raspberry Pi OS **Desktop** with the **labwc** window manager. In **Preferences → Control Centre → System**, set **Boot** to **To desktop** and turn on **Desktop Auto Login**. In the **Display** tab, turn off **Screen Blanking** if the menu should stay visible while idle. Raspberry Pi's [configuration guide](https://www.raspberrypi.com/documentation/computers/configuration.html) describes these settings. Test `pi_touch.py` manually first, then from this project directory run:

```sh
.venv/bin/python scripts/install_touch_autostart.py
sudo reboot
```

After the desktop loads, the welcome screen opens full screen on the connected display. For a USB camera, install with `.venv/bin/python scripts/install_touch_autostart.py --camera usb --device 0` (change the device number if necessary). The installer adds one entry to `~/.config/labwc/autostart`, preserving any other entries. It follows Raspberry Pi's [labwc autostart guidance](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/). If the app does not open, check `~/.local/state/chess-board-pi-only/touchscreen.log`.

To stop automatic launch, run `.venv/bin/python scripts/install_touch_autostart.py --remove` on the Pi and reboot. This autostart setup opens the app after the desktop loads; it does not replace the Pi's early boot splash screen.

The `vision_cli.py` commands provide offline calibration, image inspection, and debugging with saved photos. Run `.venv/bin/python vision_cli.py --help` for options.
