"""Home once: the remembered position (gantry_driver.PositionMemory), put back on a restarted board, the
"moving" and "untrusted" rules, parking at the start after a job, and keeping the ESP32 running on connect.

The pretend board starts not homed, like a restarted ESP32, so a fresh one is a restarted board. It only uses a
remembered position when GANTRY_SIM_STATE names a file; these tests give it one in a temporary directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gantry_driver as gd  # noqa: E402

HOST = Path(__file__).resolve().parent.parent / "firmware" / "host"
CONFIG = {
    "port": "sim", "baud": 115200, "square_mm": 50.0, "a1_center_mm": [75.0, 40.0],
    "park_columns_mm": {"w": [43.0, 15.0], "b": [507.0, 535.0]}, "park_rows_mm": None,
    "travel_mm": [450.0, 400.0], "travel_speed_mm_s": 120.0, "carry_speed_mm_s": 60.0,
    "grab_settle_s": 0.3, "release_settle_s": 0.2, "servo": True, "servo_settle_s": 0.3, "home_on_start": False,
    "mm_per_turn": 40.0, "microsteps": 8,   # 40 steps/mm: positions read back exactly
    "home_beside": "a1",
}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-s", "-C", str(HOST), "virtual_gantry"], check=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "sim-position.json"
        self.env = mock.patch.dict(os.environ, {"GANTRY_SIM_STATE": str(self.state), "GANTRY_SIM_SPEED": "200",
                                                "GANTRY_HOME_BESIDE": "a1", "GANTRY_STATE_DIR": self.tmp.name})
        self.env.start()
        self.links = []
        drain = gd.Gantry.drain    # the pretend board prints its banner at once: no need to listen a whole second
        self.quick = mock.patch.object(gd.Gantry, "drain", lambda g, seconds: drain(g, min(seconds, 0.2)))
        self.quick.start()
        self.addCleanup(self.quick.stop)

    def tearDown(self):
        for link in self.links:
            try:
                link.close()
            except Exception:
                pass
        self.env.stop()
        self.tmp.cleanup()

    def open(self, config=CONFIG):
        """A fresh pretend board (not homed, like a restarted ESP32), configured like run_job does."""
        link = gd.Link("sim", 115200)
        self.links.append(link)
        link.flip_y = gd.y_flip_mm(config)
        sent = []
        real = link.write
        link.write = lambda text: (sent.append(text), real(text))[1]
        gantry = gd.Gantry(link)
        gantry.set_motion(config)
        return link, gantry, sent

    def saved(self) -> dict:
        return json.loads(self.state.read_text())

    def home_and_go(self, x, y, config=CONFIG):
        link, gantry, _ = self.open(config)
        gantry.command("$HM")
        gantry.command(f"G1 X{x:.3f} Y{y:.3f} F6000")
        gantry.wait_idle(30, "the move")
        link.close()
        return self.saved()


class MemoryFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "deep" / "position.json"
        self.memory = gd.PositionMemory(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_written_atomically_and_only_when_it_changed(self):
        self.assertTrue(self.memory.settled(10.0, 20.0, 40.0, [450, 400], "a1"))
        data = json.loads(self.path.read_text())
        self.assertEqual((data["x"], data["y"], data["steps_per_mm"], data["home_beside"]), (10.0, 20.0, 40.0, "a1"))
        self.assertEqual((data["moving"], data["trusted"]), (False, True))
        self.assertIn("updated", data)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["position.json"])   # no .tmp left behind
        self.assertFalse(self.memory.settled(10.005, 20.0, 40.0, [450, 400], "a1"))          # under 0.01 mm: no write
        self.assertTrue(self.memory.settled(10.02, 20.0, 40.0, [450, 400], "a1"))
        self.memory.moving("G1")
        self.assertTrue(json.loads(self.path.read_text())["moving"])
        self.assertTrue(self.memory.settled(10.02, 20.0, 40.0, [450, 400], "a1"))           # moving flips back: written
        self.assertFalse(json.loads(self.path.read_text())["moving"])

    def test_a_new_memory_reads_the_file_back(self):
        self.memory.settled(1.0, 2.0, 40.0, None, "a8")
        again = gd.PositionMemory(self.path)
        self.assertEqual(again.cache["y"], 2.0)
        self.assertTrue(again.describe()["remembered"])
        again.untrust("forgotten")
        self.assertEqual(gd.PositionMemory(self.path).describe()["text"], "position not remembered (forgotten)")

    def test_the_pretend_board_never_uses_the_real_file(self):
        with mock.patch.dict(os.environ, {"GANTRY_STATE_DIR": self.tmp.name}):
            os.environ.pop("GANTRY_SIM_STATE", None)
            self.assertIsNone(gd.PositionMemory.for_port("sim"))
            self.assertEqual(gd.PositionMemory.for_port("/dev/ttyUSB0").path, Path(self.tmp.name) / "position.json")
            os.environ["GANTRY_SIM_STATE"] = str(Path(self.tmp.name) / "sim.json")
            self.assertEqual(gd.PositionMemory.for_port("sim").path, Path(self.tmp.name) / "sim.json")


class LinkWatchTests(Base):
    def test_idle_and_homed_is_remembered_and_moving_is_set_before_a_move(self):
        link, gantry, _ = self.open()
        self.assertFalse(self.state.exists())            # not homed yet: nothing to remember
        gantry.command("$HM")
        gantry.status()
        self.assertEqual((self.saved()["x"], self.saved()["y"], self.saved()["trusted"]), (0.0, 0.0, True))
        gantry.command("G1 X120 Y80 F6000")
        self.assertTrue(self.saved()["moving"])          # written before the move was sent
        gantry.wait_idle(30, "the move")
        data = self.saved()
        self.assertEqual((data["x"], data["y"], data["moving"], data["trusted"], data["steps_per_mm"]),
                         (120.0, 80.0, False, True, 40.0))

    def test_inside_a_batch_the_spot_is_written_once_afterwards(self):
        link, gantry, _ = self.open()
        gantry.command("$HM")
        gantry.status()
        with link.batch():
            for x in (50, 100, 150):
                gantry.command(f"G1 X{x} Y10 F6000")
                gantry.wait_idle(30, "the move")
                self.assertTrue(self.saved()["moving"])
        gantry.status()
        self.assertEqual((self.saved()["x"], self.saved()["moving"]), (150.0, False))

    def test_motors_off_untrusts_until_the_next_home(self):
        link, gantry, _ = self.open()
        gantry.command("$HM")
        gantry.status()
        gantry.command("M18")
        self.assertFalse(self.saved()["trusted"])
        self.assertIn("M18", self.saved()["reason"])
        gantry.status()                                  # the board still says homed: not enough to trust it again
        self.assertFalse(self.saved()["trusted"])
        gantry.command("$HM")
        gantry.status()
        self.assertTrue(self.saved()["trusted"])

    def test_an_alarm_a_hold_and_a_stop_mid_move_untrust(self):
        link, gantry, _ = self.open()
        gantry.command("$HM")
        gantry.status()
        link._watch_status("<Alarm:move took too long|MPos:1.000,2.000|Mag:0|Homed:0>")
        self.assertFalse(self.saved()["trusted"])
        self.assertIn("move took too long", self.saved()["reason"])
        gantry.command("$HM")
        gantry.status()
        link._watch_status("<Alarm:not homed|MPos:0.000,0.000|Mag:0|Homed:0>")    # what a restart says: kept
        self.assertTrue(self.saved()["trusted"])
        gantry.command("G1 X400 Y300 F600")
        link.write("!")
        self.assertFalse(self.saved()["trusted"])
        self.assertIn("stopped", self.saved()["reason"])

    def test_a_new_steps_per_mm_untrusts(self):
        link, gantry, _ = self.open()
        gantry.command("$HM")
        gantry.status()
        gantry.command("$100=40")                        # the same: nothing changes
        self.assertTrue(self.saved()["trusted"])
        link._watch_status("$100=4.878 (steps/mm)")      # only reported ($$ after a restart): not a change
        self.assertTrue(self.saved()["trusted"])
        gantry.command("$100=39")
        self.assertFalse(self.saved()["trusted"])
        self.assertIn("steps per mm", self.saved()["reason"])


class RestoreTests(Base):
    def test_a_restarted_board_is_put_back_without_homing(self):
        self.home_and_go(123.5, 234.25)
        link, gantry, sent = self.open()                 # a fresh pretend board: not homed
        self.assertFalse(gantry.status()["homed"])
        outcome, text = gantry.restore_position(CONFIG)
        self.assertEqual(outcome, "restored", text)
        self.assertIn("Position restored from", text)
        self.assertIn("no homing needed", text)
        st = gantry.status()
        self.assertTrue(st["homed"])
        self.assertEqual((st["x"], st["y"]), (123.5, 234.25))
        self.assertEqual([t for t in sent if t in ("$HM", "$H") or t.startswith("G92")], ["$HM", "G92 X123.500 Y234.250"])

    def test_run_job_restores_instead_of_homing(self):
        self.home_and_go(75.0, 40.0)                     # a1
        link, gantry, sent = self.open()
        job = {"id": 1, "label": "a1a2", "expect": [], "steps": [
            {"op": "carry", "piece": "R", "from": "a1", "to": "a2", "path": [[0, 0], [0, 1]]}]}
        state = gd.run_job(job, CONFIG, link)
        self.assertNotIn("$H", sent)
        self.assertIn("$HM", sent)
        self.assertEqual((state["x"], state["y"]), (75.0, 90.0))
        self.assertEqual((self.saved()["x"], self.saved()["y"], self.saved()["moving"]), (75.0, 90.0, False))

    def test_a_board_that_kept_running_keeps_its_own_count(self):
        link, gantry, sent = self.open()
        gantry.command("$HM")
        gantry.command("G1 X10 Y10 F6000")
        gantry.wait_idle(30, "the move")
        self.state.write_text(json.dumps(dict(self.saved(), trusted=False, reason="old")))
        link.memory.cache = None
        link.doubt = None
        outcome, _ = gantry.restore_position(CONFIG)
        self.assertEqual(outcome, "kept")
        gantry.status()
        self.assertTrue(self.saved()["trusted"])         # refreshed from the board's own count

    def test_power_lost_during_a_move_is_refused(self):
        self.home_and_go(100.0, 100.0)
        self.state.write_text(json.dumps(dict(self.saved(), moving=True)))
        link, gantry, sent = self.open()
        outcome, text = gantry.restore_position(CONFIG)
        self.assertEqual(outcome, "moving")
        self.assertEqual(text, "The power went off during a move, so the position is unknown: home once more.")
        self.assertNotIn("$HM", sent)
        with self.assertRaises(gd.GantryError) as caught:
            gantry.ensure_homed(may_bump=False, config=CONFIG)
        self.assertIn("power went off during a move", str(caught.exception))
        self.assertFalse(gantry.status()["homed"])

    def test_another_steps_per_mm_or_home_corner_is_refused(self):
        self.home_and_go(100.0, 100.0)
        other = dict(CONFIG, mm_per_turn=41.0)           # 39.02 steps/mm
        link, gantry, sent = self.open(other)            # sending the new $100 already untrusts the file
        outcome, text = gantry.restore_position(other)
        self.assertEqual(outcome, "none")
        self.assertIn("steps per mm changed", text)
        self.assertNotIn("$HM", sent)
        self.home_and_go(100.0, 100.0)                   # and the check itself, should the file still say trusted
        link, gantry, sent = self.open()
        outcome, text = gd.restore_position(link.memory, other, gantry.command, gantry.status)
        self.assertEqual(outcome, "none")
        self.assertIn("steps/mm", text)
        self.assertNotIn("$HM", sent)
        self.home_and_go(100.0, 100.0)
        with mock.patch.dict(os.environ, {"GANTRY_HOME_BESIDE": "a8"}):
            outcome, text = gd.Gantry(self.open()[0]).restore_position(dict(CONFIG, home_beside="a8"))
        self.assertEqual(outcome, "none")
        self.assertIn("home beside", text)

    def test_an_untrusted_position_is_refused(self):
        self.home_and_go(100.0, 100.0)
        gd.PositionMemory(self.state).untrust("forgotten on the console page")
        outcome, text = self.open()[1].restore_position(CONFIG)
        self.assertEqual(outcome, "none")
        self.assertIn("forgotten", text)

    @mock.patch.dict(os.environ, {"GANTRY_HOME_BESIDE": "a8"})
    def test_home_beside_a8_lands_on_the_same_physical_spot(self):
        config = dict(CONFIG, home_beside="a8")
        data = self.home_and_go(100.0, 150.0, config)
        self.assertEqual((data["y"], data["home_beside"]), (150.0, "a8"))    # remembered as the Pi counts
        link, gantry, sent = self.open(config)
        self.assertEqual(gantry.restore_position(config)[0], "restored")
        self.assertIn("G92 X100.000 Y250.000", [gd.flip_line(t, link.flip_y) for t in sent])   # 400 - 150 on the wire
        self.assertEqual(gantry.status()["y"], 150.0)
        link.flip_y = None                               # the board's own count
        self.assertEqual(gantry.status()["y"], 250.0)


class ParkTests(Base):
    JOB = {"id": 1, "label": "a1a2", "expect": [], "steps": [
        {"op": "carry", "piece": "R", "from": "a1", "to": "a2", "path": [[0, 0], [0, 1]]}]}

    def test_a_job_ends_with_the_empty_magnet_at_the_start(self):
        config = dict(CONFIG, start_mm=[75.0, 390.0])
        link, gantry, sent = self.open(config)
        gantry.command("$HM")
        state = gd.run_job(self.JOB, config, link)
        self.assertEqual((state["x"], state["y"], state["magnet"]), (75.0, 390.0, False))
        self.assertEqual(state.get("servo"), "down")
        self.assertEqual((self.saved()["x"], self.saved()["y"]), (75.0, 390.0))

    def test_no_start_or_switched_off_means_no_park(self):
        link, gantry, _ = self.open()
        gantry.command("$HM")
        self.assertEqual(gd.run_job(self.JOB, CONFIG, link)["y"], 90.0)
        config = dict(CONFIG, start_mm=[75.0, 390.0], park_at_start_after_job=False)
        self.assertFalse(gd.parks_after_job(config))
        self.assertEqual(gd.run_job(self.JOB, config, link)["y"], 90.0)   # the same carry again: ends on a2


class FakeSerial:
    """pyserial's Serial as far as Link uses it; answers "?" like a board that kept running (or restarts)."""
    made: list = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.port = self.baudrate = self.timeout = None
        self.dsrdtr = self.rtscts = None
        self.dtr = self.rts = True
        self.at_open = None
        self.out: queue.Queue = queue.Queue()
        self.banner_after: float | None = None
        self.opened_at = None
        FakeSerial.made.append(self)

    def open(self):
        self.at_open = {"dtr": self.dtr, "rts": self.rts, "dsrdtr": self.dsrdtr, "rtscts": self.rtscts}
        self.opened_at = time.monotonic()

    def fileno(self):
        raise OSError("no fd")

    def write(self, data: bytes):
        if data.strip() == b"?" and self.banner_after is None:
            self.out.put(b"<Idle|MPos:0.000,0.000|Mag:0|Homed:1>\n")
        return len(data)

    def readline(self):
        if self.banner_after is not None and time.monotonic() - self.opened_at > self.banner_after:
            self.banner_after = None
            return b"[duen-gantry 0.8.1 ready: $X to unlock, $H to home]\n"
        try:
            return self.out.get(timeout=0.02)
        except queue.Empty:
            return b""

    def close(self):
        pass


class KeepRunningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_dir = gd.PORT_LOCK_DIR
        gd.PORT_LOCK_DIR = Path(self.tmp.name)
        FakeSerial.made = []
        self.fake = mock.MagicMock(Serial=FakeSerial)
        self.mods = mock.patch.dict(sys.modules, {"serial": self.fake})
        self.mods.start()

    def tearDown(self):
        self.mods.stop()
        gd.PORT_LOCK_DIR = self.saved_dir
        self.tmp.cleanup()

    def test_dtr_and_rts_are_low_before_the_port_opens_and_a_running_board_is_not_waited_for(self):
        began = time.monotonic()
        link = gd.Link("/dev/ttyFAKE0", 115200, keep_running=True, memory=None)
        try:
            self.assertLess(time.monotonic() - began, 1.0)        # no 2 s reset wait: it answered "?"
            port = FakeSerial.made[0]
            self.assertEqual(port.args, ())                       # made unopened, then opened
            self.assertEqual(port.at_open, {"dtr": False, "rts": False, "dsrdtr": False, "rtscts": False})
            self.assertEqual((port.port, port.baudrate), ("/dev/ttyFAKE0", 115200))
            self.assertIs(link.restarted, False)
        finally:
            link.close()

    def test_a_board_that_restarts_anyway_is_waited_for_not_hung_on(self):
        real_init = FakeSerial.__init__

        def restarting(self, *a, **k):
            real_init(self, *a, **k)
            self.banner_after = 0.4
        with mock.patch.object(FakeSerial, "__init__", restarting):
            began = time.monotonic()
            link = gd.Link("/dev/ttyFAKE1", 115200, keep_running=True, memory=None)
        try:
            self.assertLess(time.monotonic() - began, gd.SETTLE_S)
            self.assertIs(link.restarted, True)
            self.assertTrue(link.boot_lines[-1].startswith("[duen-gantry"))
        finally:
            link.close()

    def test_without_keep_running_the_port_opens_as_before(self):
        port = gd.open_serial(self.fake, "/dev/ttyFAKE2", 115200, keep_running=False)
        self.assertEqual(port.args, ("/dev/ttyFAKE2", 115200))
        self.assertEqual(port.kwargs, {"timeout": 0.2})

    def test_the_setting_comes_from_the_config_when_not_given(self):
        with mock.patch.object(gd, "DEFAULT_CONFIG", Path(self.tmp.name) / "c.json"):
            (Path(self.tmp.name) / "c.json").write_text(json.dumps({"keep_esp32_running": True}))
            self.assertTrue(gd._config_flag("keep_esp32_running"))
            (Path(self.tmp.name) / "c.json").write_text("{}")
            self.assertFalse(gd._config_flag("keep_esp32_running"))
        self.assertTrue(json.loads(Path(gd.HERE / "gantry_config.json").read_text())["keep_esp32_running"])


if __name__ == "__main__":
    unittest.main()
