"""The touch app's Gantry screen: reachable between games only, every action in the worker with the buttons
disabled meanwhile, the jog pad as seen from White's side, the gantry's words on the screen, and Back letting the
port go. First without a display (tkinter mocked, a fake gantry), then, when a display and the pretend board are
there, once for real: a Tk window driving the pretend board."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import pi_touch
from pi_touch import TouchApp, TouchSession
from pi_gantry import GantryError

try:
    from .test_pi_touch_app import HeadlessApp, ScreenApp
    from .test_pi_gantry import GANTRY_DIR, pretend_board_builds
except ImportError:   # run from inside test/
    from test_pi_touch_app import HeadlessApp, ScreenApp
    from test_pi_gantry import GANTRY_DIR, pretend_board_builds


class FakeControl:
    """Stands in for pi_gantry.GantryPlayer's manual controls: records the calls, reports a spot."""

    def __init__(self, fail=None):
        self.calls, self.fail, self.closed = [], dict(fail or {}), False
        self.x, self.y, self.homed = 0.0, 420.0, False

    def _done(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise GantryError(self.fail[name])
        return {"x": self.x, "y": self.y, "homed": self.homed, "state": "Idle", "reason": "", "magnet": False,
                "servo": "down", "remembered": self.homed, "memory": "position remembered" if self.homed else
                "position not remembered", "restore": "", "units": (0.0, 7.0) if self.homed else None}

    def status(self):
        return self._done("status")

    def jog(self, dx, dy):
        self.x, self.y = self.x + dx, self.y + dy
        return self._done("jog", dx, dy)

    def center(self):
        self.x, self.y = 42.0, 420.0
        return self._done("center")

    def go_to_square(self, square):
        return self._done("go_to_square", square)

    def set_home_here(self):
        self.homed = True
        return self._done("set_home_here")

    def save_home_spot_here(self):
        return dict(self._done("save_home_spot_here"), saved={"home_spot_mm": [10.0, 5.0]})

    def set_start_here(self):
        return dict(self._done("set_start_here"), saved={"start_mm": [42.0, 420.0]})

    def return_home(self):
        return self._done("return_home")

    def motors_off(self):
        return self._done("motors_off")

    def arm(self, up):
        self.calls.append(("arm", up))
        return dict(self._done("arm_up" if up else "arm_down"), servo="up" if up else "down")

    def square_size_mm(self):
        return 53.0, 53.0

    def close(self):
        self.closed = True


class HeadlessGantryScreenTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = mock.patch.object(pi_touch, "tk")      # the screen's widgets are mocks
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, cls=HeadlessApp):
        app = cls(Path(self.directory.name), [], [])
        self.addCleanup(app.executor.shutdown, True)
        return app

    def open_with(self, app, control):
        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": "sim"}), \
                mock.patch.object(pi_touch, "GantryPlayer", mock.Mock(from_env=lambda: control)):
            app.open_gantry_panel()
        app.poll_results()

    def test_the_gantry_button_is_on_the_menus_between_games(self):
        app = self.app(ScreenApp)
        app.welcome_screen = True
        app.render()
        self.assertIn("Gantry", app.buttons)
        app.welcome_screen = False
        app.render()
        self.assertIn("Gantry", app.buttons, "the level choice")
        app.session = TouchSession("beginner")
        app.session.phase = "human"
        app.render()
        self.assertNotIn("Gantry", app.buttons, "not during a game")
        app.open_gantry_panel()
        self.assertIsNone(app.gantry_screen, "not reachable mid-game")
        app.session.phase = "finished"
        app.render()
        self.assertIn("Gantry", app.buttons, "game over")

    def test_actions_run_in_the_worker_and_reach_the_screen(self):
        app = self.app()
        control = FakeControl()
        self.open_with(app, control)
        self.assertIsNotNone(app.gantry_screen)
        self.assertIs(app.gantry, control)
        self.assertEqual(control.calls, [("status",)])
        self.assertFalse(app.busy)
        self.assertIn("NOT homed", app.gantry_readout_text())

        app.gantry_set_home()
        app.poll_results()
        self.assertIn("Home set", app.message)
        self.assertIn("homed", app.gantry_readout_text())

        # The jog pad as seen from White's side: ▲ is +Y (towards rank 8), ▶ is +X (towards the h-file).
        pad = {label: (files, ranks) for label, files, ranks in TouchApp.GANTRY_PAD}
        self.assertEqual(pad, {"▲": (0, 1), "▼": (0, -1), "◀": (-1, 0), "▶": (1, 0)})
        app.gantry_jog(*pad["▼"])
        app.poll_results()
        self.assertEqual(control.calls[-1], ("jog", 0.0, -5.0))   # 5 mm is the default step
        app.set_gantry_step("1")
        app.gantry_jog(*pad["◀"])
        app.poll_results()
        self.assertEqual(control.calls[-1], ("jog", -1.0, 0.0))
        app.set_gantry_step("square")
        app.gantry_jog(*pad["▶"])
        app.poll_results()
        self.assertEqual(control.calls[-1], ("jog", 53.0, 0.0))

        app.gantry_center()
        app.poll_results()
        self.assertEqual(control.calls[-1], ("center",))
        self.assertTrue(app.message.startswith("Centered"))

        # Tap e4 on the little board (mocked canvas: 160 x 160, so 17 px squares from 18, 12).
        left, top, step = app.gantry_board_geometry()
        app.tap_gantry_board(mock.Mock(x=left + 4.5 * step, y=top + 4.5 * step))
        app.poll_results()
        self.assertEqual(control.calls[-1], ("go_to_square", "e4"))

        for action, name in ((app.gantry_set_start, "set_start_here"), (app.gantry_return_home, "return_home"),
                             (app.gantry_motors_off, "motors_off")):
            action()
            app.poll_results()
            self.assertEqual(control.calls[-1], (name,))

        app.gantry_arm(True)
        app.poll_results()
        self.assertIn(("arm", True), control.calls)
        self.assertIn("arm up", app.gantry_readout_text())
        app.gantry_arm(False)
        app.poll_results()
        self.assertIn(("arm", False), control.calls)
        self.assertIn("arm down", app.gantry_readout_text())

    def test_one_action_at_a_time(self):
        app = self.app()
        control = FakeControl()
        self.open_with(app, control)
        with mock.patch.object(app, "submit") as submit:   # the first press is still running
            app.gantry_center()
            self.assertTrue(app.busy)
            app.gantry_jog(0, 1)
            app.gantry_go_to("e4")
            app.close_gantry_panel()
        self.assertEqual(submit.call_count, 1)
        self.assertIsNotNone(app.gantry_screen, "Back waits for the gantry to stop")

    def test_make_this_spot_home_asks_first(self):
        app = self.app()
        control = FakeControl()
        self.open_with(app, control)
        app.gantry_ask_home_spot()
        self.assertTrue(app.gantry_confirm)
        self.assertEqual(control.calls, [("status",)], "nothing saved by the first tap")
        app.gantry_cancel_home_spot()
        self.assertFalse(app.gantry_confirm)
        app.gantry_ask_home_spot()
        app.gantry_save_home_spot()
        app.poll_results()
        self.assertEqual(control.calls[-1], ("save_home_spot_here",))
        self.assertIn("10.0 mm along X and 5.0 mm along Y", app.message)

    def test_a_gantry_error_is_shown_and_the_screen_stays_usable(self):
        app = self.app()
        control = FakeControl(fail={"center": "Not homed. Put the carriage on its home spot and tap Set home here."})
        self.open_with(app, control)
        app.gantry_center()
        app.poll_results()
        self.assertEqual(app.message, "Not homed. Put the carriage on its home spot and tap Set home here.")
        self.assertEqual(app.message_color, app.RED)
        self.assertFalse(app.busy)
        app.gantry_set_home()
        app.poll_results()
        self.assertEqual(control.calls[-1], ("set_home_here",))

    def test_back_lets_the_port_go_when_the_screen_opened_it(self):
        app = self.app()
        control = FakeControl()
        self.open_with(app, control)
        app.close_gantry_panel()
        app.executor.shutdown(wait=True)
        self.assertTrue(control.closed)
        self.assertIsNone(app.gantry, "the next game opens the gantry as before")
        self.assertIsNone(app.gantry_screen)

    def test_back_keeps_the_games_gantry_open(self):
        app = self.app()
        control = FakeControl()
        app.gantry = control             # opened by the last game
        self.open_with(app, mock.Mock(side_effect=AssertionError("not opened again")))
        app.close_gantry_panel()
        app.executor.shutdown(wait=True)
        self.assertFalse(control.closed)
        self.assertIs(app.gantry, control)

    def test_without_a_gantry_the_reason_is_shown(self):
        app = self.app()
        environment = {key: value for key, value in os.environ.items() if key != "CHESS_GANTRY_PORT"}
        with mock.patch.dict(os.environ, environment, clear=True):
            app.open_gantry_panel()
        self.assertIsNotNone(app.gantry_screen)
        self.assertFalse(app.busy)
        self.assertIn("CHESS_GANTRY_PORT is not set", app.message)
        app.gantry_center()              # nothing to do, nothing breaks
        self.assertFalse(app.busy)
        app.close_gantry_panel()
        self.assertIsNone(app.gantry_screen)

        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": "/dev/ttyUSB0"}), \
                mock.patch.object(pi_touch, "GantryPlayer", mock.Mock(from_env=lambda: None)):
            app.open_gantry_panel()
        self.assertIn("could not be found", app.message)


