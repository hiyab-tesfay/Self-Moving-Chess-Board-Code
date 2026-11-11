"""Stockfish for the gantry terminal: finds a local Stockfish and picks moves at a chosen strength.

Same levels as the board app (backend/chess_game.py): beginner, intermediate, advanced.
Never uses a network service. On the Pi: sudo apt install stockfish (it lands in /usr/games/stockfish).
"""

from __future__ import annotations

import os
import shutil

import chess
import chess.engine

LEVELS = {                     # name: (Skill Level 0-20, seconds to think)
    "beginner": (0, 0.15),
    "intermediate": (8, 0.50),
    "advanced": (20, 1.40),
}


def find_stockfish() -> str:
    configured = os.environ.get("STOCKFISH_PATH")
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise FileNotFoundError(f"STOCKFISH_PATH is not an executable file: {configured}")
    found = shutil.which("stockfish")
    if found:
        return found
    for candidate in ("/usr/games/stockfish", "/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("Stockfish was not found. On the Pi: sudo apt install stockfish (or set STOCKFISH_PATH).")


class Engine:
    def __init__(self, level: str = "intermediate", executable: str | None = None) -> None:
        if level not in LEVELS:
            raise ValueError(f"level is one of {', '.join(LEVELS)}")
        self.level = level
        self.engine = chess.engine.SimpleEngine.popen_uci(executable or find_stockfish())

    def choose(self, board: chess.Board) -> chess.Move:
        skill, think = LEVELS[self.level]
        if "Skill Level" in self.engine.options:
            self.engine.configure({"Skill Level": skill})
        result = self.engine.play(board, chess.engine.Limit(time=think), info=chess.engine.INFO_NONE)
        if result.move is None or result.move not in board.legal_moves:
            raise RuntimeError("Stockfish returned no legal move.")
        return result.move

    def close(self) -> None:
        try:
            self.engine.quit()
        except Exception:  # noqa: BLE001
            pass
