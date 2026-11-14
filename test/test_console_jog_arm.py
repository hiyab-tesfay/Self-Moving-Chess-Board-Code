"""Manual jog regressions with a stub link; no motors or motor simulator."""
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gantry/console'))
import console_server as cs


class InlineThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target
    def start(self):
        self.target()


class ConsoleJogArmTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.object(cs, 'SETTINGS_FILE', Path(folder.name) / 'settings.json'), patch.object(cs.threading, 'Thread'):
            self.c = cs.Console(cs.gd.load_config(cs.gd.HERE / 'gantry_config.json'))
        self.c.status = {'x': 328., 'y': 259., 'homed': False, 'state': 'Idle', 'servo': 'up', 'magnet': True}
        self.c.require = Mock(side_effect=lambda **_kwargs: self.c.status.copy())
        self.c.cmd = Mock()
        self.c.move_to = Mock()
        self.c._safe = Mock()
        self.c.link = Mock()
        self.c.link.batch.side_effect = contextlib.nullcontext
        patcher = patch.object(cs.threading, 'Thread', InlineThread)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_left_unhomed_keeps_arm_and_magnet_and_uses_slow_jog(self):
        self.c.jog(-50, 0)
        self.c.move_to.assert_called_once_with(278., 259., 15., 'jog', carrying=True)
        self.c.cmd.assert_not_called()
        self.c._safe.assert_not_called()
        self.assertIsNone(self.c.task)

    def test_down_unhomed_is_also_allowed_without_changing_home(self):
        self.c.jog(0, -10)
        self.c.move_to.assert_called_once_with(328., 249., 15., 'jog', carrying=True)
        self.assertFalse(self.c.status['homed'])
        self.c._safe.assert_not_called()

    def test_rejected_boundary_jog_does_not_lower_arm_or_release_magnet(self):
        self.c.status['x'] = 0.
        self.c.jog(-50, 0)
        self.c.move_to.assert_not_called()
        self.c.cmd.assert_not_called()
        self.c._safe.assert_not_called()
        self.assertTrue(any('warn' == entry['kind'] for entry in self.c.log))

    def test_real_motion_fault_still_makes_hardware_safe(self):
        self.c.move_to.side_effect = cs.gd.GantryError('controller refused the move')
        self.c.jog(-10, 0)
        self.c._safe.assert_called_once()