def display_available() -> bool:
    """A Tk window can be opened (tried in a child process: without a usable display Tk may fail or hang)."""
    if os.environ.get("CHESS_SKIP_TK"):
        return False
    try:
        return subprocess.run([sys.executable, "-c", "import tkinter; r = tkinter.Tk(); r.update(); r.destroy()"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(display_available(), "needs a display for tkinter")
@unittest.skipUnless(pretend_board_builds(), "needs the gantry folder and a C++ compiler for the pretend board")
class RealScreenSmokeTest(unittest.TestCase):
    """A real Tk window and the pretend board: the buttons exist, they drive the gantry, Back closes it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config = json.loads((GANTRY_DIR / "gantry_config.json").read_text())
        config.pop("start_mm", None)
        (self.tmp / "gantry_config.json").write_text(json.dumps(config))
        env = mock.patch.dict(os.environ, {
            "CHESS_GANTRY_PORT": "sim", "CHESS_GANTRY_DIR": str(GANTRY_DIR),
            "CHESS_GANTRY_CONFIG": str(self.tmp / "gantry_config.json"), "GANTRY_SIM_SPEED": "200",
            "GANTRY_SIM_STATE": str(self.tmp / "sim-position.json"), "GANTRY_STATE_DIR": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)

    def pump(self, app, root, seconds=20.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            if not app.busy:
                return
            time.sleep(0.02)
        self.fail("the gantry action did not finish")

    def find(self, widget, text):
        for child in widget.winfo_children():
            try:
                if child.cget("text") == text:
                    return child
            except Exception:
                pass
            found = self.find(child, text)
            if found is not None:
                return found
        return None

    def test_the_screen_drives_the_pretend_board(self):
        root = pi_touch.tk.Tk()
        args = argparse.Namespace(calibration=self.tmp / "calibration", camera="usb", device=0, corners=None,
                                  windowed=True)
        app = TouchApp(root, args)
        try:
            root.update()
            self.assertIsNotNone(self.find(app.panel, "Gantry"), "the welcome screen has the Gantry button")
            app.open_gantry_panel()
            self.pump(app, root)
            screen = app.gantry_screen
            for label in ("Center gantry", "▲", "▼", "◀", "▶", "1 mm", "5 mm", "1 square", "Set home here",
                          "Make this spot home", "Set start here", "Return home", "Motors off", "Back"):
                self.assertIsNotNone(self.find(screen, label), label)
            self.assertIn("NOT homed", app.gantry_readout_label.cget("text"))
            self.find(screen, "Set home here").invoke()
            self.assertEqual(self.find(screen, "Center gantry").cget("state"), "disabled", "busy: buttons off")
            self.pump(app, root)
            self.assertEqual(self.find(screen, "Center gantry").cget("state"), "normal")
            self.find(screen, "Center gantry").invoke()
            self.pump(app, root)
            self.assertTrue(app.gantry_readout["homed"])
            self.assertAlmostEqual(app.gantry_readout["x"], 42.0, delta=0.25)
            self.assertTrue(app.gantry_message.cget("text").startswith("Centered"), app.gantry_message.cget("text"))
            self.find(screen, "▼").invoke()
            self.pump(app, root)
            self.assertAlmostEqual(app.gantry_readout["y"], 415.0, delta=0.25)
            gantry = app.gantry
            self.find(screen, "Back").invoke()
            root.update()
            self.assertIsNone(app.gantry_screen)
            app.executor.submit(lambda: None).result(timeout=10)   # the close ran
            self.assertIsNone(gantry.link)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
