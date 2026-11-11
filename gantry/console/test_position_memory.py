"""Home once, in the console: Connect puts a restarted board back at its remembered spot, a stall untrusts it,
the page's Forget position route, and the status JSON's "position_memory". The pretend board starts not homed
(like a restarted ESP32) and only remembers when GANTRY_SIM_STATE names a file: here, a temporary one."""
from pathlib import Path
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console_server as cs  # noqa: E402
cs.gd.DEFAULT_CONFIG = cs.gd.HERE / "sketch5_test_config.json"

FAST = {"servo_settle": 0.02, "grab_settle": 0.02, "release_settle": 0.02}


def wait(console, seconds=60.0):
    import time
    deadline = time.monotonic() + seconds
    while console.task or console.waiting:
        if time.monotonic() > deadline:
            raise AssertionError(f"still busy: {console.task}")
        time.sleep(0.05)


class ConsoleMemoryTests(unittest.TestCase):
    def setUp(self):
        if not cs.gd.VIRTUAL_GANTRY.exists():
            self.skipTest("build the pretend board first: make -C gantry/firmware/host virtual_gantry")
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "sim-position.json"
        self.env = mock.patch.dict(os.environ, {"GANTRY_SIM_STATE": str(self.state), "GANTRY_HOME_BESIDE": "a1",
                                                "GANTRY_STATE_DIR": self.tmp.name})
        self.env.start()
        self.settings = mock.patch.object(cs, "SETTINGS_FILE", Path(self.tmp.name) / "settings.json")
        self.settings.start()
        config = dict(cs.gd.load_config(cs.gd.DEFAULT_CONFIG), mm_per_turn=[40.0, 40.0], microsteps=8)   # $100 = 40
        self.c = cs.Console(config, sim_speed=40)
        self.c.settings.update(FAST, travel_speed=60.0, carry_speed=40.0)

    def tearDown(self):
        wait(self.c)
        if self.c.link is not None:
            self.c.disconnect()
        self.settings.stop()
        self.env.stop()
        self.tmp.cleanup()

    def connect(self):
        self.c.connect("sim")
        wait(self.c)
        self.assertIsNotNone(self.c.link, [e["text"] for e in self.c.log])

    def texts(self):
        return [e["text"] for e in self.c.log]

    def saved(self):
        return json.loads(self.state.read_text())

    def test_connect_puts_a_restarted_board_back(self):
        self.connect()
        self.assertFalse(self.c.view(0)["position_memory"]["remembered"])
        self.c.home_manual()
        wait(self.c)
        self.c.goto("e4")
        wait(self.c)
        self.c.query_status()
        x, y = self.c.map.square_mm("e4")
        self.assertEqual((self.saved()["x"], self.saved()["y"], self.saved()["moving"]), (round(x, 3), round(y, 3), False))
        memory = self.c.view(0)["position_memory"]
        self.assertTrue(memory["remembered"])
        self.assertTrue(memory["text"].startswith("position remembered (saved "))
        self.c.disconnect()
        self.connect()                                  # a fresh pretend board: it starts not homed
        st = self.c.query_status()
        self.assertTrue(st["homed"])
        self.assertFalse(self.c.position_lost)
        self.assertAlmostEqual(st["x"], x, delta=0.03)
        self.assertAlmostEqual(st["y"], y, delta=0.03)
        self.assertTrue(any(t.startswith("Position restored from") and "no homing needed" in t for t in self.texts()))
        self.c.require(homed=True)                      # moves are allowed at once

    def test_power_lost_mid_move_asks_for_a_home(self):
        self.connect()
        self.c.home_manual()
        wait(self.c)
        self.c.disconnect()
        self.state.write_text(json.dumps(dict(self.saved(), moving=True)))
        self.connect()
        self.assertFalse(self.c.query_status()["homed"])
        self.assertIn("The power went off during a move, so the position is unknown: home once more.", self.texts())
        with self.assertRaises(cs.gd.GantryError):
            self.c.require(homed=True)

    def test_a_stall_untrusts_the_position(self):
        self.connect()
        self.c.home_manual()
        wait(self.c)
        self.c.query_status()
        self.c.tests["ladder"] = {"axis": "x", "at": 80, "ok": [60], "done": False}
        self.c.stalled()
        self.assertTrue(self.c.position_lost)
        self.assertFalse(self.saved()["trusted"])
        self.assertIn("stall", self.saved()["reason"])
        self.c.query_status()                           # the board still says homed: stays untrusted
        self.assertFalse(self.saved()["trusted"])

    def test_forget_position_route(self):
        self.connect()
        self.c.home_manual()
        wait(self.c)
        self.c.query_status()
        self.assertTrue(self.saved()["trusted"])
        cs.Handler.console = self.c
        server = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/forget-position", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                answer = json.loads(r.read())
            self.assertFalse(answer["remembered"])
            self.assertFalse(self.saved()["trusted"])
            self.c.query_status()                       # still homed on the board: the file stays forgotten
            self.assertFalse(self.saved()["trusted"])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as r:
                state = json.loads(r.read())
            self.assertFalse(state["position_memory"]["remembered"])
            self.assertIn("position not remembered", state["position_memory"]["text"])
        finally:
            server.shutdown()
            server.server_close()
        self.c.disconnect()
        self.connect()
        self.assertFalse(self.c.query_status()["homed"])   # nothing put back after the restart
        self.assertTrue(any("not trusted" in t for t in self.texts()))


