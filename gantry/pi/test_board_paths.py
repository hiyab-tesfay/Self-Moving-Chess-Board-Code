"""The path finder: nothing is touched, lanes are used only where a piece fits, and room is made when it does not."""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_paths as bp  # noqa: E402

# The sketch's numbers: 50 mm squares, a1 at (60, 35), one lane each side at a 35 mm pitch.
ROWS = [17.5 + 35 * i for i in range(12)]
GEO = bp.Geometry(50.0, (60.0, 35.0), {-1: 17.5, 8: 452.5}, ROWS, piece_mm=27.0, clearance_mm=3.0)


def clearance_ok(board: bp.Board, carry: bp.Carry) -> float:
    """Smallest centre-to-centre distance between the carried piece and any other piece along the way."""
    nodes = [board._node_of(p) for p in carry.path]
    smallest = float("inf")
    for a, b in zip(nodes, nodes[1:]):
        pa, pb = board.g.node_mm(a), board.g.node_mm(b)
        for name in board.occupied:
            if name in (carry.piece_from, carry.piece_to):
                continue
            smallest = min(smallest, bp._dist_point_segment(board.g.node_mm(board.node(name)), pa, pb))
    return smallest


def air_ok(board: bp.Board, carry: bp.Carry) -> float:
    """Smallest gap in mm between the carried piece's edge and any other piece's edge along the way."""
    nodes = [board._node_of(p) for p in carry.path]
    carried = board.size_of(carry.piece_from)
    smallest = float("inf")
    for a, b in zip(nodes, nodes[1:]):
        pa, pb = board.g.node_mm(a), board.g.node_mm(b)
        for name in board.occupied:
            if name in (carry.piece_from, carry.piece_to):
                continue
            centre = bp._dist_point_segment(board.g.node_mm(board.node(name)), pa, pb)
            smallest = min(smallest, centre - (carried + board.size_of(name)) / 2)
    return smallest


