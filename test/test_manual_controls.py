"""Manual command validation and routing without hardware or motor simulation."""
import unittest
from unittest.mock import Mock, patch

from manual_controls import valid_manual
from pi_gantry import GantryError
from touch_companion import CompanionBridge
from test import test_gantry_manual_home_guard as guard
from test import test_laptop_setup as laptop


class ManualValidationTests(unittest.TestCase):
    def test_supported_commands_and_jog_steps(self):
        for op in ('connect', 'disconnect', 'status', 'unlock', 'forget', 'home', 'bump_home',
                   'motors_off', 'return_home', 'start', 'save_start', 'arm_up', 'arm_down', 'magnet_on', 'magnet_off'):
            self.assertTrue(valid_manual({'op': op}))
        for step in (1, 5, 10, 50, 'square'):
            self.assertTrue(valid_manual({'op': 'jog', 'dx': -1, 'dy': 1, 'step': step}))
        for place in ('e4', 'b:3', '7.5,2'):
            self.assertTrue(valid_manual({'op': 'goto', 'place': place}))

    def test_rejects_unbounded_or_malformed_motion(self):
        for command in ({'op': 'jog', 'dx': True, 'dy': 0, 'step': 10},
                        {'op': 'jog', 'dx': 1, 'dy': 0, 'step': 1000},
                        {'op': 'raw', 'line': 'M3\nG1 X100'},
                        {'op': 'raw', 'line': ''}, {'op': 'goto', 'place': 'z9'},
                        {'op': 'home', 'extra': 1},
                        {'op': 'settings', 'travel_speed_mm_s': float('nan'), 'carry_speed_mm_s': 25, 'accel_mm_s2': 400}):
            self.assertFalse(valid_manual(command))

    def test_bridge_rejects_manual_commands_during_game_or_busy(self):
        bridge = CompanionBridge()
        bridge.publish({'manual_enabled': False, 'busy': False})
        self.assertFalse(bridge.submit('manual', bridge.snapshot()['revision'], {'op': 'arm_up'}))
        bridge.publish({'manual_enabled': True, 'busy': True})
        self.assertFalse(bridge.submit('manual', bridge.snapshot()['revision'], {'op': 'arm_up'}))
        bridge.publish({'manual_enabled': True, 'busy': False})
        self.assertTrue(bridge.submit('manual', bridge.snapshot()['revision'], {'op': 'arm_up'}))
        self.assertFalse(bridge.submit('manual', bridge.snapshot()['revision'], {'op': 'arm_up'}))


class ManualDriverTests(unittest.TestCase):
    setUp = guard.ManualHomeGuardTests.setUp

    def test_square_and_diagonal_jogs_use_measured_square_size(self):
        with patch.object(self.player, 'square_size_mm', return_value=(53, 52)) as size, \
                patch.object(self.player, 'jog', return_value={}) as jog:
            self.player.manual_command({'op': 'jog', 'dx': 1, 'dy': -1, 'step': 'square'})
            size.assert_called_once()
            jog.assert_called_once_with(53, -52)

    def test_absolute_and_raw_motion_require_manual_home(self):
        for command in ({'op': 'goto', 'place': 'e4'}, {'op': 'raw', 'line': 'G1 X100 F1200'}):
            with self.assertRaisesRegex(GantryError, 'Set home'):
                self.player.manual_command(command)
        self.board.command.assert_not_called()

    def test_explicit_arm_commands_and_position_refresh_are_distinct(self):
        with patch.object(self.player, 'arm', return_value={}) as arm, \
                patch.object(self.player, 'status', return_value={}) as status:
            self.player.manual_command({'op': 'status'})
            arm.assert_not_called()
            self.player.manual_command({'op': 'arm_up'})
            arm.assert_called_once_with(True)
            status.assert_called_once()

    def test_disconnect_never_rehomes_or_moves(self):
        self.player.link.close = Mock()
        self.player.manual_command({'op': 'disconnect'})
        self.board.command.assert_not_called()
        self.assertIsNone(self.player.link)
        self.assertFalse(self.player.manual_home_ready)


class ManualUITests(unittest.TestCase):
    setUp = laptop.LaptopSetupTests.setUp

    def test_worker_receives_exact_command_only_from_manual_panel(self):
        self.app.gantry = Mock()
        self.app.gantry.config = {}
        self.app.gantry.manual_command.return_value = {'response': 'done'}
        with patch.object(self.app, 'run_gantry_action') as run:
            self.app.companion_manual({'op': 'arm_up'})
            run.assert_not_called()
            self.app.gantry_screen = Mock()
            self.app.companion_manual({'op': 'arm_up'})
            run.assert_called_once()
            run.call_args.args[1](self.app.gantry)
            self.app.gantry.manual_command.assert_called_once_with({'op': 'arm_up'})
