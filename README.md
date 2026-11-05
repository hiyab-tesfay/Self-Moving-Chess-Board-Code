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

Connect the LCD touch screen and make sure Raspberry Pi OS shows its desktop on that display and touch input works. Complete `setup`, `check`, and `scan` above before starting a game. From a desktop terminal, launch the full-screen touch interface:

```sh
.venv/bin/python pi_touch.py
```

For a USB camera, use `--camera usb` (and `--device 1` if needed). For development in a normal window, add `--windowed`. If calibration used manually specified corners rather than visible ArUco markers or saved fixed corners, pass `--corners TL_X TL_Y TR_X TR_Y BR_X BR_Y BL_X BL_Y` as with `pi_game.py`.

Select Beginner, Intermediate, or Advanced, then tap **Start Game**. Place the pieces in the starting position and tap **Scan Starting Board**. Move White on the physical board and tap **Scan Move**. A legal move appears in green; an unchanged, uncertain, or unmatched board asks you to correct it and scan again. Tap a white piece on the displayed board to see its legal destination squares. If several legal moves look identical to the camera, choose the actual move on screen; this includes choosing Queen, Rook, Bishop, or Knight for promotion. Follow Stockfish's displayed move on the physical board, then tap **Verify Computer Move**. **End Game** records a resignation, and the result screen offers **New Game**. Press Escape to exit the app when using a keyboard.

The camera observes occupied squares, not piece identities. It cannot independently prove which piece was placed on a square or distinguish promotions and some captures from the image alone. Keep the physical pieces in sync with the displayed board and use the on-screen choice when prompted. The real LCD, touch coordinates, camera, board, and lighting still require a hardware test.

### The gantry plays Black's move

When the self-moving gantry is connected, the touch screen plays Stockfish's move with it instead of asking you to move the piece: after your move is read by the camera, the screen shows "Gantry moving e7e5… keep hands clear", the gantry carries the piece (castling moves the king, then the rook), and then the **same camera check** runs as for a move made by hand, so every piece position is verified after every move, the gantry's included. The gantry never takes a captured piece off the board: when Stockfish captures, the screen shows "Remove the piece" with the square in red, you take the piece off and tap **Check**, the camera confirms the square is empty (every tap re-checks until it is), and only then does the gantry move. If the gantry is not connected or fails, the screen falls back to "move it by hand, then tap Verify Computer Move". When a game ends, is abandoned, or **Reset Board** is tapped twice, the gantry carries every piece still on the board back to its starting square, lists the pieces you took during the game ("Put a black pawn on e7."), and the camera checks the starting position before the next game. The logic lives in `pi_gantry.py`, which uses the driver on the `gantry` branch of this repository; configure it with environment variables:

```sh
CHESS_GANTRY_DIR=/home/smart/chess-gantry/gantry/pi   # a checkout of the gantry branch (default: ~/chess-gantry/gantry/pi or ~/Self-Moving-Chess-Board-Code/gantry/pi)
CHESS_GANTRY_PORT=/dev/ttyUSB0                        # the ESP32; "sim" runs the pretend board; unset = no gantry
```

### Online: the website plays on this board

The fourth menu option, **○ Online**, lets a player on the website (kennynakao/chess-website, served by the `duen-relay` service on this Pi) play against Stockfish on this physical board. Tapping it photographs the board and requires the full starting position (the screen names any missing or extra pieces; fix them and tap **Check Again**), then shows the website address and the board's access code (`CHESS_ACCESS_CODE` in `~/.config/duen-chess/relay.env`) until a player connects and starts a game. While Online is selected the screen heartbeats the game server every second; without it the website refuses to play. The first heartbeat tells the server the board is fresh, so an old unfinished game is never shown. During the game the screen mirrors the website's game and the gantry/camera pipeline; **End Game** (tapped twice) ends it from the board, and the pieces go home. When a game ends for any reason, including the website's clock running out, the gantry resets the board, the camera checks it, and the screen returns to the address and code with the last result. The logic lives in `pi_online.py`; the website server drives the gantry itself while Online is selected, so the touch app closes its own connection to it then.

### Open the touch screen automatically when the Pi starts

On the Pi, use Raspberry Pi OS **Desktop** with the **labwc** window manager. In **Preferences → Control Centre → System**, set **Boot** to **To desktop** and turn on **Desktop Auto Login**. In the **Display** tab, turn off **Screen Blanking** if the menu should stay visible while idle. Raspberry Pi's [configuration guide](https://www.raspberrypi.com/documentation/computers/configuration.html) describes these settings. Test `pi_touch.py` manually first, then from this project directory run:

```sh
.venv/bin/python scripts/install_touch_autostart.py
sudo reboot
```

After the desktop loads, the chess menu opens full screen on the connected display. For a USB camera, install with `.venv/bin/python scripts/install_touch_autostart.py --camera usb --device 0` (change the device number if necessary). The installer adds one entry to `~/.config/labwc/autostart`, preserving any other entries. It follows Raspberry Pi's [labwc autostart guidance](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/). If the app does not open, check `~/.local/state/chess-board-pi-only/touchscreen.log`.

To stop automatic launch, run `.venv/bin/python scripts/install_touch_autostart.py --remove` on the Pi and reboot. This autostart setup opens the app after the desktop loads; it does not replace the Pi's early boot splash screen.

The `vision_cli.py` commands provide offline calibration, image inspection, and debugging with saved photos. Run `.venv/bin/python vision_cli.py --help` for options.