class PathTests(unittest.TestCase):
    def test_a_clear_straight_line_is_one_run(self):
        board = bp.Board(GEO, {"e2"})
        carry, = board.plan("e2", "e4")
        self.assertEqual(carry.path, [[4.0, 1.0], [4.0, 3.0]])

    def test_a_blocked_line_goes_round_through_the_lanes(self):
        board = bp.Board(GEO, {"a1", "a4"})   # a rook wants a8; a4 is in the way, the b file is empty
        carry, = board.plan("a1", "a8")
        self.assertNotIn([0.0, 3.0], carry.path, "never passes over a4")
        self.assertGreaterEqual(clearance_ok(board, carry), GEO.keep_apart_mm)
        self.assertEqual(carry.path[0], [0.0, 0.0])
        self.assertEqual(carry.path[-1], [0.0, 7.0])

    def test_a_lane_between_two_pieces_is_too_narrow_for_27_mm_bases_on_50_mm_squares(self):
        # A piece on e1 heading to e3 with the whole second rank occupied: every lane up from rank 1
        # passes between two rank-2 pieces, 23 mm apart. (No parking lanes here: with them it would
        # go round the end of the rank and down the empty lane, which is allowed.)
        no_lanes = bp.Geometry(50.0, (60.0, 35.0), {}, [], 27.0, 3.0)
        board = bp.Board(no_lanes, {"e1", "a2", "b2", "c2", "d2", "e2", "f2", "g2", "h2"}, slots_per_lane=12)
        self.assertIsNone(board.find_path("e1", "e3"))
        wider = bp.Board(bp.Geometry(60.0, (60.0, 35.0), {}, [], 27.0, 3.0), board.occupied, slots_per_lane=12)
        self.assertIsNotNone(wider.find_path("e1", "e3"), "with 60 mm squares the lane is 33 mm wide and the piece fits")

    def test_an_empty_parking_lane_is_a_corridor_round_the_pawns(self):
        board = bp.Board(GEO, {"e1", "a2", "b2", "c2", "d2", "e2", "f2", "g2", "h2"})
        carry, = board.plan("e1", "e3")
        self.assertTrue(any(p[0] in (-1.0, 8.0) for p in carry.path), "uses a lane beside the board")
        self.assertGreaterEqual(clearance_ok(board, carry), GEO.keep_apart_mm)

    def test_the_knight_goes_through_the_lanes_between_the_pawns(self):
        board = bp.Board(GEO, bp.occupied_at_start(), pieces=bp.pieces_at_start())
        carry, = board.plan("g1", "f3")
        self.assertEqual(carry.reason, "move", "no piece is moved aside")
        self.assertEqual(carry.path, [[6.0, 0.0], [5.5, 0.5], [5.5, 1.5], [5.0, 2.0]],
                         "out to the corner, up the lane between f2 and g2, into f3 from its corner")
        self.assertGreaterEqual(air_ok(board, carry), GEO.lane_clearance_mm - 1e-9, "1 mm of air between the 25 mm knight and the 23 mm pawns")
        # The other knight, and Black's, do the same.
        self.assertEqual(len(board.plan("b1", "c3")), 1)
        self.assertEqual(len(board.plan("g8", "f6")), 1)

    def test_without_piece_types_the_finder_stays_cautious_and_makes_room(self):
        board = bp.Board(GEO, bp.occupied_at_start())   # every piece counts as 27 mm: no lane is wide enough
        carries = board.plan("g1", "f3")
        self.assertEqual([c.reason for c in carries], ["make room", "move", "put back"])
        out, move, back = carries
        self.assertIn(out.piece_from, ("f2", "g2", "h2", "e2"), "a rank-2 pawn steps aside")
        self.assertEqual((move.piece_from, move.piece_to), ("g1", "f3"))
        self.assertEqual((back.piece_from, back.piece_to), (out.piece_to, out.piece_from))
        world = bp.Board(GEO, bp.occupied_at_start())
        for carry in carries:
            self.assertGreaterEqual(clearance_ok(world, carry), GEO.keep_apart_mm - 1e-9, f"{carry.reason} {carry.piece_from}->{carry.piece_to} brushes a piece")
            world.apply(carry)
        self.assertEqual(world.occupied, (bp.occupied_at_start() - {"g1"}) | {"f3"}, "only the knight has moved, in the end")

    def test_making_room_is_the_last_resort_between_wide_pieces(self):
        pieces = {"d1": "Q", "e1": "K", "c2": "B", "d2": "P", "e2": "P"}
        board = bp.Board(GEO, set(pieces), pieces=pieces)
        self.assertEqual(len(board.plan("c2", "c4")), 1, "straight up: c3 is clear, one run")
        boxed = {"a1": "R", "b1": "N", "c1": "B", "d1": "Q", "e1": "K", "f1": "B", "g1": "N", "h1": "R",
                 "a2": "P", "b2": "P", "c2": "P", "d2": "P", "e2": "P", "f2": "P", "g2": "P", "h2": "P"}
        board = bp.Board(GEO, set(boxed), pieces=boxed)
        carries = board.plan("d1", "d3")   # the 27 mm queen needs 26 mm of lane beside a 23 mm pawn; a lane gives 25
        self.assertEqual([c.reason for c in carries], ["make room", "move", "put back"])

    def test_every_path_ends_exactly_on_the_target_centre(self):
        board = bp.Board(GEO, bp.occupied_at_start(), pieces=bp.pieces_at_start())
        for carry in board.plan("g1", "f3"):
            last = board._node_of(carry.path[-1])
            self.assertEqual(board.g.node_mm(last), board.g.node_mm(board.node(carry.piece_to)))
        self.assertEqual(GEO.node_mm(bp.square_node("f3")), (310.0, 135.0))

    def test_captured_piece_goes_down_the_lane_at_the_lanes_own_pitch(self):
        board = bp.Board(GEO, {"d5", "e4"})
        carry, = board.plan("d5", "w:0")
        self.assertEqual(carry.path[-1], [-1.0, 0.0])
        self.assertEqual(GEO.node_mm(board._node_of(carry.path[-1])), (17.5, 17.5))
        self.assertGreaterEqual(clearance_ok(board, carry), GEO.keep_apart_mm)
        board.apply(carry)
        second, = board.plan("e4", "b:3")
        self.assertEqual(GEO.node_mm(board._node_of(second.path[-1])), (452.5, 122.5))

    def test_a_full_lane_blocks_the_next_slot_behind_it(self):
        board = bp.Board(GEO, {"d5", "w:0", "w:1", "w:2"})
        carry, = board.plan("d5", "w:3")
        self.assertEqual(carry.path[-1], [-1.0, 3.0])
        self.assertGreaterEqual(clearance_ok(board, carry), GEO.keep_apart_mm)

    def test_a_piece_with_nowhere_to_go_is_reported_not_rammed(self):
        full = {f"{f}{r}" for f in bp.FILES for r in range(1, 9)} - {"h8"}
        board = bp.Board(GEO, full)
        with self.assertRaises(bp.NoPath):
            board.plan("a1", "h8")

    def test_bad_requests(self):
        board = bp.Board(GEO, {"e2", "e4"})
        with self.assertRaises(bp.NoPath):
            board.plan("e3", "e5")
        with self.assertRaises(bp.NoPath):
            board.plan("e2", "e4")


