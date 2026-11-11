"""A capture is a remove step then one carry, castling is two carries, en passant, the promotion note, and the reset."""
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


def ops(job):
    return [s["op"] for s in job["steps"]]


def slots(layout):
    return [name for name in layout.pieces if ":" in name]


class PlanTests(unittest.TestCase):
    def test_a_plain_move_is_one_carry_with_the_board_before_it(self):
        board, layout, (job,) = play(["e2e4"])
        self.assertEqual(ops(job), ["carry"])
        self.assertEqual(moves(job), [("P", "e2", "e4")])
        self.assertEqual(len(job["before"]), 32)
        self.assertEqual(job["pieces"]["e2"], "P")
        self.assertIn("e4", job["expect"])
        self.assertNotIn("e2", job["expect"])

    def test_a_capture_is_a_remove_step_then_the_takers_carry(self):
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5"])
        self.assertEqual(ops(jobs[2]), ["remove", "carry"], "the person takes the pawn off first")
        self.assertEqual(jobs[2]["steps"][0], {"op": "remove", "piece": "p", "square": "d5"})
        self.assertEqual(moves(jobs[2]), [("P", "e4", "d5")], "one carry: the gantry never docks a piece")
        self.assertEqual(slots(layout), [], "the taken pawn is gone, not in a lane")
        self.assertEqual(layout.pieces["d5"], "P")
        self.assertEqual(len(layout.pieces), 31)
        self.assertEqual(jobs[2]["expect"], layout.on_board())
        self.assertIn("d5", jobs[2]["before"], "the job starts with the pawn still on d5")
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5", "d8d5"])
        self.assertEqual(jobs[3]["steps"][0], {"op": "remove", "piece": "P", "square": "d5"}, "a white piece the same way")
        self.assertEqual(moves(jobs[3]), [("q", "d8", "d5")])

    def test_en_passant_removes_the_pawn_that_was_passed(self):
        board, layout, jobs = play(["e2e4", "a7a6", "e4e5", "d7d5", "e5d6"])
        self.assertEqual(jobs[4]["steps"][0], {"op": "remove", "piece": "p", "square": "d5"})
        self.assertEqual(moves(jobs[4]), [("P", "e5", "d6")])
        self.assertNotIn("d5", layout.pieces)

    def test_castling_carries_the_king_then_the_rook(self):
        board, layout, jobs = play(["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "e1g1"])
        self.assertEqual(moves(jobs[6]), [("K", "e1", "g1"), ("R", "h1", "f1")])
        board, layout, jobs = play(["d2d4", "d7d5", "b1c3", "b8c6", "c1f4", "c8f5", "d1d2", "d8d7", "e1c1"])
        self.assertEqual(moves(jobs[8]), [("K", "e1", "c1"), ("R", "a1", "d1")], "queenside too")

    def test_promotion_carries_the_pawn_and_says_so(self):
        layout = cp.Layout()
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        layout.pieces = {"a7": "P", "e1": "K", "e8": "k"}
        job = cp.job_for_move(layout, board, chess.Move.from_uci("a7a8q"))
        self.assertEqual(moves(job), [("P", "a7", "a8")])
        self.assertEqual(job["steps"][-1], {"op": "note", "message": "The pawn on a8 now counts as a queen; swap it by hand if you have a spare piece."})
        self.assertEqual(layout.pieces["a8"], "P", "physically it is still the pawn")
        board = chess.Board("4k3/1P6/8/8/8/8/8/4K3 w - - 0 1")
        layout.pieces = {"b7": "P", "e1": "K", "e8": "k"}
        job = cp.job_for_move(layout, board, chess.Move.from_uci("b7b8n"))
        self.assertIn("counts as a knight", job["steps"][-1]["message"])

    def test_a_capturing_promotion_removes_first(self):
        board = chess.Board("1n2k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        layout = cp.Layout(board)
        job = cp.job_for_move(layout, board, chess.Move.from_uci("a7b8q"))
        self.assertEqual(ops(job), ["remove", "carry", "note"])
        self.assertEqual(job["steps"][0], {"op": "remove", "piece": "n", "square": "b8"})

    def test_the_persons_own_moves_update_the_layout_without_carries(self):
        board, layout = chess.Board(), cp.Layout()
        layout.apply_human(board, chess.Move.from_uci("e2e4")); board.push_uci("e2e4")
        self.assertEqual(layout.pieces["e4"], "P")
        board.push_uci("d7d5")
        layout.pieces["d5"] = layout.pieces.pop("d7")
        layout.apply_human(board, chess.Move.from_uci("e4d5")); board.push_uci("e4d5")
        self.assertEqual(layout.pieces["d5"], "P")
        self.assertEqual(len(layout.pieces), 31, "a piece the person took is simply gone")
        self.assertEqual(slots(layout), [])

    def test_removal_names_the_piece_a_person_must_take_off(self):
        board = chess.Board()
        self.assertIsNone(cp.removal(board, chess.Move.from_uci("e2e4")))
        for uci in ["e2e4", "d7d5"]:
            board.push_uci(uci)
        self.assertEqual(cp.removal(board, chess.Move.from_uci("e4d5")), ("d5", "p"))
        self.assertIsNone(cp.removal(board, chess.Move.from_uci("g1f3")))
        board = chess.Board()
        for uci in ["e2e4", "a7a6", "e4e5", "d7d5"]:
            board.push_uci(uci)
        self.assertEqual(cp.removal(board, chess.Move.from_uci("e5d6")), ("d5", "p"), "en passant: the passed pawn, not the landing square")

    def test_reset_carries_strays_home_and_asks_for_the_pieces_the_person_took(self):
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5"])
        job = cp.job_for_reset(layout)
        self.assertEqual(moves(job), [("P", "d5", "e2")], "the white pawn goes back to the empty pawn square")
        self.assertEqual([s["message"] for s in job["steps"] if s["op"] == "note"], ["Put a black pawn on d7."])
        self.assertEqual(slots(layout), [])
        self.assertEqual(layout.on_board(), sorted(set(cp.Layout().on_board()) - {"d7"}), "d7 stays empty until the person fills it")
        self.assertEqual(job["expect"], cp.Layout().on_board(), "but the job expects the full start: the camera check waits for the person")
        self.assertEqual(cp.describe(job), ["Move the white pawn from d5 to e2.", "Put a black pawn on d7."])

    def test_reset_notes_one_per_missing_square_after_a_longer_game(self):
        board, layout, jobs = play(["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "f3e5", "c6e5"])
        job = cp.job_for_reset(layout)
        self.assertEqual(sorted(moves(job)), sorted([("P", "e4", "e2"), ("n", "e5", "b8"), ("B", "c4", "f1"), ("b", "c5", "f8")]))
        self.assertEqual([s["message"] for s in job["steps"] if s["op"] == "note"], ["Put a black pawn on e7.", "Put a white knight on g1."])
        self.assertEqual(job["expect"], cp.Layout().on_board())

    def test_reset_moves_a_piece_off_another_ones_home_square_first_and_swaps_through_an_empty_square(self):
        layout = cp.Layout()
        layout.pieces["g8"], layout.pieces["g1"] = "N", "n"    # the knights have swapped places
        layout.pieces["d4"] = layout.pieces.pop("d2")           # the d-pawn is out
        layout.pieces["d2"] = layout.pieces.pop("c1")           # and the bishop stands on its square
        job = cp.job_for_reset(layout)
        self.assertEqual(moves(job)[0], ("B", "d2", "c1"), "the bishop clears d2 before the pawn comes back")
        self.assertEqual([s["op"] for s in job["steps"] if s["op"] != "carry"], [], "nothing is missing, so no notes")
        occupied = set(job["before"])
        for piece, source, target in moves(job):
            self.assertIn(source, occupied, f"{piece} is on {source} when it is carried")
            self.assertNotIn(target, occupied, f"{target} is empty when {piece} arrives")
            occupied.remove(source)
            occupied.add(target)
        self.assertEqual(layout.pieces, cp.Layout().pieces, "everything ends at home")
        self.assertTrue(any(t not in cp.Layout().pieces for _, _, t in moves(job)), "one knight went round through an empty square")

    def test_reset_asks_to_take_off_a_piece_that_has_no_home_square(self):
        layout = cp.Layout()
        layout.pieces["e4"] = "Q"     # a second white queen, from the person's promotion; the first is on d1
        job = cp.job_for_reset(layout)
        self.assertEqual(moves(job), [], "it is already out of the way")
        self.assertEqual([s["message"] for s in job["steps"]], ["Take the white queen off e4."])
        self.assertNotIn("e4", layout.pieces)
        layout = cp.Layout()
        layout.pieces["d1"] = "Q"
        layout.pieces["e1"] = "Q"     # the extra queen on the king's square, the king on e3
        layout.pieces["e3"] = "K"
        job = cp.job_for_reset(layout)
        self.assertEqual([m[1] for m in moves(job)], ["e1", "e3"], "the extra queen steps aside before the king comes home")
        self.assertEqual(moves(job)[1], ("K", "e3", "e1"))
        self.assertEqual([s["message"] for s in job["steps"] if s["op"] == "note"], [f"Take the white queen off {moves(job)[0][2]}."])

    def test_a_piece_still_in_a_lane_is_understood_and_comes_home(self):
        layout = cp.Layout()
        layout.pieces["b:0"] = layout.pieces.pop("d7")    # a layout typed with a docked pawn (--layout b:0=p)
        self.assertEqual(cp._xy("b:0"), [8.0, 0.0])
        self.assertEqual(cp._xy("b:12"), [9.0, 0.0], "the second lane")
        job = cp.job_for_reset(layout)
        self.assertEqual(moves(job), [("p", "b:0", "d7")])
        self.assertEqual(layout.pieces, cp.Layout().pieces)

    def test_describe_reads_naturally(self):
        board, layout, jobs = play(["e2e4", "d7d5", "e4d5"])
        self.assertEqual(cp.describe(jobs[2]), ["Take the black pawn off d5, then press Check.", "Move the white pawn from e4 to d5."])
        self.assertEqual(cp.describe(jobs[0]), ["Move the white pawn from e2 to e4."])


def play_docked(uci_moves, start=None, slots=None):
    board = chess.Board(start) if start else chess.Board()
    layout = cp.Layout(board, captures="dock", **({"dock_slots": slots} if slots else {}))
    jobs = []
    for uci in uci_moves:
        move = chess.Move.from_uci(uci)
        jobs.append(cp.job_for_move(layout, board, move))
        board.push(move)
    return board, layout, jobs


class DockTests(unittest.TestCase):
    """The bench console's mode: the gantry carries captured pieces into the right-hand lane itself."""

    def test_a_capture_docks_the_taken_piece_on_the_right_then_carries_the_taker(self):
        board, layout, jobs = play_docked(["e2e4", "d7d5", "e4d5"])
        self.assertEqual(ops(jobs[2]), ["carry", "carry"])
        self.assertEqual(moves(jobs[2]), [("p", "d5", "b:0"), ("P", "e4", "d5")])
        self.assertEqual(jobs[2]["steps"][0]["reason"], "capture")
        self.assertEqual(jobs[2]["steps"][0]["path"], [[3.0, 4.0], [8.0, 0.0]])
        self.assertEqual(layout.pieces["b:0"], "p")
        self.assertNotIn("b:0", jobs[2]["expect"])          # the camera sees only the board
        self.assertEqual(len(jobs[2]["expect"]), 31)

    def test_both_colours_share_the_right_lane_in_order(self):
        board, layout, jobs = play_docked(["e2e4", "d7d5", "e4d5", "d8d5"])
        self.assertEqual(moves(jobs[3])[0], ("P", "d5", "b:1"))
        self.assertEqual(layout.free_slots()[0], "b:2")
        self.assertFalse(any(name.startswith("w:") for name in layout.pieces))

    def test_the_caller_may_name_the_slot(self):
        board, layout = chess.Board(), cp.Layout(captures="dock")
        for uci in ["e2e4", "d7d5"]:
            layout.plan_computer(board, chess.Move.from_uci(uci))
            board.push(chess.Move.from_uci(uci))
        steps = layout.plan_computer(board, chess.Move.from_uci("e4d5"), dock_slot="b:5")
        self.assertEqual(steps[0]["to"], "b:5")
        with self.assertRaises(ValueError):
            layout.dock("d5", "b:5")                     # taken now

    def test_en_passant_docks_the_passed_pawn(self):
        board, layout, jobs = play_docked(["e2e4", "a7a6", "e4e5", "d7d5", "e5d6"])
        self.assertEqual(moves(jobs[4]), [("p", "d5", "b:0"), ("P", "e5", "d6")])

    def test_a_full_lane_falls_back_to_a_person_removing_the_piece(self):
        board, layout, jobs = play_docked(["e2e4", "d7d5", "e4d5", "d8d5"], slots=1)
        self.assertEqual(ops(jobs[3]), ["remove", "carry"])
        self.assertEqual(jobs[3]["steps"][0]["square"], "d5")

    def test_promotion_brings_a_docked_queen_out_and_parks_the_pawn_in_its_slot(self):
        start = "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"
        board = chess.Board(start)
        layout = cp.Layout(board, captures="dock")
        layout.pieces["b:3"] = "Q"                     # a white queen taken earlier waits in the lane
        steps = layout.plan_computer(board, chess.Move.from_uci("a7a8q"))
        self.assertEqual([(s["piece"], s["from"], s["to"]) for s in steps], [("Q", "b:3", "a8"), ("P", "a7", "b:3")])
        self.assertEqual(layout.pieces["a8"], "Q")
        self.assertEqual(layout.pieces["b:3"], "P")
        self.assertFalse(any(s["op"] == "note" for s in steps))

    def test_promotion_without_a_spare_is_the_carry_and_the_note(self):
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        layout = cp.Layout(board, captures="dock")
        steps = layout.plan_computer(board, chess.Move.from_uci("a7a8q"))
        self.assertEqual([s["op"] for s in steps], ["carry", "note"])

    def test_a_promotion_can_leave_the_spare_in_the_lane(self):
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        layout = cp.Layout(board, captures="dock")
        layout.pieces["b:3"] = "Q"
        steps = layout.plan_computer(board, chess.Move.from_uci("a7a8q"), spare=False)
        self.assertEqual([s["op"] for s in steps], ["carry", "note"], "the pawn goes to a8; a person swaps it")
        self.assertEqual(layout.pieces["b:3"], "Q")
        self.assertEqual(cp.FALLBACKS[0], {}, "the plan as made is always tried first")

    def test_the_reset_puts_back_ranks_home_before_pawns_and_deep_strays_first(self):
        layout = cp.Layout()
        # A white queen wandered to c8; a black knight and a white pawn are off home in the middle.
        layout.pieces.pop("d1")
        layout.pieces.pop("c8")
        layout.pieces["c8"] = "Q"
        layout.pieces.pop("b8")
        layout.pieces["e5"] = "n"
        layout.pieces.pop("e2")
        layout.pieces["e4"] = "P"
        steps = [s for s in layout.plan_reset() if s["op"] == "carry"]
        order = [(s["from"], s["to"]) for s in steps]
        self.assertEqual(order[0], ("c8", "d1"), "the queen leaves the back rank before anything fills round it")
        self.assertEqual(order[-1], ("e4", "e2"), "pawns last")

    def test_a_reset_brings_docked_pieces_home(self):
        board, layout, jobs = play_docked(["e2e4", "d7d5", "e4d5", "d8d5"])
        job = cp.job_for_reset(layout)
        self.assertEqual(ops(job), ["carry"] * len(job["steps"]))
        sources = {s["from"] for s in job["steps"]}
        self.assertTrue({"b:0", "b:1"} <= sources)
        self.assertEqual(sorted(layout.pieces), sorted(cp.Layout().pieces))
        self.assertEqual(layout.pieces, cp.Layout().pieces)

    def test_describe_names_the_slot(self):
        board, layout, jobs = play_docked(["e2e4", "d7d5", "e4d5"])
        self.assertEqual(cp.describe(jobs[2])[0], "Take the black pawn off d5 to docking slot b:0.")

    def test_remove_mode_is_still_the_default(self):
        self.assertEqual(cp.Layout().captures, "remove")
        with self.assertRaises(ValueError):
            cp.Layout(captures="left")


if __name__ == "__main__":
    unittest.main()
