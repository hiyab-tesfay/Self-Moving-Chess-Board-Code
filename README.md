# Chessboard Lab — desktop, web, and automatic chessboard

Play against Stockfish at three difficulty levels in a desktop chessboard simulator. The application code and interface are written in Python with Tkinter. It uses the pure-Python `chess` package for legal moves and a local Stockfish executable for computer moves. Playing does not need an internet connection.

## Setup

You need Python 3.10+ with Tkinter and a Stockfish executable. On macOS, install Stockfish with `brew install stockfish`. On Ubuntu or Raspberry Pi OS, use `sudo apt install stockfish`. If Stockfish is somewhere else, set `STOCKFISH_PATH` to its executable path.

Create a virtual environment and install the Python chess rules library:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python simulator.py
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`. Install the Stockfish executable for Windows and set `STOCKFISH_PATH` to its full path. Dependencies and Stockfish need to be installed once; the simulator then runs offline.

Choose Beginner, Intermediate, or Advanced and select **Start game**. You play White. Click a white piece and then a highlighted square. Promotion opens a piece picker. Changing the selected level applies when you select **New game**.

## Difficulty

| Level | Stockfish `Skill Level` | Thinking time per move |
| --- | ---: | ---: |
| Beginner | 0 | 0.15 seconds |
| Intermediate | 8 | 0.50 seconds |
| Advanced | 20 | 1.40 seconds |

These are relative settings, not Elo guarantees. The values are in `chess_game.py`. Stockfish uses the [UCI engine protocol](https://python-chess.readthedocs.io/en/stable/engine.html) through `python-chess`.

## Verify

```sh
.venv/bin/python -m unittest discover -s test -p 'test_*.py'
```

The simulator covers the computer mode from the project breakdown. Motor control, sensors, and remote-player mode are separate integrations. Stockfish is a compiled engine, so the executable is the one non-Python component required to play against Stockfish.

## Web frontend

After installing the Python dependencies and Stockfish above, run:

```sh
.venv/bin/python frontend/server.py
```

Open http://127.0.0.1:8000 for the animated landing page, then choose
**Play chess** (or open http://127.0.0.1:8000/play.html directly).
To serve on a Raspberry Pi's local network, run
`.venv/bin/python frontend/server.py --host 0.0.0.0` and open
http://chessboard.local:8000 from a device on the same network.
The web interface includes both piece styles and connects to the Python backend
for Stockfish games. Keep this local development server on a trusted network.

## Folder layout

- `backend/`: shared Python chess rules and Stockfish integration.
- `desktop/`: Tkinter desktop interface (`python -m desktop.simulator`).
- `frontend/`: landing page, browser game, assets, and its Python HTTP/API server.
  `index.html` opens the landing page; `play.html` opens the existing game.
  `assets/vendor/` includes the locally served Three.js runtime and its license.
- `frontend/test/`: JavaScript move-generation and chess rules checks.
- `test/`: Python rules and Stockfish integration checks.
- `CAD Files/`: existing mechanical-design files.

The root `chess_game.py` and `simulator.py` remain as compatibility entry points
so existing imports and the original launch command continue to work.
Raspberry Pi camera, touchscreen, and gantry instructions follow below.

Run the browser rules checks with Node.js:

```sh
node frontend/test/perft.js
```

---

## Raspberry Pi camera and touchscreen

The Pi applications run the camera-based chess game on a Raspberry Pi. You play White against a local Stockfish engine. The camera detects which squares are occupied; `python-chess` tracks piece identities and checks move legality. After Stockfish announces Black's move, move that piece on the physical board and let the camera verify the result.

Camera-only play uses manual piece movement. The gantry dashboard controls the motors separately; the camera does not identify piece types from images. Moves with identical occupancy patterns, including promotions and some captures, require a manual choice. The camera and calibration still need to be tested with your actual board and lighting.

**Gantry (the magnet that moves the pieces):** the code is in `gantry/`. To run the gantry dashboard yourself (connect, home, jog, arm, chess moves, Tune), see **Quick start: the gantry dashboard** in [gantry/README.md](gantry/README.md): `bash gantry/console/console.sh` with the ESP32 on USB, or `bash gantry/console/console.sh sim` without one.

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

### Open the touch screen automatically when the Pi starts

On the Pi, use Raspberry Pi OS **Desktop** with the **labwc** window manager. In **Preferences → Control Centre → System**, set **Boot** to **To desktop** and turn on **Desktop Auto Login**. In the **Display** tab, turn off **Screen Blanking** if the menu should stay visible while idle. Raspberry Pi's [configuration guide](https://www.raspberrypi.com/documentation/computers/configuration.html) describes these settings. Test `pi_touch.py` manually first, then from this project directory run:

```sh
.venv/bin/python scripts/install_touch_autostart.py
sudo reboot
```

After the desktop loads, the chess menu opens full screen on the connected display. For a USB camera, install with `.venv/bin/python scripts/install_touch_autostart.py --camera usb --device 0` (change the device number if necessary). The installer adds one entry to `~/.config/labwc/autostart`, preserving any other entries. It follows Raspberry Pi's [labwc autostart guidance](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/). If the app does not open, check `~/.local/state/chess-board-pi-only/touchscreen.log`.

To stop automatic launch, run `.venv/bin/python scripts/install_touch_autostart.py --remove` on the Pi and reboot. This autostart setup opens the app after the desktop loads; it does not replace the Pi's early boot splash screen.

The `vision_cli.py` commands provide offline calibration, image inspection, and debugging with saved photos. Run `.venv/bin/python vision_cli.py --help` for options.
