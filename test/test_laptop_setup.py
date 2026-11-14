"""Laptop setup shares the Tk action queue; no hardware is operated."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pi_touch import CalibrationFlow
from test.test_pi_touch_app import HeadlessApp
from touch_companion import CompanionBridge


class LaptopSetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = HeadlessApp(Path(folder.name), [], [])
        self.app.args.standalone = True
        self.app.companion = SimpleNamespace(bridge=CompanionBridge())
        self.bridge = self.app.companion.bridge

    def send(self, control):
        self.app.publish_companion()
        self.assertTrue(self.bridge.submit('control', self.bridge.snapshot()['revision'], control))
        self.app.dispatch_companion()

    def test_menu_controls_run_only_when_explicitly_requested(self):
        with patch.object(self.app, 'start_game') as start, patch.object(self.app, 'open_gantry_panel') as home:
            self.app.publish_companion()
            offered = {item['id'] for item in self.bridge.snapshot()['controls']}
            self.assertEqual(offered, {'gantry', 'calibrate', 'start_game'})
            start.assert_not_called()
            home.assert_not_called()
            self.send('start_game')
            start.assert_called_once()
            home.assert_not_called()

    def test_homing_is_an_explicit_separate_action(self):
        self.app.gantry_screen = Mock()
        self.app.gantry = Mock()
        self.app.gantry_readout = {'homed': False}
        with patch.object(self.app, 'gantry_set_home') as home:
            self.app.publish_companion()
            home.assert_not_called()
            self.assertNotIn('jog_up', {c['id'] for c in self.bridge.snapshot()['controls']})
            self.send('home_e7')
            home.assert_called_once()

    def test_busy_and_stale_or_unoffered_controls_are_rejected(self):
        self.app.publish_companion()
        revision = self.bridge.snapshot()['revision']
        self.assertFalse(self.bridge.submit('control', revision, 'home_e7'))
        self.assertFalse(self.bridge.submit('control', revision - 1, 'start_game'))
        self.app.busy = True
        self.app.publish_companion()
        self.assertFalse(self.bridge.submit('control', self.bridge.snapshot()['revision'], 'start_game'))

    def test_calibration_and_normalized_photo_corners_use_the_same_handler(self):
        self.app.calibration_flow = CalibrationFlow()
        self.app.calibration_flow.stage = 'empty_corners'
        self.app.preview_geometry = (10, 20, .5)
        self.app.companion_photo = {'clickable': True, 'width': 640, 'height': 480}
        self.app.publish_companion()
        revision = self.bridge.snapshot()['revision']
        for invalid in ([True, .2], [-.1, .2], [1, .2], [float('nan'), .2], [.5], 'point'):
            self.assertFalse(self.bridge.submit('corner', revision, invalid))
        self.assertTrue(self.bridge.submit('corner', revision, [.25, .5]))
        with patch.object(self.app, 'tap_calibration_corner') as corner:
            self.app.dispatch_companion()
            event = corner.call_args.args[0]
            self.assertEqual((event.x, event.y), (90, 140))

    def test_photo_payload_is_separate_from_frequently_polled_state(self):
        photo_id = self.bridge.publish_photo(b'new photo')
        self.assertEqual(photo_id, self.bridge.publish_photo(b'new photo'))
        self.assertEqual(self.bridge.read_photo(), b'new photo')
        self.assertNotIn('new photo', str(self.bridge.snapshot()))
