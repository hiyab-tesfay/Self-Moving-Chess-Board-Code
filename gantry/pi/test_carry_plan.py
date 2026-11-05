"""Captures to the dock first, castling as two carries, en passant, promotion from the dock, and the reset."""
from pathlib import Path
import sys
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parent))
import carry_plan as cp  # noqa: E402


def play(uci_moves):
    board, layout = chess.Board(), cp.Layout()
    jobs = []
    for uci in uci_moves:
        move = chess.Move.from_uci(uci)
        jobs.append(cp.job_for_move(layout, board, move))
        board.push(move)
    return board, layout, jobs


def moves(job):
    return [(s["piece"], s["from"], s["to"]) for s in job["steps"] if s["op"] == "carry"]


class PlanTests(unittest.TestCase):
    def test_a_plain_move_is_one_carry_with_the_board_before_it(self):
        board, layout, (job,) = play(["e2e4"])
        self.assertEqual(moves(job), [("P", "e2", "e4")])
        self.assertEqual(len(job["before"]), 32)
        self.assertEqual(job["pieces"]["e2"], "P")
        self.assertIn("e4", job["expect"])
        self.assertNotIn("e2", job["expect"])

    def test_a_capture_docks_the_taken_piece_before_the_taker_moves(self):
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5"])
        self.assertEqual(moves(jobs[2]), [("p", "d5", "b:0"), ("P", "e4", "d5")], "the black pawn goes to Black's lane first")
        self.assertEqual(layout.pieces["b:0"], "p")
        self.assertNotIn("b:0", jobs[2]["expect"], "docked pieces are not on the board")
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5", "d8d5"])
        self.assertEqual(moves(jobs[3]), [("P", "d5", "w:0"), ("q", "d8", "d5")], "a white piece goes to White's lane")

    def test_en_passant_docks_the_pawn_that_was_passed(self):
        board, layout, jobs = play(["e2e4", "a7a6", "e4e5", "d7d5", "e5d6"])
        self.assertEqual(moves(jobs[4]), [("p", "d5", "b:0"), ("P", "e5", "d6")])

    def test_castling_carries_the_king_then_the_rook(self):
        board, layout, jobs = play(["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "e1g1"])
        self.assertEqual(moves(jobs[6]), [("K", "e1", "g1"), ("R", "h1", "f1")])
        board, layout, jobs = play(["d2d4", "d7d5", "b1c3", "b8c6", "c1f4", "c8f5", "d1d2", "d8d7", "e1c1"])
        self.assertEqual(moves(jobs[8]), [("K", "e1", "c1"), ("R", "a1", "d1")], "queenside too")

    def test_promotion_uses_a_docked_piece_of_that_type_when_there_is_one(self):
        layout = cp.Layout()
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        layout.pieces = {"a7": "P", "e1": "K", "e8": "k", "w:0": "Q"}   # a white queen was captured earlier and docked
        job = cp.job_for_move(layout, board, chess.Move.from_uci("a7a8q"))
        self.assertEqual(moves(job), [("P", "a7", "w:1"), ("Q", "w:0", "a8")])
        self.assertEqual(layout.pieces["a8"], "Q")
        layout.pieces = {"a7": "P", "e1": "K", "e8": "k"}
        job = cp.job_for_move(layout, board, chess.Move.from_uci("a7a8q"))
        self.assertEqual(moves(job), [("P", "a7", "a8")])
        self.assertEqual(job["steps"][-1]["op"], "note")

    def test_the_persons_own_moves_update_the_layout_without_carries(self):
        board, layout = chess.Board(), cp.Layout()
        layout.apply_human(board, chess.Move.from_uci("e2e4")); board.push_uci("e2e4")
        self.assertEqual(layout.pieces["e4"], "P")
        board.push_uci("d7d5")
        layout.pieces["d5"] = layout.pieces.pop("d7")
        layout.apply_human(board, chess.Move.from_uci("e4d5")); board.push_uci("e4d5")
        self.assertEqual(layout.pieces["d5"], "P")
        self.assertNotIn("b:0", layout.pieces, "a piece the person took is simply gone")
        self.assertEqual(len([n for n in layout.pieces if ":" in n]), 0)

    def test_twelve_slots_per_lane_then_the_second_lane(self):
        layout = cp.Layout()
        for index in range(12):
            layout.pieces[f"b:{index}"] = "p"
        self.assertEqual(layout.free_slot("p"), "b:12")
        self.assertEqual(cp._xy("b:12"), [9.0, 0.0], "the second lane")
        self.assertEqual(cp._xy("w:11"), [-1.0, 11.0])

    def test_reset_docks_strays_and_brings_pieces_home(self):
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5"])
        job = cp.job_for_reset(layout)
        froms = [s["from"] for s in job["steps"] if s["op"] == "carry"]
        self.assertIn("d5", froms, "the white pawn on d5 is docked, then pawns come home")
        self.assertEqual(sorted(n for n in layout.pieces if ":" in n), [], "every docked piece is back")
        self.assertEqual(layout.on_board(), cp.Layout().on_board())
        self.assertEqual(job["expect"], cp.Layout().on_board())

    def test_describe_reads_naturally(self):
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5"])
        self.assertEqual(cp.describe(jobs[2]), ["Take the black pawn off d5 (slot b:0).", "Move the white pawn from e4 to d5."])


if __name__ == "__main__":
    unittest.main()
