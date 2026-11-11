"""The home spot: homing at a fixed spot a little off the frame's true corner (config "home_spot_mm": [dx, dy], mm
along X and along Y away from the corner), gantry_driver.set_home_here / home_spot_from_here / home_xy, for home
beside a1 and beside a8 (the link mirrors Y), checked on the pretend board as the Pi counts AND as the board itself
counts (the physical spot), and recorded by the position memory. Also update_config_file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gantry_driver as gd  # noqa: E402

HOST = Path(__file__).resolve().parent.parent / "firmware" / "host"
CONFIG = {
    "port": "sim", "baud": 115200, "square_mm": 53.0, "a1_center_mm": [42.0, 49.0], "park_columns_mm": {},
    "travel_mm": [470.0, 420.0], "travel_speed_mm_s": 120.0, "carry_speed_mm_s": 60.0, "servo": True,
    "servo_settle_s": 0.3, "home_on_start": False, "mm_per_turn": 40.0, "microsteps": 8,   # 40 steps/mm: exact
}
SPOT = [12.5, 7.25]


class PureTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GANTRY_HOME_BESIDE", None)

    def test_home_spot_mm_reads_the_config(self):
        self.assertIsNone(gd.home_spot_mm(CONFIG))
        self.assertIsNone(gd.home_spot_mm(dict(CONFIG, home_spot_mm=None)))
        self.assertIsNone(gd.home_spot_mm(dict(CONFIG, home_spot_mm=[0, 0])))
        self.assertEqual(gd.home_spot_mm(dict(CONFIG, home_spot_mm=SPOT)), (12.5, 7.25))
        for bad in ([-1, 0], [0, 500], ["x", 1], [3]):
            with self.assertRaises(gd.GantryError):
                gd.home_spot_mm(dict(CONFIG, home_spot_mm=bad))

    def test_home_xy_as_the_pi_counts(self):
        a1, a8 = dict(CONFIG, home_beside="a1"), dict(CONFIG, home_beside="a8")
        self.assertEqual(gd.home_xy(a1), (0.0, 0.0))
        self.assertEqual(gd.home_xy(a8), (0.0, 420.0))
        self.assertEqual(gd.home_xy(dict(a1, home_spot_mm=SPOT)), (12.5, 7.25))
        self.assertEqual(gd.home_xy(dict(a8, home_spot_mm=SPOT)), (12.5, 420.0 - 7.25))

    def test_home_spot_from_here(self):
        a1, a8 = dict(CONFIG, home_beside="a1"), dict(CONFIG, home_beside="a8")
        self.assertEqual(gd.home_spot_from_here({"homed": True, "x": 12.5, "y": 7.25}, a1), SPOT)
        self.assertEqual(gd.home_spot_from_here({"homed": True, "x": 12.5, "y": 412.75}, a8), SPOT)
        self.assertEqual(gd.home_spot_from_here({"homed": True, "x": -0.2, "y": 420.1}, a8), [0.0, 0.0])
        with self.assertRaises(gd.GantryError):
            gd.home_spot_from_here({"homed": False, "x": 1.0, "y": 1.0}, a1)
        with self.assertRaises(gd.GantryError):
            gd.home_spot_from_here({"homed": True, "x": -5.0, "y": 1.0}, a1)

    def test_a2_home_is_the_square_centre_for_both_motor_directions(self):
        for beside in ("a1", "a8"):
            config = dict(CONFIG, home_beside=beside, home_square="a2", swap_xy=False)
            with self.subTest(beside=beside):
                self.assertEqual(gd.home_xy(config), (42.0, 102.0))
                self.assertEqual(gd.home_spot_mm(config), (42.0, 318.0 if beside == "a8" else 102.0))

    def test_square_home_tracks_calibration_and_rejects_bad_references(self):
        config = dict(CONFIG, home_square="a2", a1_center_mm=[44.0, 50.0], home_spot_mm=SPOT)
        self.assertEqual(gd.home_xy(config), (44.0, 103.0))
        for square in ("a9", "a2 pawn", "../a2"):
            with self.subTest(square=square), self.assertRaises(gd.GantryError):
                gd.home_xy(dict(CONFIG, home_square=square))
        with self.assertRaises(gd.GantryError):
            gd.home_xy(dict(CONFIG, home_square="a2", a1_center_mm=[500.0, 49.0]))

    def test_update_config_file_keeps_the_old_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry_config.json"
            path.write_text(json.dumps({"_comment": "kept", "square_mm": 53.0}))
            gd.update_config_file(path, {"start_mm": [42.0, 420.0]})
            gd.update_config_file(path, {"home_spot_mm": SPOT})
            data = json.loads(path.read_text())
            self.assertEqual(data, {"_comment": "kept", "square_mm": 53.0, "start_mm": [42.0, 420.0], "home_spot_mm": SPOT})
            backup = json.loads((Path(tmp) / "gantry_config.json.before-touch").read_text())
            self.assertEqual(backup, {"_comment": "kept", "square_mm": 53.0}, "the backup is the file before the first change")


class PretendBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-s", "-C", str(HOST), "virtual_gantry"], check=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "sim-position.json"
        env = mock.patch.dict(os.environ, {"GANTRY_SIM_STATE": str(self.state), "GANTRY_SIM_SPEED": "200",
                                           "GANTRY_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GANTRY_HOME_BESIDE", None)
        drain = gd.Gantry.drain
        quick = mock.patch.object(gd.Gantry, "drain", lambda g, seconds: drain(g, min(seconds, 0.2)))
        quick.start()
        self.addCleanup(quick.stop)

    def open(self, config):
        link = gd.Link("sim", 115200)
        self.addCleanup(link.close)
        link.flip_y = gd.y_flip_mm(config)
        gantry = gd.Gantry(link)
        gantry.set_motion(config)
        return link, gantry

    def physical(self, gantry):
        """Where the board itself counts the carriage (no Y mirror): the physical spot from the true corner."""
        link = gantry.link
        flip, memory = link.flip_y, link.memory
        link.flip_y, link.memory = None, None     # unmirrored, and not recorded as the Pi's count
        try:
            st = gantry.status()
        finally:
            link.flip_y, link.memory = flip, memory
        return st["x"], st["y"]

    def check_spot(self, beside: str, pi_xy):
        config = dict(CONFIG, home_beside=beside, home_spot_mm=SPOT)
        link, gantry = self.open(config)
        st = gd.set_home_here(link, config)          # a Link works as well as a Gantry
        self.assertTrue(st["homed"])
        self.assertAlmostEqual(st["x"], pi_xy[0], places=2)
        self.assertAlmostEqual(st["y"], pi_xy[1], places=2)
        px, py = self.physical(gantry)
        self.assertAlmostEqual(px, SPOT[0], places=2, msg="the board counts dx from the true corner")
        self.assertAlmostEqual(py, SPOT[1], places=2, msg="the board counts dy from the true corner")
        saved = json.loads(self.state.read_text())
        self.assertTrue(saved["trusted"])
        self.assertFalse(saved["moving"])
        self.assertEqual(saved["home_beside"], beside)
        self.assertAlmostEqual(saved["x"], pi_xy[0], places=2)
        self.assertAlmostEqual(saved["y"], pi_xy[1], places=2)
        # The spot read back from here is the one configured.
        self.assertEqual(gd.home_spot_from_here(gantry.status(), config), SPOT)

    def test_home_beside_a1(self):
        self.check_spot("a1", (12.5, 7.25))

    def test_home_beside_a8_mirrors_y(self):
        self.check_spot("a8", (12.5, 420.0 - 7.25))

    def test_without_a_spot_home_is_the_corner(self):
        config = dict(CONFIG, home_beside="a8")
        link, gantry = self.open(config)
        st = gd.set_home_here(gantry, config)
        self.assertEqual((round(st["x"], 3), round(st["y"], 3)), (0.0, 420.0))
        self.assertEqual(self.physical(gantry), (0.0, 0.0))

    def test_a2_home_sets_coordinates_without_a_motion_command(self):
        config = dict(CONFIG, home_beside="a8", home_square="a2", swap_xy=False)
        link, gantry = self.open(config)
        with mock.patch.object(gantry, "command", wraps=gantry.command) as command:
            st = gd.set_home_here(gantry, config)
        self.assertEqual([call.args[0] for call in command.call_args_list], ["$HM", "G92 X42.000 Y102.000"])
        self.assertEqual((st["x"], st["y"]), (42.0, 102.0))
        self.assertEqual(self.physical(gantry), (42.0, 318.0))

    def test_a_spot_measured_from_the_corner_then_used(self):
        """Home in the true corner, jog to the new home spot, save it: homing there later gives the same count."""
        config = dict(CONFIG, home_beside="a8")
        link, gantry = self.open(config)
        gd.set_home_here(gantry, config)
        gantry.command("G1 X12.5 Y412.75 F6000")
        gantry.wait_idle(30, "the jog")
        spot = gd.home_spot_from_here(gantry.status(), config)
        self.assertEqual(spot, SPOT)
        gantry.command("M18")                       # the person pushes it around, then back onto the spot
        st = gd.set_home_here(gantry, dict(config, home_spot_mm=spot))
        self.assertEqual((round(st["x"], 2), round(st["y"], 2)), (12.5, 412.75))
        self.assertTrue(json.loads(self.state.read_text())["trusted"], "a fresh home is trusted again after M18")


if __name__ == "__main__":
    unittest.main()
