"""The bench console against the pretend board: chess moves with captures docked on the right, undo,
a stop part-way and its retry, the magnet rests, mm per turn, the board check, and the page's
own-origin rule. The board is gantry_config.json's (Sketch 5, magnet home at x 60); most checks
count at 40 steps/mm (a fine step, so positions read back exactly), test_the_bench_counts_41_mm_per_turn
at the bench's own 4.878."""
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
        config = dict(cs.gd.load_config(cs.gd.DEFAULT_CONFIG), mm_per_turn=[40.0, 40.0], microsteps=8)   # $100 = 40
        self.c = cs.Console(config, sim_speed=40)
        self.c.settings.update(FAST, travel_speed=60.0, carry_speed=40.0)
        self.c.connect("sim")
        wait(self.c)
        self.assertIsNotNone(self.c.link, [e["text"] for e in self.c.log])
        # The pretend board boots with the real 1500 us step pulse (333 steps/s); these checks are about the
        # console's own speeds, so give it short pulses like a board that takes them.
        self.c.exchange("$0=10")
        self.c.board_settings["$0"] = 10.0
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

    def test_a_permanent_magnet_carries_without_rests(self):
        self.c.settings.update(carry_speed=10.0, magnet_budget=0.0)
        self.assertEqual(self.c.board_settings.get("$G"), 0.0, "firmware 0.7.2: no coil limits ($G=0)")
        self.assertIsNone(self.c.hold_limit())
        self.assertEqual(self.c.holds([(60.0, 35.0), (410.0, 35.0)]), [[(60.0, 35.0), (410.0, 35.0)]])
        saved = dict(self.c.board_settings)
        try:
            self.c.board_settings["$G"] = 15000.0             # firmware 0.7 and earlier cut the magnet at 15 s
            self.assertEqual(self.c.hold_limit(), 12.0)
            self.assertGreater(len(self.c.holds([(60.0, 35.0), (410.0, 35.0)])), 1, "35 s of carrying rests under 0.7")
            del self.c.board_settings["$G"]                    # a board that does not say: assume it cuts at 15 s
            self.assertEqual(self.c.hold_limit(), 12.0)
        finally:
            self.c.board_settings = saved

    def test_long_carries_rest_the_magnet(self):
        self.c.settings.update(carry_speed=10.0, magnet_budget=6.0)
        groups = self.c.holds([(60.0, 35.0), (410.0, 35.0)])     # 350 mm at 10 mm/s
        self.assertGreater(len(groups), 1)
        speed, accel = self.c.carry_speed_real(), self.c.accel_real()
        for group in groups:
            held = sum(cs.trapezoid_s(cs.math.dist(a, b), speed, accel) + cs.POLL_LEG_S for a, b in zip(group, group[1:]))
            self.assertLessEqual(held, 6.0 - FAST["grab_settle"] + 1e-9)
        self.assertEqual(groups[-1][-1], (410.0, 35.0))

    def test_the_board_counts_real_mm_from_the_sketch(self):
        self.assertEqual(self.c.board_settings["$100"], 40.0, "the config's steps/mm was sent on connecting")
        self.assertEqual(self.c.board_settings["$130"], 470.0, "Sketch 5: the magnet's travel")
        self.c.goto("e4")
        wait(self.c)
        st = self.c.query_status()
        self.assertAlmostEqual(st["x"], 261.25, places=2)                # e4 on Sketch 5: 52.5 mm squares from a1 at 51.25
        self.assertAlmostEqual(st["y"], 183.75, places=2)
        g = self.c.view(0)["geometry"]
        self.assertEqual((g["a1"], g["home"], g["lanes"]), ((51.25, 26.25), (0.0, 0.0), ["w", "b"]))
        self.assertEqual(g["problems"], [])

    def test_the_bench_counts_41_mm_per_turn(self):
        self.c.config = cs.gd.load_config(cs.gd.DEFAULT_CONFIG)          # 41 mm per turn, full steps
        self.c.push_motion()
        self.assertAlmostEqual(self.c.board_settings["$100"], 200 / 41, places=4)
        self.assertTrue(self.c.position_lost, "a new steps/mm: home again")
        self.c.home_manual()
        self.c.one_turn("x", 1)
        wait(self.c)
        self.assertEqual(self.c.tests["turn"]["steps"], 200)
        self.assertAlmostEqual(self.c.query_status()["x"], 41.0, delta=0.11)
        self.c.goto("h8")
        wait(self.c)
        st = self.c.query_status()
        self.assertAlmostEqual(st["x"], 418.75, delta=0.11)              # h8 on Sketch 5
        self.assertAlmostEqual(st["y"], 393.75, delta=0.11)

    def test_one_turn_sends_one_pulley_turn_of_steps(self):
        self.c.one_turn("x", 1)
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["x"], 40.0, places=2)      # 1600 steps at 40 steps/mm
        self.assertEqual(self.c.tests["turn"]["steps"], 1600)
        self.c.one_turn("y", 1)
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["y"], 40.0, places=2)
        self.c.one_turn("x", -1)
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["x"], 0.0, places=2)

    def test_a_measured_turn_gives_mm_per_turn(self):
        self.c.one_turn("y", 1)
        wait(self.c)
        self.assertEqual(self.c.tests["turn"]["state"], "measure")
        self.assertAlmostEqual(self.c.turn_result(measured=41.0), 41.0)
        self.assertIn("not 40", self.c.tests["turn"]["hint"])
        self.assertAlmostEqual(self.c.turn_result(measured=40.0), 40.0)
        self.assertIn("Nothing to change", self.c.tests["turn"]["hint"])
        self.c.turn_result(turns=8)
        self.assertIn("microstep", self.c.tests["turn"]["hint"])
        self.c.use_mm_per_turn("y", 40.6)
        self.assertEqual(self.c.config["mm_per_turn"], [40.0, 40.6])
        self.assertEqual(self.c.board_settings["$100"], 40.0, "Y's value does not change the board's steps/mm")
        self.assertFalse(self.c.position_lost)
        self.c.use_mm_per_turn("x", 41.0)
        self.assertAlmostEqual(self.c.board_settings["$100"], 1600 / 41, places=4)
        self.assertTrue(self.c.position_lost, "X's value does: home again")

    def test_one_turn_runs_past_the_edges_and_keeps_the_true_count(self):
        self.c.one_turn("x", -1)                         # at the home corner: the board alone would refuse this
        wait(self.c)
        self.assertFalse(any(e["kind"] == "error" for e in self.c.log), [e["text"] for e in self.c.log][-4:])
        self.assertAlmostEqual(self.c.query_status()["x"], -40.0, places=2)      # the true count: 40 mm past home
        self.c.one_turn("x", 1)
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["x"], 0.0, places=2)
        self.c.exchange("G92 X450")                      # near the far end: 40 more is past the 470 limit
        self.c.one_turn("x", 1)
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["x"], 490.0, places=2)
        self.assertFalse(any(e["kind"] == "error" for e in self.c.log))

    def test_the_step_pulse_caps_the_planned_speed(self):
        self.c.exchange("$0=1500")
        self.c.board_settings["$0"] = 1500.0
        fx, fy = self.c.step_rate_caps()
        self.assertAlmostEqual(fx, 1e6 / 3000 / 40, places=3)                    # 8.33 mm/s at 40 steps/mm
        self.assertAlmostEqual(fy, fx, places=6)
        self.assertAlmostEqual(self.c.leg_speed((0, 0), (100, 0), 60.0), fx)
        self.assertAlmostEqual(self.c.leg_speed((0, 0), (100, 0), 60.0, capped=False), fx)   # the board's own limit stays
        self.c.board_settings["$100"] = 200 / 41
        self.assertAlmostEqual(self.c.step_rate_caps()[0], 1e6 / 3000 / (200 / 41), places=3)   # 68 mm/s on the bench
        self.c.board_settings["$100"] = 40.0
        self.c.goto("b2")                                # plans and runs at the capped speed, nothing times out
        wait(self.c)
        self.assertFalse(any(e["kind"] == "error" for e in self.c.log), [e["text"] for e in self.c.log][-3:])
        self.assertAlmostEqual(self.c.query_status()["x"], 103.75, places=1)

    def test_the_board_check_visits_the_corners_and_the_lane(self):
        self.c.check_board()
        seen = []
        deadline = time.monotonic() + 60
        while self.c.task and time.monotonic() < deadline:
            if self.c.waiting:
                seen.append(self.c.waiting["prompt"].split("centre of ")[1].split("?")[0])
                self.c.done()
            time.sleep(0.02)
        wait(self.c)
        self.assertEqual(seen, ["a1", "h1", "h8", "a8", "w:0", "w:8", "b:0", "b:8"])
        st = self.c.query_status()
        self.assertEqual((round(st["x"], 2), round(st["y"], 2)), (51.25, 26.25), "back on a1")

    def test_a1_is_here_moves_the_whole_sketch(self):
        self.c.exchange("G1 X56.25 Y28.25 F1200")
        time.sleep(0.5)
        report = self.c.a1_here()
        self.assertIn("+5.0 mm in X and +2.0 mm in Y", report[0])
        self.assertEqual(self.c.config["magnet_home_mm"], [-5.0, -2.0])
        self.assertEqual(self.c.map.square_mm("a1"), (56.25, 28.25))
        self.assertEqual(self.c.map.square_mm("h8"), (423.75, 395.75))
        self.assertEqual(self.c.config["a1_center_mm"], [51.25, 26.25], "the sketch's own numbers stay")

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

    def test_a_long_distance_check_gives_mm_per_turn(self):
        self.c.distance_check("x", 20)
        wait(self.c)
        self.assertEqual(self.c.tests["distance"]["state"], "measure")
        self.assertAlmostEqual(self.c.distance_result(20.5), 41.0)
        self.assertIn("use 41.000 mm per turn", self.c.tests["distance"]["hint"])
        self.assertAlmostEqual(self.c.distance_result(160.0), 320.0)
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
        self.assertEqual(carried[-2:], [(261.25, 188.75), (261.25, 183.75)], "5 mm past e4 along the push, then onto e4")
        self.assertEqual(self.c.query_status()["state"], "Idle")
        moves.clear()
        self.c.settings.update(overshoot=0.0)
        self.play("e7e5")
        self.assertEqual([(x, y) for x, y, c in moves if c][-1], (261.25, 236.25))
        self.assertNotIn((261.25, 231.25), [(x, y) for x, y, c in moves])
        self.c.move_to = real

    def test_no_overshoot_out_of_reach(self):
        self.c.settings.update(overshoot=5.0)
        self.assertEqual(self.c.geometry.reach_mm, (0.0, 0.0, 470.0, 420.0), "Sketch 5: the magnet's travel")
        self.assertEqual(self.c.overshoot_point([(100.0, 35.0), (10.0, 35.0)]), (5.0, 35.0))
        self.assertIsNone(self.c.overshoot_point([(50.0, 35.0), (0.0, 35.0)]), "past the a file is out of reach")
        self.assertIsNone(self.c.overshoot_point([(470.0, 300.0), (470.0, 417.5)]), "past the top of the travel")

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
            self.assertEqual(state["geometry"]["columns"]["8"], 470.0)
            self.assertEqual(state["geometry"]["lanes"], ["w", "b"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
