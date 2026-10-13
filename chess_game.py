"""Chess rules and Stockfish integration for the Python simulator."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.engine


@dataclass(frozen=True)
class Difficulty:
    label: str
    skill: int
    think_seconds: float
    description: str


DIFFICULTIES = {
    "beginner": Difficulty("Beginner", 0, 0.15, "A gentle first opponent"),
    "intermediate": Difficulty("Intermediate", 8, 0.50, "Plans a few moves ahead"),
    "advanced": Difficulty("Advanced", 20, 1.40, "A serious challenge"),
}


class PromotionRequired(ValueError):
    """The player must choose a promotion piece before this move is accepted."""


def find_stockfish() -> str:
    """Find a local Stockfish executable; never use a network service."""
    configured = os.environ.get("STOCKFISH_PATH")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise FileNotFoundError(f"STOCKFISH_PATH is not an executable file: {path}")

    found = shutil.which("stockfish")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish", "/usr/games/stockfish"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(
        "Stockfish was not found. Install it (macOS: brew install stockfish; "
        "Ubuntu/Raspberry Pi OS: sudo apt install stockfish), or set STOCKFISH_PATH."
    )


class StockfishPlayer:
    def __init__(self, executable: str | None = None) -> None:
        self.engine = chess.engine.SimpleEngine.popen_uci(executable or find_stockfish())
        if "Skill Level" not in self.engine.options:
            self.engine.quit()
            raise RuntimeError("This Stockfish build does not support the Skill Level option.")

    def choose_move(self, board: chess.Board, level: str) -> chess.Move:
        settings = DIFFICULTIES[level]
        self.engine.configure({"Skill Level": settings.skill})
        result = self.engine.play(
            board,
            chess.engine.Limit(time=settings.think_seconds),
            info=chess.engine.INFO_NONE,
        )
        if result.move is None or result.move not in board.legal_moves:
            raise RuntimeError("Stockfish returned no legal move.")
        return result.move

    def close(self) -> None:
        self.engine.quit()


@dataclass
class ChessGame:
    board: chess.Board = field(default_factory=chess.Board)
    level: str = "beginner"
    started: bool = False
    san_moves: list[str] = field(default_factory=list)
    last_move: chess.Move | None = None
    revision: int = 0

    def start(self, level: str) -> None:
        if level not in DIFFICULTIES:
            raise ValueError(f"Unknown difficulty: {level}")
        self.board.reset()
        self.level = level
        self.started = True
        self.san_moves.clear()
        self.last_move = None
        self.revision += 1

    def legal_destinations(self, square_name: str) -> set[str]:
        if not self.started or self.board.turn != chess.WHITE or self.board.is_game_over(claim_draw=True):
            return set()
        try:
            square = chess.parse_square(square_name)
        except ValueError:
            return set()
        piece = self.board.piece_at(square)
        if piece is None or piece.color != chess.WHITE:
            return set()
        return {chess.square_name(move.to_square) for move in self.board.legal_moves if move.from_square == square}

    def play_human(self, source: str, target: str, promotion: str | None = None) -> str:
        if not self.started or self.board.turn != chess.WHITE or self.board.is_game_over(claim_draw=True):
            raise ValueError("It is not your turn.")
        try:
            from_square = chess.parse_square(source)
            to_square = chess.parse_square(target)
        except ValueError as exc:
            raise ValueError("Choose squares on the board.") from exc

        piece = self.board.piece_at(from_square)
        if piece is None or piece.color != chess.WHITE:
            raise ValueError("Choose one of your pieces.")
        if piece.piece_type == chess.PAWN and chess.square_rank(to_square) == 7 and promotion is None:
            if any(move.from_square == from_square and move.to_square == to_square for move in self.board.legal_moves):
                raise PromotionRequired("Choose a piece for your pawn.")

        promotion_piece = {"q": chess.QUEEN, "r": chess.ROOK, "b": chess.BISHOP, "n": chess.KNIGHT}.get(promotion)
        if promotion is not None and promotion_piece is None:
            raise ValueError("Promotion must be queen, rook, bishop, or knight.")
        move = chess.Move(from_square, to_square, promotion=promotion_piece)
        if move not in self.board.legal_moves:
            raise ValueError("That move is not legal. The board has not changed.")
        return self._push(move)

    def play_computer(self, move: chess.Move) -> str:
        if not self.started or self.board.turn != chess.BLACK or self.board.is_game_over(claim_draw=True):
            raise ValueError("It is not Stockfish's turn.")
        if move not in self.board.legal_moves:
            raise ValueError("Stockfish returned an illegal move. The board has not changed.")
        return self._push(move)

    def _push(self, move: chess.Move) -> str:
        san = self.board.san(move)
        self.board.push(move)
        self.san_moves.append(san)
        self.last_move = move
        self.revision += 1
        return san

    def result_text(self) -> str | None:
        outcome = self.board.outcome(claim_draw=True)
        if outcome is None:
            return None
        if outcome.winner is chess.WHITE:
            return "You win by checkmate!"
        if outcome.winner is chess.BLACK:
            return "Stockfish wins by checkmate."
        reasons = {
            chess.Termination.STALEMATE: "Draw by stalemate.",
            chess.Termination.INSUFFICIENT_MATERIAL: "Draw: insufficient material.",
            chess.Termination.THREEFOLD_REPETITION: "Draw by repetition.",
            chess.Termination.FIVEFOLD_REPETITION: "Draw by repetition.",
            chess.Termination.FIFTY_MOVES: "Draw by the fifty-move rule.",
            chess.Termination.SEVENTYFIVE_MOVES: "Draw by the seventy-five-move rule.",
        }
        return reasons.get(outcome.termination, "Game drawn.")
