"""Checks for the Python simulator's rules and local Stockfish integration."""

import shutil
import unittest

import chess

from chess_game import ChessGame, DIFFICULTIES, PromotionRequired, StockfishPlayer


class ChessGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.game = ChessGame()
        self.game.start("beginner")

    def test_illegal_move_keeps_accepted_state(self) -> None:
        before = self.game.board.fen()
        with self.assertRaises(ValueError):
            self.game.play_human("e2", "e5")
        self.assertEqual(self.game.board.fen(), before)
        self.assertEqual(self.game.san_moves, [])

    def test_castling(self) -> None:
        self.game.board.set_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        self.assertEqual(self.game.play_human("e1", "g1"), "O-O")
        self.assertEqual(self.game.board.piece_at(chess.G1), chess.Piece(chess.KING, chess.WHITE))
        self.assertEqual(self.game.board.piece_at(chess.F1), chess.Piece(chess.ROOK, chess.WHITE))

    def test_en_passant(self) -> None:
        self.game.board.set_fen("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
        self.game.play_human("e5", "d6")
        self.assertIsNone(self.game.board.piece_at(chess.D5))
        self.assertEqual(self.game.board.piece_at(chess.D6), chess.Piece(chess.PAWN, chess.WHITE))

    def test_promotion_requires_choice(self) -> None:
        self.game.board.set_fen("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        before = self.game.board.fen()
        with self.assertRaises(PromotionRequired):
            self.game.play_human("a7", "a8")
        self.assertEqual(self.game.board.fen(), before)
        self.game.play_human("a7", "a8", "q")
        self.assertEqual(self.game.board.piece_at(chess.A8), chess.Piece(chess.QUEEN, chess.WHITE))


@unittest.skipUnless(shutil.which("stockfish"), "Install Stockfish to run the engine integration check")
class StockfishTests(unittest.TestCase):
    def test_every_level_returns_a_legal_move(self) -> None:
        player = StockfishPlayer()
        try:
            for level in DIFFICULTIES:
                with self.subTest(level=level):
                    game = ChessGame()
                    game.start(level)
                    game.play_human("e2", "e4")
                    move = player.choose_move(game.board.copy(stack=True), level)
                    self.assertIn(move, game.board.legal_moves)
                    game.play_computer(move)
                    self.assertEqual(len(game.san_moves), 2)
        finally:
            player.close()


if __name__ == "__main__":
    unittest.main()
