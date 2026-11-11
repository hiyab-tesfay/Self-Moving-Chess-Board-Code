"""The X/Y swap on the link (config "swap_xy"): what the Pi means by X reaches the board's Y motor and back."""
import os
import unittest
from unittest import mock

import gantry_driver as gd


class SwapLineTests(unittest.TestCase):
    def test_current_a2_to_a4_move_increases_motor_y(self):
        config = gd.load_config(gd.HERE / "gantry_config.json")
        self.assertEqual(config["home_beside"], "a1")
        self.assertFalse(config["swap_xy"])
        with mock.patch.dict(os.environ):
            os.environ.pop("GANTRY_HOME_BESIDE", None)
            os.environ.pop("GANTRY_SWAP_XY", None)
            board = gd.BoardMap(config)
            self.assertEqual(gd.home_xy(config), board.square_mm("a2"))
            wire = []
            for square in ("a2", "a4"):
                x, y = board.square_mm(square)
                line = f"G1 X{x:.3f} Y{y:.3f}"
                wire.append(gd.swap_line(gd.flip_line(line, gd.y_flip_mm(config)), gd.swap_xy_flag(config)))
            self.assertEqual(wire, ["G1 X42.000 Y102.000", "G1 X42.000 Y208.000"])

    def test_moves_and_g92_exchange_words(self):
        self.assertEqual(gd.swap_line("G1 X10.000 Y20.500 F2400", True), "G1 Y10.000 X20.500 F2400")
        self.assertEqual(gd.swap_line("G92 X5 Y7", True), "G92 Y5 X7")
        self.assertEqual(gd.swap_line("G1 X10 F100", True), "G1 Y10 F100")

    def test_status_and_travel(self):
        self.assertIn("MPos:2.000,1.000", gd.swap_line("<Idle|MPos:1.000,2.000|H:1>", True))
        self.assertEqual(gd.swap_line("$130=470", True), "$131=470")
        self.assertEqual(gd.swap_line("$131=420", True), "$130=420")
        self.assertEqual(gd.swap_line("$100=5.17", True), "$100=5.17")

    def test_off_and_other_lines_untouched(self):
        self.assertEqual(gd.swap_line("G1 X1 Y2", False), "G1 X1 Y2")
        self.assertEqual(gd.swap_line("M7", True), "M7")
        self.assertEqual(gd.swap_line("ok", True), "ok")

    def test_round_trip_with_the_a8_mirror(self):
        # Sending: mirror Y, then swap. Reading: swap, then mirror. A point must come back unchanged.
        sent = gd.swap_line(gd.flip_line("G1 X95.000 Y420.000", 420.0), True)
        self.assertEqual(sent, "G1 Y95.000 X0.000")         # a8's file is along the board's Y; a8 is at home along X
        status = "<Idle|MPos:0.000,95.000|H:1>"
        back = gd.flip_line(gd.swap_line(status, True), 420.0)
        self.assertIn("MPos:95.000,420.000", back)

    def test_flag_and_memory_key(self):
        saved = os.environ.pop("GANTRY_SWAP_XY", None)
        self.addCleanup(lambda: saved is None or os.environ.__setitem__("GANTRY_SWAP_XY", saved))
        self.assertTrue(gd.swap_xy_flag({"swap_xy": True}))
        self.assertFalse(gd.swap_xy_flag({}))
        os.environ["GANTRY_SWAP_XY"] = "0"
        try:
            self.assertFalse(gd.swap_xy_flag({"swap_xy": True}))
        finally:
            del os.environ["GANTRY_SWAP_XY"]
        self.assertNotEqual(gd.home_beside_name(420.0, True), gd.home_beside_name(420.0, False))
        self.assertTrue(gd.uses_gcode({"swap_xy": True, "home_beside": "a1", "travel_mm": [470, 420]}))


if __name__ == "__main__":
    unittest.main()
