"""The bench console against the pretend board: chess moves with captures docked on the right, undo,
a stop part-way and its retry, the magnet rests, the scale, and the page's own-origin rule."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console_server as cs  # noqa: E402

FAST = {"servo_settle": 0.02, "grab_settle": 0.02, "release_settle": 0.02}


def wait(console, seconds=60.0):
    deadline = time.monotonic() + seconds
    while console.task or console.waiting:
        if time.monotonic() > deadline:
            raise AssertionError(f"still busy: {console.task}")
        time.sleep(0.05)


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not cs.gd.VIRTUAL_GANTRY.exists():
            raise unittest.SkipTest("build the pretend board first: make -C gantry/firmware/host virtual_gantry")
        cls.tmp = tempfile.TemporaryDirectory()
        cs.SETTINGS_FILE = Path(cls.tmp.name) / "settings.json"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.c = cs.Console(cs.gd.load_config(cs.gd.DEFAULT_CONFIG), sim_speed=40)
        self.c.settings.update(FAST, travel_speed=60.0, carry_speed=40.0)
        self.c.connect("sim")
        wait(self.c)
        self.assertIsNotNone(self.c.link, [e["text"] for e in self.c.log])
        self.c.home_manual()

    def tearDown(self):
        wait(self.c)
        if self.c.link is not None:
            self.c.disconnect()

    def play(self, *ucis):
        for uci in ucis:
            self.c.plan_move(uci[:2], uci[2:4], uci[4:] or None, "chess")
            self.c.run_preview()
            wait(self.c)
            self.assertIsNone(self.c.pending, [e["text"] for e in self.c.log][-5:])

    def test_a_capture_is_docked_in_the_right_lane_and_undo_brings_it_back(self):
        self.play("e2e4", "d7d5", "e4d5")
        docked = {k: v for k, v in self.c.layout.pieces.items() if ":" in k}
        self.assertEqual(list(docked.values()), ["p"])
        self.assertTrue(next(iter(docked)).startswith("b:"))
        self.assertEqual(self.c.layout.pieces["d5"], "P")
        self.assertEqual([h["san"] for h in self.c.history], ["e4", "d5", "exd5"])
        st = self.c.query_status()
        self.assertEqual((st["state"], st["magnet"], st["servo"]), ("Idle", False, "down"))
        self.c.plan_undo()
        self.c.run_preview()
        wait(self.c)
        self.assertEqual(self.c.layout.pieces["d5"], "p")
        self.assertEqual(self.c.layout.pieces["e4"], "P")
        self.assertFalse(any(":" in k for k in self.c.layout.pieces))
        self.assertEqual(len(self.c.game.move_stack), 2)

    def test_a_stop_part_way_offers_a_retry_that_waits_for_unlock(self):
        self.c.settings.update(travel_speed=10.0)
        self.c.plan_move("g1", "f3", None, "chess")
        self.c.run_preview()
        time.sleep(0.6)
        self.c.stop()
        wait(self.c)
        self.assertEqual(self.c.pending["next"], 0)
        self.assertEqual(self.c.query_status()["state"], "Hold")
        with self.assertRaises(cs.gd.GantryError):
            self.c.resolve("retry")                      # not before Unlock
        self.assertIsNotNone(self.c.pending)
        self.c.settings.update(travel_speed=60.0)
        self.c.unlock()
        self.c.resolve("retry")
        wait(self.c)
        self.assertIsNone(self.c.pending)
        self.assertEqual(self.c.layout.pieces["f3"], "N")
        self.assertEqual(self.c.game.peek().uci(), "g1f3")

    def test_moves_need_a_home(self):
        self.c.exchange("$100=40")                       # same value: nothing changes
        self.c.position_lost = True
        self.c.plan_move("e2", "e4", None, "chess")
        with self.assertRaises(cs.gd.GantryError):
            self.c.run_preview()

    def test_long_carries_rest_the_magnet(self):
        self.c.settings.update(carry_speed=10.0, magnet_budget=6.0)
        groups = self.c.holds([(60.0, 35.0), (410.0, 35.0)])     # 350 mm at 10 mm/s
        self.assertGreater(len(groups), 1)
        speed, accel = self.c.carry_speed_real(), self.c.accel_real()
        for group in groups:
            held = sum(cs.trapezoid_s(cs.math.dist(a, b), speed, accel) + cs.POLL_LEG_S for a, b in zip(group, group[1:]))
            self.assertLessEqual(held, 6.0 - FAST["grab_settle"] + 1e-9)
        self.assertEqual(groups[-1][-1], (410.0, 35.0))

    def test_each_axis_has_its_own_scale(self):
        self.c.settings.update(scale_x=1.0, scale_y=8.0)            # the Y driver on full steps, X right
        self.c.goto("e4")
        wait(self.c)
        st = self.c.query_status()
        self.assertAlmostEqual(st["x"], 260.0, places=2)
        self.assertAlmostEqual(st["y"], 185.0 / 8, places=2)
        view = self.c.view(0)["status"]
        self.assertAlmostEqual(view["x"], 260.0, places=1)
        self.assertAlmostEqual(view["y"], 185.0, places=1)
        with self.assertRaises(cs.gd.GantryError):
            self.c.home_bump()                           # the firmware's own homing distances are wrong at scale 8
        self.c.settings.update(scale_y=1.0)

    def test_a_diagonal_slows_so_no_axis_passes_its_cap(self):
        self.c.settings.update(x_speed_cap=20.0, y_speed_cap=120.0)
        self.assertAlmostEqual(self.c.leg_speed((0, 0), (100, 0), 60.0), 20.0)
        self.assertAlmostEqual(self.c.leg_speed((0, 0), (0, 100), 60.0), 60.0)
        self.assertAlmostEqual(self.c.leg_speed((0, 0), (100, 100), 60.0), 20.0 * 2 ** 0.5)
        self.assertAlmostEqual(self.c.leg_speed((0, 0), (100, 0), 60.0, capped=False), 60.0)

    def test_quick_jog_clicks_queue_up(self):
        self.c.settings.update(travel_speed=20.0)
        for _ in range(4):
            self.c.jog(0, 10)
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["y"], 40.0, places=2)
        self.assertFalse(any("busy" in e["text"] for e in self.c.log))

    def test_the_distance_check_names_floating_microstep_pins(self):
        self.c.distance_check("x", 20)
        wait(self.c)
        self.assertEqual(self.c.tests["distance"]["state"], "measure")
        self.assertAlmostEqual(self.c.distance_result(160.0), 8.0)
        self.assertIn("microstep", self.c.tests["distance"]["hint"])

    def test_a_carry_goes_a_little_past_the_square_and_back(self):
        moves = []
        real = self.c.move_to
        def record(x, y, *args, **kwargs):
            moves.append((round(x, 2), round(y, 2), kwargs.get("carrying", False)))
            return real(x, y, *args, **kwargs)
        self.c.move_to = record
        self.c.settings.update(overshoot=5.0)
        self.play("e2e4")
        carried = [(x, y) for x, y, carrying in moves if carrying]
        self.assertEqual(carried[-2:], [(260.0, 190.0), (260.0, 185.0)], "5 mm past e4 along the push, then onto e4")
        self.assertEqual(self.c.query_status()["state"], "Idle")
        moves.clear()
        self.c.settings.update(overshoot=0.0)
        self.play("e7e5")
        self.assertEqual([(x, y) for x, y, c in moves if c][-1], (260.0, 235.0))
        self.assertNotIn((260.0, 230.0), [(x, y) for x, y, c in moves])
        self.c.move_to = real

    def test_no_overshoot_out_of_reach(self):
        self.c.settings.update(overshoot=5.0)
        self.assertEqual(self.c.geometry.reach_mm[0], 50.0, "gantry_config.json: the magnet reaches from x = 50 mm")
        self.assertEqual(self.c.overshoot_point([(110.0, 35.0), (60.0, 35.0)]), (55.0, 35.0))
        self.assertIsNone(self.c.overshoot_point([(110.0, 35.0), (52.0, 35.0)]), "47 mm is out of reach")
        self.assertIsNone(self.c.overshoot_point([(452.5, 300.0), (452.5, 417.5)]), "past the top of the travel")

    def test_a_capture_with_no_way_into_the_lane_is_taken_off_by_hand(self):
        self.play("e2e4", "d7d5")
        real = cs.gd.route
        def no_lane(job, *args):
            if any(s.get("reason") == "capture" for s in job["steps"]):
                raise cs.gd.GantryError("no way from d5 to b:0, even after moving one piece aside")
            return real(job, *args)
        cs.gd.route = no_lane
        try:
            preview = self.c.plan_move("e4", "d5", None, "chess")
        finally:
            cs.gd.route = real
        self.assertEqual([s["op"] for s in preview["steps"]], ["remove", "carry"])
        self.assertIn("by hand", preview["warning"])
        self.assertIn("by hand", self.c.view(0)["preview"]["warning"])
        self.assertFalse(any(":" in k for k in preview["after"].pieces), "nothing docked")
        self.c.preview = None

    def test_the_page_only_answers_its_own_origin(self):
        cs.Handler.console = self.c
        server = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            def post(path, origin):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json", "Origin": origin})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    e.close()
                    return e.code
            self.assertEqual(post("/api/unlock", "http://evil.example"), 403)
            self.assertEqual(post("/api/unlock", f"http://127.0.0.1:{port}"), 200)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as r:
                state = json.loads(r.read())
            self.assertTrue(state["connected"])
            self.assertEqual(state["geometry"]["columns"]["8"], 452.5)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