if __name__ == "__main__":
    unittest.main()



class ConsoleHomeSpotTests(unittest.TestCase):
    """Set home here at a spot off the corner (config home_spot_mm): the count says corner + offset, the same spot
    gantry_driver.home_xy gives the touch screen, and Return home comes back to it."""

    def test_home_spot_offsets_the_count_and_return_home(self):
        import time
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (mock.patch.dict(os.environ, {"GANTRY_SIM_STATE": str(Path(tmp.name) / "sim.json"), "GANTRY_STATE_DIR": tmp.name}),
                      mock.patch.object(cs, "SETTINGS_FILE", Path(tmp.name) / "settings.json")):
            patch.start()
            self.addCleanup(patch.stop)
        config = dict(cs.gd.load_config(cs.gd.HERE / "gantry_config.json"), home_square=None, home_spot_mm=[4.0, 6.0], start_mm=None)
        c = cs.Console(config, sim_speed=40)
        c.connect("sim")
        wait(c)
        try:
            c.home_manual()
            wait(c)
            want = c.home_xy()
            self.assertEqual(want, cs.gd.home_xy(config) if c.link.flip_y is not None else (4.0, 6.0))
            st = c.query_status()
            self.assertAlmostEqual(st["x"], want[0], delta=0.25)    # 41 mm per turn: 0.205 mm steps
            self.assertAlmostEqual(st["y"], want[1], delta=0.25)
            c.jog(20, 0)
            wait(c)
            c.return_home()
            wait(c)
            st = c.query_status()
            self.assertAlmostEqual(st["x"], want[0], delta=0.3)
            self.assertAlmostEqual(st["y"], want[1], delta=0.3)
        finally:
            wait(c)
            c.disconnect()

    def test_a2_manual_home_stays_at_the_pawn_even_with_a_saved_start(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "GANTRY_SIM_STATE": str(Path(tmp) / "sim.json"), "GANTRY_STATE_DIR": tmp,
                "GANTRY_HOME_BESIDE": "a8", "GANTRY_SWAP_XY": "0"}), mock.patch.object(
                cs, "SETTINGS_FILE", Path(tmp) / "settings.json"):
            config = dict(cs.gd.load_config(cs.gd.HERE / "gantry_config.json"),
                          home_square="a2", swap_xy=False, start_mm=[100.0, 200.0])
            c = cs.Console(config, sim_speed=40)
            c.connect("sim")
            wait(c)
            try:
                since = c.log_id
                c.home_manual()
                self.assertIsNone(c.task, "manual home must not drive to a different saved start")
                st = c.query_status()
                want = c.map.square_mm("a2")
                self.assertAlmostEqual(st["x"], want[0], delta=0.25)
                self.assertAlmostEqual(st["y"], want[1], delta=0.25)
                view = c.view(since)
                self.assertEqual(view["geometry"]["home_square"], "a2")
                self.assertEqual(tuple(view["geometry"]["home_xy"]), want)
                self.assertFalse(view["geometry"]["swap_xy"])
                sent = [e["text"] for e in view["log"] if e["kind"] == "send"]
                self.assertFalse(any(line.startswith(("G0 ", "G1 ")) for line in sent), sent)
            finally:
                wait(c)
                c.disconnect()
