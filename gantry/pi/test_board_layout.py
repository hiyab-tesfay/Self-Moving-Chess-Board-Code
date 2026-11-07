"""The board's layout in the magnet's millimetres: counting real mm, both docking lanes, calibration."""
import unittest

import board_layout as bl
import board_paths as bp
import gantry_driver as gd

try:
    import chess
    import carry_plan as cp
except ImportError:      # python-chess missing: the docking checks are skipped
    chess = cp = None

REAL = gd.load_config(gd.DEFAULT_CONFIG)           # Sketch 5 as drawn: the magnet's travel, home at its bottom-left corner
OFF60 = dict(REAL, magnet_home_mm=[60.0, 0.0])      # the misreading of 2026-11-06: the 60 mm gantry offset added on top


class CountingTests(unittest.TestCase):
    def test_41_mm_per_turn_on_full_steps(self):
        self.assertAlmostEqual(bl.steps_per_mm(REAL), 200 / 41, places=6)
        self.assertEqual(bl.motion_lines(REAL), ["$100=4.87805", "$130=470", "$131=420"])
        self.assertAlmostEqual(bl.steps_per_mm(dict(REAL, mm_per_turn=40.0, microsteps=8)), 40.0)

    def test_the_firmware_default_needs_no_change(self):
        # firmware 0.7 boots with 200 * 1 / 41.0f; the Pi's $100 must be within the firmware's 1e-4 tolerance
        self.assertLess(abs(float(f"{bl.steps_per_mm(REAL):.5f}") - 200 / 41), 1e-4)


class LayoutTests(unittest.TestCase):
    def test_sketch_5_is_what_the_board_counts(self):
        self.assertIs(bl.machine(REAL), REAL, "home is the sketch's own corner: nothing to subtract")
        self.assertEqual(REAL["a1_center_mm"], [51.25, 26.25])
        self.assertEqual(bl.lanes(REAL), {"w": 0.0, "b": 470.0}, "the docking columns sit on the travel's side edges")
        self.assertEqual(bl.travel(REAL), (470.0, 420.0))
        board = gd.BoardMap(REAL)
        self.assertEqual(board.square_mm("a1"), (51.25, 26.25))
        self.assertEqual(board.square_mm("h8"), (418.75, 393.75), "52.5 mm squares: the board fills the 420 mm height")
        self.assertEqual(board.square_mm("w:0"), (0.0, 12.35))
        self.assertEqual(board.square_mm("b:8"), (470.0, 407.65))
        self.assertEqual(len(REAL["park_rows_mm"]), 9, "every other one of the sketch's 17 circles")

    def test_both_lanes_are_in_reach(self):
        self.assertEqual(bl.check(REAL), [])
        self.assertEqual(bl.usable_lanes(REAL), ["w", "b"])
        geometry = gd.BoardMap(REAL).geometry(REAL)
        self.assertEqual(set(geometry.slot_columns), {-1, 8})
        self.assertEqual(geometry.reach_mm, (0.0, 0.0, 470.0, 420.0), "the reach is the travel")

    def test_a_home_off_the_sketch_is_named(self):
        # adding the 60 mm gantry offset on top of the sketch puts the a file and the left column out of reach
        problems = " ".join(bl.check(OFF60))
        self.assertIn("is not inside the magnet's reach", problems)
        self.assertIn("left docking lane (x 0 on the sketch) is 60.0 mm left of where the magnet can go", problems)
        self.assertEqual(bl.usable_lanes(OFF60), ["b"])

    def test_a_lane_too_close_to_the_board(self):
        tight = dict(REAL, park_columns_mm={"w": [31.25], "b": [443.75]})
        text = " ".join(bl.check(tight))
        self.assertIn("left docking lane is only 20.0 mm from the a-file", text)
        self.assertIn("right docking lane is only 25.0 mm from the h-file", text)

    def test_per_axis_squares(self):
        two = dict(REAL, square_mm=[52.6, 52.2])
        self.assertEqual(bl.square_xy(two), (52.6, 52.2))
        board = gd.BoardMap(two)
        x, y = board.square_mm("a8")
        self.assertAlmostEqual(x, 51.25)
        self.assertAlmostEqual(y, 26.25 + 7 * 52.2)
        geometry = board.geometry(two)
        for got, want in zip(geometry.board_mm(7, 7), board.square_mm("h8")):
            self.assertAlmostEqual(got, want)
        self.assertAlmostEqual(board.units(*board.square_mm("c6"))[1], 5.0)