# The bench: only the right lane holds pieces, and the magnet (on the right of the Y carriage) cannot reach x < 50 mm.
BENCH = dict(slot_columns=(8,), reach_mm=(50.0, 0.0, 470.0, 420.0))


def bench(**options) -> bp.Geometry:
    return bp.Geometry(50.0, (60.0, 35.0), {-1: 17.5, 8: 452.5}, ROWS, piece_mm=27.0, clearance_mm=3.0, **{**BENCH, **options})


def corners(path) -> int:
    return len(path) - 2


class OptionTests(unittest.TestCase):
    def test_nothing_out_of_reach_is_ever_used(self):
        board = bp.Board(bench(), bp.occupied_at_start(), pieces=bp.pieces_at_start())
        self.assertFalse([n for n in board.nodes if not board.g.reachable(*board.g.node_mm(n))])
        self.assertFalse([n for n in board.nodes if isinstance(n, bp.Slot) and n.column < 0], "the left lane is off")
        with self.assertRaises(bp.NoPath):
            board.plan("a2", "w:0")
        # Every carry the A file makes stays right of 50 mm.
        for carry in bp.Board(bench(), {"a1", "a4"}).plan("a1", "a8"):
            self.assertGreaterEqual(min(board.g.board_mm(*p)[0] for p in carry.path), 50.0)

    def test_a_square_out_of_reach_is_reported(self):
        board = bp.Board(bench(reach_mm=(70.0, 0.0, 470.0, 420.0)), {"a1"})
        with self.assertRaises(bp.NoPath) as caught:
            board.plan("a1", "b3")
        self.assertIn("reach", str(caught.exception))

    def test_shortcuts_cut_corners_but_never_closer_than_the_open_clearance(self):
        occupied = {"c1", "d4", "f3", "b5"}
        plain = bp.Board(bench(), occupied).plan("c1", "g6")
        short = bp.Board(bench(smooth=True), occupied).plan("c1", "g6")
        self.assertLessEqual(corners(short[-1].path), corners(plain[-1].path))
        board = bp.Board(bench(smooth=True), occupied)
        self.assertGreaterEqual(air_ok(board, short[-1]), 0.0)
        for a, b in zip(short[-1].path, short[-1].path[1:]):
            self.assertTrue(board.clear_mm(board.g.board_mm(*a), board.g.board_mm(*b), {"c1", "g6"}, None,
                                           3.0 if (a[0] != b[0] and a[1] != b[1]) else 1.0))

    def test_a_straight_shortcut_is_one_run(self):
        carry, = bp.Board(bench(smooth=True), {"b1"}).plan("b1", "c3")
        self.assertEqual(carry.path, [[1.0, 0.0], [2.0, 2.0]], "a knight on an empty board slides straight")

    def test_the_best_slot_is_the_nearest_clear_one(self):
        board = bp.Board(bench(), {"h1", "b:0", "b:1"})
        self.assertEqual(board.best_slot("h1", ["b:2", "b:5", "b:11"]), "b:2")
        self.assertEqual(bp.Board(bench(), {"h8"}).best_slot("h8", ["b:0", "b:11"]), "b:11")

    def test_the_grid_is_built_once_per_geometry(self):
        g = bench()
        a, b = bp.Board(g, {"e2"}), bp.Board(g, {"d7", "e7"})
        self.assertIs(a.edges, b.edges)
        self.assertIsNot(a.edges, bp.Board(bench(reach_mm=None), {"e2"}).edges)

    def test_a_corner_piece_with_both_neighbours_finds_a_way_or_says_so(self):
        # The case the simulation found: a8 hemmed in by a7, b7 and b8 with the left lane out of reach.
        occupied = {"a8", "b8", "a7", "b7", "c8", "c7"}
        pieces = {"a8": "r", "b8": "n", "a7": "p", "b7": "P", "c8": "b", "c7": "p"}
        board = bp.Board(bench(smooth=True), occupied, pieces=pieces)
        try:
            carries = board.plan("a8", "b:0")
        except bp.NoPath as exc:
            self.assertIn("a8", str(exc))
        else:
            self.assertEqual(carries[-2 if len(carries) == 3 else 0].piece_to, "b:0")


if __name__ == "__main__":
    unittest.main()
