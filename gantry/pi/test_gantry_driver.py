"""The Pi side against the pretend board: the right millimetres, in the right order, and the
predicted steps match what the board counted. Builds gantry/firmware/host/virtual_gantry first.

    python3 -m unittest test_gantry_driver    (from gantry/pi)
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gantry_driver as gd  # noqa: E402

HOST = Path(__file__).resolve().parent.parent / "firmware" / "host"
CONFIG = {
    "port": "sim", "baud": 115200, "square_mm": 50.0, "a1_center_mm": [75.0, 40.0],
    "park_columns_mm": {"w": [43.0, 15.0], "b": [507.0, 535.0]}, "park_rows_mm": None,
    "travel_mm": [450.0, 400.0], "travel_speed_mm_s": 120.0, "carry_speed_mm_s": 60.0,
    "grab_settle_s": 0.3, "release_settle_s": 0.2, "home_on_start": True,
}
STEPS_PER_MM = 40.0


def job(label, *carries):
    return {"id": 1, "label": label, "expect": [], "steps": [{"op": "carry", "piece": p, "from": a, "to": b, "path": path} for p, a, b, path in carries]}


class BoardMapTests(unittest.TestCase):
    def setUp(self):
        self.board = gd.BoardMap(CONFIG)

    def test_squares_and_lanes(self):
        self.assertEqual(self.board.square_mm("a1"), (75.0, 40.0))
        self.assertEqual(self.board.square_mm("e4"), (275.0, 190.0))
        self.assertEqual(self.board.point_mm(4.5, 1.5), (300.0, 115.0))

    def test_parking_slots_use_the_configured_columns(self):
        self.assertEqual(self.board.square_mm("w:0"), (43.0, 40.0))
        self.assertEqual(self.board.square_mm("w:9"), (15.0, 90.0))
        self.assertEqual(self.board.square_mm("b:7"), (507.0, 390.0))
        self.assertEqual(self.board.point_mm(-0.5, 0), (59.0, 40.0))

    def test_unreachable_points_fail_before_anything_moves(self):
        with self.assertRaises(gd.GantryError):
            gd.plan(job("x", ("p", "h2", "b:0", [[7, 1], [8, 0]])), self.board, CONFIG)  # b:0 is at X=507 > 450


class PredictionTests(unittest.TestCase):
    def test_a_pawn_push_is_two_squares_of_steps(self):
        lines = gd.plan(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), gd.BoardMap(CONFIG), CONFIG)
        self.assertEqual([t for t, _ in lines], ["G1 X275.000 Y90.000 F7200", "M3", "G4 P0.300", "G1 X275.000 Y190.000 F3600", "M5", "G4 P0.200"])
        guess = gd.predict(lines, CONFIG, STEPS_PER_MM, start=(275.0, 90.0))
        self.assertEqual(guess["steps"], [0, 4000], "100 mm at 40 steps/mm")
        self.assertEqual(guess["end_mm"], (275.0, 190.0))


@unittest.skipUnless(shutil.which("make") and shutil.which("c++"), "needs a C++ compiler")
class PretendBoardTests(unittest.TestCase):
    config_path = Path(__file__).resolve().parent / ".test_config.json"

    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-s", "-C", str(HOST), "virtual_gantry"], check=True)
        os.environ["GANTRY_SIM_SPEED"] = "200"
        cls.config_path.write_text(json.dumps(CONFIG))

    def run_job(self, a_job, config=CONFIG):
        link = gd.Link("sim", 115200)
        try:
            state = gd.run_job(a_job, config, link)
        finally:
            link.close()
        moves = [re.search(r"steps=(\d+),(\d+) to X=([\d.]+) Y=([\d.]+)", t) for t in link.traces if t.startswith("# MOVE")]
        mags = [re.search(r"MAG (on|off) X=([\d.]+) Y=([\d.]+)", t) for t in link.traces if t.startswith("# MAG")]
        return state, [(int(m[1]), int(m[2]), float(m[3]), float(m[4])) for m in moves], [(m[1], float(m[2]), float(m[3])) for m in mags]

    def test_a_move_homes_first_then_grabs_carries_and_releases_at_the_right_squares(self):
        state, moves, mags = self.run_job(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])))
        self.assertTrue(state["homed"])
        self.assertEqual((state["x"], state["y"]), (275.0, 190.0), "ends over e4")
        # Magnet on at e2 and off at e4, in that order.
        self.assertEqual(mags, [("on", 275.0, 90.0), ("off", 275.0, 190.0)])
        # The carry itself: 100 mm in Y only, which is 4000 steps on the Y motor, exactly as predicted.
        carry = [m for m in moves if (m[2], m[3]) == (275.0, 190.0)][-1]
        self.assertEqual(carry[:2], (0, 4000))
        self.assertFalse(state["magnet"])

    def test_a_capture_parks_the_taken_piece_first(self):
        captured = ("p", "d5", "w:0", [[3, 4], [2.5, 4.5], [-0.5, 4.5], [-1, 4]])
        taker = ("P", "e4", "d5", [[4, 3], [3, 4]])
        state, moves, mags = self.run_job(job("e4d5", captured, taker))
        self.assertEqual([m[0] for m in mags], ["on", "off", "on", "off"])
        self.assertEqual(mags[0][1:], (225.0, 240.0), "picks the pawn up on d5")
        self.assertEqual(mags[1][1:], (43.0, 240.0), "drops it in slot w:0's column, row 5")
        self.assertEqual(mags[3][1:], (225.0, 240.0), "the taker ends on d5")
        self.assertEqual((state["x"], state["y"]), (225.0, 240.0))

    def test_the_website_contract_job_on_stdin_exit_code(self):
        result = subprocess.run([sys.executable, str(Path(gd.__file__)), "--port", "sim", "--config", str(self.config_path), "--quiet"],
                                input=json.dumps(job("g1f3", ("N", "g1", "f3", [[6, 0], [5.5, 0.5], [5.5, 1.5], [5, 2]]))),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(reply["ok"])
        self.assertEqual((reply["x_mm"], reply["y_mm"]), (325.0, 140.0), "f3")
        bad = subprocess.run([sys.executable, str(Path(gd.__file__)), "--port", "sim", "--config", str(self.config_path), "--quiet"],
                             input=json.dumps(job("h2", ("p", "h2", "b:0", [[7, 1], [8, 0]]))), capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("outside the gantry's travel", json.loads(bad.stdout.strip().splitlines()[-1])["error"])

    def test_calibration_steers_to_a1_and_h1_and_saves_the_squares(self):
        config = dict(CONFIG, square_mm=1.0, a1_center_mm=[0.0, 0.0])
        path = Path(__file__).resolve().parent / ".test_calibrated.json"
        answers = iter(["x+60", "y+35", "bogus", "x+900", "ok", "x+350", "ok"])
        said = []
        link = gd.Link("sim", 115200)
        try:
            gd.calibrate(gd.Gantry(link), config, path, read=lambda prompt: next(answers), write=said.append)
            saved = json.loads(path.read_text())
        finally:
            link.close()
            path.unlink(missing_ok=True)
        self.assertEqual(saved["a1_center_mm"], [60.0, 35.0])
        self.assertEqual(saved["square_mm"], 50.0)
        self.assertEqual(saved["travel_mm"], [450.0, 400.0], "the rest of the config is kept")
        self.assertTrue(any("x+10 moves" in line for line in said), "a typo gets help, not a crash")
        self.assertTrue(any("outside the gantry's travel" in line for line in said), "a jog past the travel is refused")
        self.assertTrue(any("squares 50.00 mm" in line for line in said))
        moves = [t for t in link.traces if t.startswith("# MOVE")]
        self.assertIn("to X=410.000 Y=35.000", moves[-1], "the last jog ends under h1")

    def test_dry_run_prints_the_gcode_and_the_prediction(self):
        result = subprocess.run([sys.executable, str(Path(gd.__file__)), "--dry-run", "--config", str(self.config_path), "--move", "a2a4"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        self.assertIn("G1 X75.000 Y190.000 F3600", result.stdout, "a straight clear run from a2 to a4")
        self.assertIn("motor steps X=", result.stdout)
        knight = subprocess.run([sys.executable, str(Path(gd.__file__)), "--dry-run", "--config", str(self.config_path), "--move", "g1f3"],
                                capture_output=True, text=True)
        self.assertEqual(knight.returncode, 0, knight.stderr[-300:])
        self.assertNotIn("(make room)", knight.stdout, "the knight goes through the lanes; nothing steps aside")
        self.assertEqual(knight.stdout.count("M3"), 1, "one grab: the knight only")
        self.assertIn("G1 X350.000 Y65.000 F3600", knight.stdout, "the corner between f1, g1, f2 and g2")
        self.assertIn("G1 X350.000 Y115.000 F3600", knight.stdout, "up the lane between f2 and g2")

    @classmethod
    def tearDownClass(cls):
        cls.config_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
