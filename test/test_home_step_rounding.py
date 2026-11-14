"""Regression for the Pi's recorded e7 full-step rounding; no motor simulator."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pi_gantry import load_modules

gd, _ = load_modules(Path(__file__).resolve().parents[1] / 'gantry/pi')


class HomeStepRoundingTests(unittest.TestCase):
    def setUp(self):
        self.config = gd.load_config(Path(__file__).resolve().parents[1] / 'gantry/pi/gantry_config.json')
        self.config.update(home_square='e7', home_beside='a1', swap_xy=False)
        self.gantry = gd.Gantry.__new__(gd.Gantry)
        self.gantry.link = SimpleNamespace(steps_per_mm=5.17063)
        self.gantry.command = Mock()
        self.state = {'homed': True, 'x': 253.934, 'y': 367.073, 'state': 'Idle'}
        self.gantry.status = Mock(side_effect=lambda: self.state.copy())

    def test_real_e7_report_is_accepted_without_any_motion_command(self):
        result = gd.set_home_here(self.gantry, self.config)
        self.assertTrue(result['homed'])
        self.assertEqual([call.args[0] for call in self.gantry.command.call_args_list],
                         ['$HM', 'G92 X254.000 Y367.000'])

    def test_larger_coordinate_error_still_fails(self):
        self.state['y'] = 367.2
        with self.assertRaisesRegex(gd.GantryError, 'Homing here did not take'):
            gd.set_home_here(self.gantry, self.config)

    def test_firmware_must_still_confirm_homed(self):
        self.state.update(x=254., y=367., homed=False)
        with self.assertRaises(gd.GantryError):
            gd.set_home_here(self.gantry, self.config)

    def test_finer_step_resolution_keeps_tighter_check(self):
        self.gantry.link.steps_per_mm = 40.
        with self.assertRaises(gd.GantryError):
            gd.set_home_here(self.gantry, self.config)
