"""The touch screen's manual gantry controls (pi_gantry.GantryPlayer: status, jog, center, go_to_square, set_home_here,
save_home_spot_here, set_start_here, return_home, motors_off) against the pretend board, with the real board's
gantry_config.json copied to a temporary file (home beside a8, 53 mm squares) so nothing real is written: the
pretend board's position file is GANTRY_SIM_STATE in a temporary folder, never ~/.duen-gantry.

    CHESS_GANTRY_DIR=.../code/pi .venv/bin/python -m unittest test.test_pi_gantry_control
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import pi_gantry

try:
    from .test_pi_gantry import GANTRY_DIR, pretend_board_builds
except ImportError:   # run from inside test/
    from test_pi_gantry import GANTRY_DIR, pretend_board_builds


@unittest.skipUnless(pretend_board_builds(), "needs the gantry folder and a C++ compiler for the pretend board")
class ManualControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config_path = self.tmp / "gantry_config.json"
        config = json.loads((GANTRY_DIR / "gantry_config.json").read_text())
        for key in ("start_mm", "start_square", "home_spot_mm"):
            config.pop(key, None)
        config["home_beside"] = "a8"
        self.config_path.write_text(json.dumps(config, indent=2))
        self.state = self.tmp / "sim-position.json"
        env = mock.patch.dict(os.environ, {
            "CHESS_GANTRY_PORT": "sim", "CHESS_GANTRY_DIR": str(GANTRY_DIR), "CHESS_GANTRY_CONFIG": str(self.config_path),
            "GANTRY_SIM_SPEED": "200", "GANTRY_SIM_STATE": str(self.state), "GANTRY_STATE_DIR": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GANTRY_HOME_BESIDE", None)
        self.player = pi_gantry.GantryPlayer.from_env()
        self.assertIsNotNone(self.player)
        self.addCleanup(self.player.close)
        gd = self.player.gd
        drain = gd.Gantry.drain
        quick = mock.patch.object(gd.Gantry, "drain", lambda g, seconds: drain(g, min(seconds, 0.2)))
        quick.start()
        self.addCleanup(quick.stop)
        self.map = gd.BoardMap(self.player.config)

    STEP = 0.25   # 41 mm per turn at 200 full steps: the board counts in steps of 0.205 mm

    def assertAt(self, st, x, y):
        self.assertAlmostEqual(st["x"], x, delta=self.STEP)
        self.assertAlmostEqual(st["y"], y, delta=self.STEP)

    def assertNear(self, pair, expected):
        for got, want in zip(pair, expected):
            self.assertAlmostEqual(got, want, delta=self.STEP)

    def file(self) -> dict:
        return json.loads(self.config_path.read_text())

    def test_before_homing_every_direction_jogs_and_nothing_that_needs_home(self):
        st = self.player.status()
        self.assertFalse(st["homed"])
        self.assertAt(st, 0.0, 420.0)       # a restarted board counts 0,0: beside a8, as the Pi counts
        # Not homed: every direction works (towards the a8 corner too), so a person can steer onto the home spot.
        # Each jog first calls the spot the middle of the travel (235, 210), then moves by the step.
        self.assertAt(self.player.jog(-5, 0), 230.0, 210.0)
        self.assertAt(self.player.jog(0, 5), 235.0, 215.0)
        self.assertAt(self.player.jog(5, -5), 240.0, 205.0)
        self.assertFalse(self.player.status()["homed"], "jogging never claims a home")
        for action in (self.player.center, self.player.return_home, self.player.set_start_here,
                       lambda: self.player.go_to_square("e4")):
            with self.assertRaisesRegex(pi_gantry.GantryError, "Not homed"):
                action()
        st = self.player.set_home_here()    # steered onto the home spot: home it there
        self.assertTrue(st["homed"])

    def test_home_in_the_corner_then_it_drives_to_the_memorised_start(self):
        self.player.set_home_here()                        # in the corner: beside a8, the Pi counts 0, 420
        self.player.jog(53, 0)
        self.player.jog(0, -26)
        st = self.player.set_start_here()
        corner = st["from_corner"]
        self.assertAlmostEqual(corner["mm"][0], 53.0, delta=self.STEP)
        self.assertAlmostEqual(corner["mm"][1], 26.0, delta=self.STEP)
        per_mm = 200 * 1 / 41.0                            # full steps per turn x microsteps / mm per turn
        self.assertAlmostEqual(corner["steps"][0], 53.0 * per_mm, delta=1.5)
        self.assertAlmostEqual(corner["steps"][1], 26.0 * per_mm, delta=1.5)
        self.player.motors_off()                           # pushed back into the corner by hand ...
        st = self.player.set_home_here()                   # ... homed there: it goes straight to the start
        self.assertTrue(st["went_to_start"])
        self.assertAt(st, *self.player.config["start_mm"])

    def test_the_arm_goes_up_and_down_before_and_after_homing(self):
        self.player.config["servo_settle_s"] = 0.0
        st = self.player.arm(True)
        self.assertEqual(st["servo"], "up")
        self.assertEqual(self.player.arm(False)["servo"], "down")
        self.player.set_home_here()
        self.assertEqual(self.player.arm(True)["servo"], "up")
        st = self.player.jog(5, -5)               # a move lowers the arm first: nothing is dragged
        self.assertEqual(st["servo"], "down")

    def test_home_center_jog_and_go_to(self):
        st = self.player.set_home_here()
        self.assertTrue(st["homed"])
        self.assertAt(st, 0.0, 420.0)
        self.assertAt(self.player.center(), *self.map.square_mm("a8"))   # no start saved: a8's centre
        # a8's centre is on the frame's edge along Y (home is beside it): ▲ refuses, ◀ a square stops at the edge.
        with self.assertRaisesRegex(pi_gantry.GantryError, "edge"):
            self.player.jog(0, 1)
        self.assertAt(self.player.jog(-53, -10), 0.0, 410.0)
        self.assertAt(self.player.jog(0, 53), 0.0, 420.0)
        self.player.center()
        sx, sy = self.player.square_size_mm()
        self.assertEqual((sx, sy), (53.0, 53.0))
        st = self.player.jog(sx, -sy)
        self.assertAt(st, self.map.square_mm("a8")[0] + 53.0, 420.0 - 53.0)
        self.assertAt(self.player.go_to_square("e4"), *self.map.square_mm("e4"))
        st = self.player.go_to_square("h1")
        self.assertAt(st, *self.map.square_mm("h1"))
        ux, uy = st["units"]
        self.assertAlmostEqual(ux, 7.0, places=2)
        self.assertAlmostEqual(uy, 0.0, places=2)
        self.assertAt(self.player.return_home(), 0.0, 420.0)
        with self.assertRaises(pi_gantry.GantryError):
            self.player.go_to_square("z9")

    def test_set_start_here_saves_it_and_center_goes_there(self):
        self.player.save_to_file = True     # a temporary copy of the config: the file may be written
        self.player.set_home_here()
        self.player.go_to_square("a8")
        self.player.jog(1, -1)
        st = self.player.set_start_here()
        x, y = self.map.square_mm("a8")
        self.assertNear(st["saved"]["start_mm"], [x + 1, y - 1])
        self.assertEqual(self.file()["start_mm"], st["saved"]["start_mm"])
        self.assertEqual(self.file()["start_square"], "a8")
        self.assertTrue((self.tmp / "gantry_config.json.before-touch").is_file())
        self.assertNotIn("start_mm", json.loads((self.tmp / "gantry_config.json.before-touch").read_text()))
        self.player.go_to_square("e4")
        self.assertAt(self.player.center(), x + 1, y - 1)

    def test_the_pretend_board_does_not_write_the_config_file(self):
        before = self.config_path.read_text()
        self.player.set_home_here()
        self.player.center()
        self.player.set_start_here()
        self.assertEqual(self.config_path.read_text(), before)
        self.assertFalse((self.tmp / "gantry_config.json.before-touch").exists())
        self.assertIn("start_mm", self.player.config, "kept in memory for this session")

    def test_make_this_spot_home_then_home_there(self):
        self.player.save_to_file = True
        self.player.set_home_here()                       # in the true corner
        self.player.jog(10, -5)                           # ▶ 10 mm, ▼ 5 mm: the new home spot
        st = self.player.save_home_spot_here()
        spot = st["saved"]["home_spot_mm"]
        self.assertNear(spot, [10.0, 5.0])
        self.assertEqual(self.file()["home_spot_mm"], spot)
        self.player.go_to_square("d4")
        self.assertAt(self.player.return_home(), spot[0], 420.0 - spot[1])   # home is the spot now
        self.player.motors_off()                         # pushed away and back onto the spot by hand
        st = self.player.set_home_here()
        self.assertAt(st, spot[0], 420.0 - spot[1])
        saved = json.loads(self.state.read_text())
        self.assertTrue(saved["trusted"])
        self.assertAlmostEqual(saved["y"], 420.0 - spot[1], delta=self.STEP)

    def test_motors_off_forgets_the_position_and_status_reports_it(self):
        self.player.set_home_here()
        self.player.center()
        st = self.player.status()
        self.assertTrue(st["remembered"], st["memory"])
        st = self.player.motors_off()
        self.assertFalse(st["remembered"])
        self.assertIn("M18", st["memory"])

    def test_a_restarted_board_is_put_back_on_connect(self):
        self.player.set_home_here()
        self.player.go_to_square("c3")
        self.player.close()                              # the pretend board is gone: a fresh one is a restarted ESP32
        st = self.player.status()
        self.assertTrue(st["homed"])
        self.assertAt(st, *self.map.square_mm("c3"))
        self.assertIn("restored", st["restore"])

    # test moves (the Gantry screen's Test move)

    def test_trace_path_ends_on_the_target_with_the_magnet_off(self):
        self.player.config["servo_settle_s"] = 0.0
        self.player.set_home_here()
        st = self.player.test_move("e2e4", carry=False)
        self.assertEqual((st["uci"], st["carry"]), ("e2e4", False))
        self.assertAt(st, *self.map.square_mm("e4"))
        self.assertFalse(st["magnet"])
        self.assertEqual(st["servo"], "down")

    def test_run_move_carries_and_ends_safe_at_the_start(self):
        self.player.config["servo_settle_s"] = 0.05   # not 0: the driver's plan only raises the arm (M7) with a settle
        self.player.set_home_here()
        st = self.player.test_move("g1f3", carry=True, occupied="start")
        self.assertEqual((st["uci"], st["carry"]), ("g1f3", True))
        self.assertFalse(st["magnet"])
        self.assertEqual(st["servo"], "down")
        self.assertAt(st, *self.map.square_mm("f3"))           # no start_mm saved: it stays where it let go
        start = list(self.map.square_mm("a8"))
        self.player.config["start_mm"] = start                  # in memory only (the pretend board)
        st = self.player.test_move("e2e4", carry=True)
        self.assertFalse(st["magnet"])
        self.assertEqual(st["servo"], "down")
        self.assertAt(st, *start)                               # parked at the start after the job
        self.player.go_to_square("c3")                          # the same open link keeps working
        self.assertNotIn("start_mm", self.file(), "the pretend board never writes the config")

    def test_test_moves_need_home(self):
        for carry in (False, True):
            with self.assertRaisesRegex(pi_gantry.GantryError, "Not homed"):
                self.player.test_move("e2e4", carry=carry)
        with self.assertRaisesRegex(pi_gantry.GantryError, "not a move"):
            self.player.test_move("e2e9", carry=False)


@unittest.skipUnless(GANTRY_DIR is not None, "needs the gantry folder")
class PlanTestMoveTests(unittest.TestCase):
    """plan_test_move is pure planning: the real config (a temporary copy), no link opened."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        config = tmp / "gantry_config.json"
        config.write_text((GANTRY_DIR / "gantry_config.json").read_text())
        self.player = pi_gantry.GantryPlayer(GANTRY_DIR, "sim", config)
        self.player.config["edge_routes"] = True

    def test_e2e4_on_an_empty_board_goes_centre_corner_line_corner_centre(self):
        plan = self.player.plan_test_move("e2e4")
        self.assertIsNone(self.player.link, "no link for planning")
        self.assertEqual(plan["uci"], "e2e4")
        self.assertEqual(plan["carries"], 1)
        self.assertEqual(plan["path_units"], [[4.0, 1.0], [4.5, 1.5], [4.5, 2.5], [4.0, 3.0]])
        self.assertEqual(plan["paths"], [plan["path_units"]])
        self.assertIn("e2", plan["notes"][0])

    def test_the_starting_position_routes_between_the_pieces(self):
        plan = self.player.plan_test_move("g1f3", occupied="start")
        self.assertGreaterEqual(plan["carries"], 1)
        path = plan["paths"][-1]
        self.assertEqual((path[0], path[-1]), ([6.0, 0.0], [5.0, 2.0]))
        self.assertTrue(all(p[0] % 1 == 0.5 or p[1] % 1 == 0.5 for p in path[1:-1]), "along the lines between squares")
        with self.assertRaisesRegex(pi_gantry.GantryError, "e7"):
            self.player.plan_test_move("e2e7", occupied="start")   # e7 is taken in the starting position
        self.assertEqual(self.player.plan_test_move("e2e7")["carries"], 1, "fine on an empty board")

    def test_squares_are_checked(self):
        for uci in ("e2e9", "i2e4", "e2", "e2e2", "", "e2-e4"):
            with self.assertRaises(pi_gantry.GantryError):
                self.player.plan_test_move(uci)


if __name__ == "__main__":
    unittest.main()
