# Chessboard Lab — Python Stockfish simulator

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

## Test chessboard vision from saved photos

There are two stages to test offline. `inspect` reports likely occupied/empty square changes between two photos, even when the pieces were moved at random; it does not judge chess legality. Later, `compare` can match a photo to a legal move in a real game. An optional `calibrate`/`scan` path uses an empty-board reference. All paths need photos of the same physical board. Install the requirements above; OpenCV supplies image alignment and marker detection, while `python-chess` supplies legal moves. No trained model is used.

### Try the supplied phone photos

The three supplied HEIC files were converted to ignored local JPEGs at `photos/IMG_1524.jpg`, `photos/IMG_1525.jpg`, and `photos/IMG_1527.jpg`. They show random position changes, so use `inspect`. The board moves within the frame, so each photo has its own manually measured corners. In the phone view White is on the left; `--rotate ccw` puts square **a8 at the top left** for chess notation. The first photo is the known starting setup:

```sh
.venv/bin/python vision_cli.py inspect \
  --before photos/IMG_1524.jpg --after photos/IMG_1525.jpg \
  --before-corners 151 276 1107 279 1102 1225 144 1225 \
  --after-corners 211 339 1125 354 1117 1225 209 1227 \
  --rotate ccw --debug-after output/1525_changes.png

.venv/bin/python vision_cli.py inspect \
  --before photos/IMG_1524.jpg --after photos/IMG_1527.jpg \
  --before-corners 151 276 1107 279 1102 1225 144 1225 \
  --after-corners 155 340 1044 350 1047 1228 151 1227 \
  --rotate ccw --debug-after output/1527_changes.png
```

The first pair reports **e2 vacated, e3 filled**; the second reports **f8 vacated, c5 filled**. This is square-change detection only. The debug images mark vacated squares red, filled squares green, and weaker threshold crossings orange. In these photos, h1 and h6 are orange because the camera angle and shadows changed; `inspect` does not count them as confident changes. A large score drop lets it flag f8 as likely vacated even though its remaining shadow makes the square look dark.

For a new pair of photos, pass their own corner coordinates in **top-left, top-right, bottom-right, bottom-left** order. If the before photo is not the standard setup, use `--fen` or `--moves` to give its known occupancy. If four printed markers are installed, omit both corner options; the software will locate the board separately in each photo. A standard phone HEIC can be converted on macOS with `sips -s format jpeg input.HEIC --out output.jpg`. Keep the camera and lighting as steady as possible; these sample photos have enough movement and shadows to produce a few low-confidence squares.

When you later capture consecutive moves from a real game, use `compare` with the same photo and corner options to ask which **legal** move matches the observed occupancy. It does not update a saved game on an uncertain or unmatched result.

### Empty-board reference path

For the planned fixed camera setup, take three photos: (1) completely empty board, (2) all 32 pieces in the standard starting position, and (3) after a known move. Keep focus, exposure, and lighting fixed. Generic chess photos from different boards or viewpoints cannot be compared to one empty-board reference. You can mark the four playing-area corners manually in pixel coordinates; with `--corners` the same points are used for every image, so this mode assumes the camera and board have not moved:

```sh
.venv/bin/python vision_cli.py calibrate \
  --empty photos/empty.jpg --starting photos/start.jpg \
  --corners 120 95 920 100 925 900 115 895 \
  --out calibration

.venv/bin/python vision_cli.py verify \
  --calibration calibration --image photos/start.jpg \
  --debug output/start_debug.png

.venv/bin/python vision_cli.py scan \
  --calibration calibration --image photos/after_e2e4.jpg \
  --debug output/e2e4_debug.png
```

Replace the example coordinates with the actual **outer corners of the 8×8 playing area** in your images. Calibration saves the straightened empty reference in `calibration/empty_warp.png` and learned thresholds in `calibration/calibration.json`. The debug image draws each classified square: green = occupied, gray = empty, orange = uncertain. The terminal prints an 8×8 occupancy grid, then the matching move and resulting FEN. An uncertain square, a mismatch, or multiple legal matches stops automatic acceptance. Promotion usually has four possible matches and needs a human choice.

For a camera that may shift between photos, create four printable markers instead of passing `--corners`:

```sh
.venv/bin/python vision_cli.py markers --out markers
.venv/bin/python vision_cli.py calibrate \
  --empty photos/empty.jpg --starting photos/start.jpg --out calibration
```

Print markers 0, 1, 2, 3 **upright** at the top-left, top-right, bottom-right, bottom-left of the board respectively. Put each sticker outside the playing area and align its nearest **outer white sticker corner** with the corresponding outer board corner. Preserve the white margin when cutting the stickers; the software uses its known width for alignment. Keep all four visible in every photo. The software detects them afresh in each image and straightens the board. If calibration reports missing markers or poor score separation, inspect the images and retake them before using move detection.

To scan a later move, give the position *before* that photo. Use a FEN or a sequence of UCI moves from the start:

```sh
.venv/bin/python vision_cli.py scan \
  --calibration calibration --image photos/after_e7e5.jpg \
  --moves e2e4

.venv/bin/python vision_cli.py verify \
  --calibration calibration --image photos/after_e7e5.jpg \
  --moves e2e4 e7e5
```

`scan` compares the photo with every legal next position. `verify` checks that a known position matches the physical board, which is the check to run after the board moves a piece. Neither command changes a saved game; it reports a move or mismatch so the eventual Raspberry Pi controller can decide whether to commit it.

The simulator covers the computer mode from the project breakdown. Motor control, sensors, and remote-player mode are separate integrations. Stockfish is a compiled engine, so the executable is the one non-Python component required to play against Stockfish.