class CalibrationTests(unittest.TestCase):
    def test_three_points_give_the_board_and_the_lanes(self):
        new, report = bl.from_points(REAL, a1=(52.0, 27.0), h1=(420.2, 27.4), h8=(420.0, 393.5))
        self.assertEqual(new["a1_center_mm"], [52.0, 27.0])
        self.assertEqual(new["square_mm"], [52.6, 52.3])
        self.assertEqual(new["park_columns_mm"], {"w": [0.75], "b": [470.0]}, "51.25 out from the edge files, kept inside the travel")
        self.assertEqual(new["park_rows_mm"], REAL["park_rows_mm"], "slot rows kept")
        self.assertEqual(new["travel_mm"], REAL["travel_mm"])
        self.assertIn("a1 centre 52.0, 27.0 mm on the sketch; squares 52.60 mm along X and 52.30 mm along Y", report[0])
        self.assertIn("Both docking lanes and the whole board are inside the magnet's reach.", report)
        self.assertEqual(REAL["a1_center_mm"], [51.25, 26.25], "the old config is not touched")

    def test_a_home_off_the_sketch_comes_back_in_sketch_mm(self):
        new, _ = bl.from_points(OFF60, a1=(-8.0, 27.0), h1=(360.2, 27.4), h8=(360.0, 393.5))
        self.assertEqual(new["a1_center_mm"], [52.0, 27.0], "steered in the board's mm, saved in the sketch's")
        self.assertEqual(new["magnet_home_mm"], [60.0, 0.0])
        self.assertEqual(gd.BoardMap(new).square_mm("a1"), (-8.0, 27.0))

    def test_measured_lane_far_corner_and_a_turned_board(self):
        new, report = bl.from_points(REAL, a1=(51, 26), h1=(418, 29), h8=(416, 396), far=(468.0, 400.0), right=(467.0, 13.0))
        self.assertEqual(new["travel_mm"], [468.0, 400.0])
        self.assertEqual(new["park_columns_mm"], {"w": [0.0], "b": [467.0]})
        self.assertEqual(new["park_rows_mm"][0], 13.0)
        self.assertEqual(new["park_rows_mm"][-1], 358.89, "slots past the travel dropped: 8 of 9 fit")
        self.assertTrue(any("The board is turned" in line and "+3.0 mm" in line for line in report), report)

    def test_nonsense_points_are_refused(self):
        with self.assertRaises(ValueError):
            bl.from_points(REAL, a1=(45, 35), h1=(45, 35), h8=(395, 385))


@unittest.skipUnless(cp, "needs python-chess")
class DockingTests(unittest.TestCase):
    def test_white_pieces_dock_left_black_right(self):
        layout = cp.Layout(captures="dock", dock_lanes=("w", "b"))
        self.assertEqual(layout.free_slots("P")[0], "w:0")
        self.assertEqual(layout.free_slots("p")[0], "b:0")
        layout = cp.Layout(chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"), captures="dock", dock_lanes=("w", "b"))
        steps = layout.plan_computer(chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"), chess.Move.from_uci("e4d5"))
        self.assertEqual(steps[0]["to"], "b:0", "the black pawn goes to the right lane")
        black = cp.Layout(chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2"), captures="dock", dock_lanes=("w", "b"))
        steps = black.plan_computer(chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2"), chess.Move.from_uci("d5e4"))
        self.assertEqual(steps[0]["to"], "w:0", "the white pawn goes to the left lane")

    def test_a_full_lane_overflows_and_the_right_lane_alone_still_works(self):
        layout = cp.Layout(captures="dock", dock_lanes=("w", "b"), dock_slots=1)
        layout.pieces["w:0"] = "P"
        self.assertEqual(layout.free_slots("N"), ["b:0"])
        self.assertEqual(cp.Layout(captures="dock").free_slots("N")[0], "b:0", "the default is the right lane only")
        self.assertEqual(layout.docked("P"), "w:0")

    def test_the_path_finder_reaches_both_lanes(self):
        geometry = gd.BoardMap(REAL).geometry(REAL)
        world = bp.Board(geometry, bp.occupied_at_start() - {"a2", "h7"}, pieces=bp.pieces_at_start())
        self.assertIsNotNone(world.find_path("a1", "w:0"))
        self.assertIsNotNone(world.find_path("a1", "w:8"))
        self.assertIsNotNone(world.find_path("h8", "b:8"))
        self.assertIsNotNone(world.find_path("h8", "b:0"))


if __name__ == "__main__":
    unittest.main()
