"""Occupancy-only move inference, including cases that must stay ambiguous."""

import unittest

import chess

from move_observation import match_legal_moves, match_scored_moves, occupied_squares


def after(board: chess.Board, uci: str) -> frozenset[str]:
    copy = board.copy(stack=False)
    copy.push_uci(uci)
    return occupied_squares(copy)


class MoveObservationTests(unittest.TestCase):
    def test_starting_move_and_unchanged_photo(self) -> None:
        board = chess.Board()
        fen = board.fen()
        self.assertEqual(match_legal_moves(board, occupied_squares(board)).status, "unchanged")
        result = match_legal_moves(board, after(board, "e2e4"))
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.candidates[0].uci(), "e2e4")
        self.assertEqual(board.fen(), fen)

    def test_castling_and_en_passant(self) -> None:
        for fen, uci in (
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
        ):
            with self.subTest(uci=uci):
                board = chess.Board(fen)
                result = match_legal_moves(board, after(board, uci))
                self.assertEqual(result.status, "matched")
                self.assertEqual(result.candidates[0].uci(), uci)

    def test_promotion_needs_a_choice(self) -> None:
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        result = match_legal_moves(board, after(board, "a7a8q"))
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({move.uci() for move in result.candidates},
                         {"a7a8q", "a7a8r", "a7a8b", "a7a8n"})

    def test_captures_can_share_the_same_occupancy(self) -> None:
        board = chess.Board("4k3/8/3p1p2/4P3/8/8/8/4K3 w - - 0 1")
        result = match_legal_moves(board, after(board, "e5d6"))
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({move.uci() for move in result.candidates}, {"e5d6", "e5f6"})

    def test_scored_promotion_is_still_ambiguous(self) -> None:
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        observed = after(board, "a7a8q")
        scores = {square: 100.0 if square in observed else 0.0 for square in chess.SQUARE_NAMES}
        result = match_scored_moves(board, scores, (50.0, 50.0))
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({move.uci() for move in result.candidates},
                         {"a7a8q", "a7a8r", "a7a8b", "a7a8n"})

    def test_illegal_pattern_has_no_match(self) -> None:
        board = chess.Board()
        observed = set(occupied_squares(board))
        observed.remove("e2")
        observed.add("e5")
        self.assertEqual(match_legal_moves(board, observed).status, "no_match")


if __name__ == "__main__":
    unittest.main()
