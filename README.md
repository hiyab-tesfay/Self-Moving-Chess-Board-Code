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

The simulator covers the computer mode from the project breakdown. Motor control, sensors, and remote-player mode are separate integrations. Stockfish is a compiled engine, so the executable is the one non-Python component required to play against Stockfish.

## Web frontend

After installing the Python dependencies and Stockfish above, run:

```sh
.venv/bin/python frontend/server.py
```

Open http://127.0.0.1:8000. To serve on a Raspberry Pi's local network, run
`.venv/bin/python frontend/server.py --host 0.0.0.0` and open
http://chessboard.local:8000 from a device on the same network.
The web interface includes both piece styles and connects to the Python backend
for Stockfish games. Keep this local development server on a trusted network.

## Folder layout

- `backend/`: shared Python chess rules and Stockfish integration.
- `desktop/`: Tkinter desktop interface (`python -m desktop.simulator`).
- `frontend/`: browser interface, assets, and its Python HTTP/API server.
- `frontend/test/`: JavaScript move-generation and chess rules checks.
- `test/`: Python rules and Stockfish integration checks.
- `CAD Files/`: existing mechanical-design files.

The root `chess_game.py` and `simulator.py` remain as compatibility entry points
so existing imports and the original launch command continue to work.
The separate `chess-vision` branch is not part of this frontend merge.

Run the browser rules checks with Node.js:

```sh
node frontend/test/perft.js
```
