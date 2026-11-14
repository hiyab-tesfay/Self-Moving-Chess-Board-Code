"""Local Stockfish Black for the original gantry dashboard; White is moved by hand."""
from pathlib import Path
import sys

import chess


class BlackBot:
    def __init__(self, console, player=None):
        self.console = console
        if player is None:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from backend.chess_game import StockfishPlayer
            player = StockfishPlayer()
        self.player = player
        self.bounds = player.rating_range
        if self.bounds is None:
            player.close()
            raise RuntimeError("This Stockfish build needs UCI_Elo support for the rating slider.")
        self.rating = max(self.bounds[0], min(self.bounds[1], 1500))
        self.enabled = True
        self.error = ""

    def view(self):
        return {"enabled": self.enabled, "rating": self.rating, "min": self.bounds[0], "max": self.bounds[1],
                "error": self.error, "human": "white", "computer": "black"}

    def configure(self, values):
        c = self.console
        with c.lock:
            if c.task or c.pending or c.preview:
                raise ValueError("Finish or cancel the current operation before changing the bot.")
            enabled = values.get("enabled", self.enabled)
            rating = values.get("rating", self.rating)
            if type(enabled) is not bool or type(rating) is not int or not self.bounds[0] <= rating <= self.bounds[1]:
                raise ValueError("Choose a supported bot rating.")
            self.enabled, self.rating, self.error = enabled, rating, ""
        return self.view()

    def reply(self):
        c = self.console
        with c.lock:
            if not self.enabled or c.game.turn != chess.BLACK or c.game.is_game_over():
                return
            if c.task or c.pending or c.preview:
                raise ValueError("Finish the current gantry operation before requesting Black's move.")
            c.require(homed=True)
            board = c.game.copy(stack=True)
            rating = self.rating
            self.error = ""
            c.start("Black bot is thinking", self._reply, board, rating)

    def _reply(self, board, rating):
        c = self.console
        try:
            move = self.player.choose_rated_move(board, rating)
            c.check_stop()
            with c.lock:
                if not self.enabled or c.game.fen() != board.fen():
                    raise ValueError("The position changed while Black was thinking; no move was sent.")
                preview = c.plan_move(chess.square_name(move.from_square), chess.square_name(move.to_square),
                                      chess.piece_symbol(move.promotion) if move.promotion else None,
                                      "chess", bot_move=True)
                if preview.get("manual_only"):
                    raise ValueError("Black's move must be carried by the gantry.")
            c.check_stop()
            c.progress(f"Black plays {preview['san']}")
            c._run_job(preview, 0)
            c.say("info", "Your turn: move White by hand, then record FROM and TO on the board.")
        except Exception as exc:
            self.error = str(exc) or "Black's move stopped. Check the gantry and use the recovery controls."
            raise

    def close(self):
        self.player.close()
