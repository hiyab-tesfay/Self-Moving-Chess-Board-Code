"""The bench console against the pretend board: chess moves with captures taken off by a person, undo,
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
# These tests check the fixed Sketch 5 layout; the real board's config can change without breaking them.
REAL_CONFIG = cs.gd.HERE / "gantry_config.json"
cs.gd.DEFAULT_CONFIG = cs.gd.HERE / "sketch5_test_config.json"
import os  # noqa: E402

# These tests count from a1; HomeBesideA8ConsoleTests switches it on for itself.
os.environ["GANTRY_HOME_BESIDE"] = "a1"

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

    def test_return_home_goes_straight_back_with_the_magnet_off_and_the_arm_down(self):
        self.c.goto("e4")
        wait(self.c)
        self.c.servo(True)
        self.c.return_home()
        wait(self.c)
        st = self.c.query_status()
        self.assertAlmostEqual(st["x"], 0.0, places=1)
        self.assertAlmostEqual(st["y"], 0.0, places=1)
        self.assertEqual((st["magnet"], st["servo"]), (False, "down"))
        self.assertTrue(any("Returning home" in e["text"] for e in self.c.log))

    def done_by_hand(self):
        """Answer every 'do this by hand' wait of the running task (the person took the piece off)."""
        deadline = time.monotonic() + 60
        while self.c.task:
            if self.c.waiting:
                self.c.done()
            self.assertLess(time.monotonic(), deadline, "still busy")
            time.sleep(0.05)

    def test_a_capture_is_taken_off_by_a_person_never_by_the_gantry(self):
        self.play("e2e4", "d7d5")
        preview = self.c.plan_move("e4", "d5", None, "chess")
        self.assertEqual([s["op"] for s in preview["steps"]], ["remove", "carry"])
        self.assertIn("by hand", preview["steps"][0]["text"])
        self.c.run_preview()
        time.sleep(0.3)
        self.assertIn("d5", self.c.waiting["prompt"])          # waits for the person before the carry
        self.assertEqual(self.c.query_status()["magnet"], False)
        self.done_by_hand()
        self.assertIsNone(self.c.pending)
        self.assertFalse(any(":" in k for k in self.c.layout.pieces), "nothing docked")
        self.assertEqual(self.c.layout.pieces["d5"], "P")
        self.assertNotIn("e4", self.c.layout.pieces)
        self.assertEqual([h["san"] for h in self.c.history], ["e4", "d5", "exd5"])
        st = self.c.query_status()
        self.assertEqual((st["state"], st["magnet"], st["servo"]), ("Idle", False, "down"))
        self.c.plan_undo()
        self.c.run_preview()
        self.done_by_hand()                                    # the person puts the black pawn back
        self.assertEqual(self.c.layout.pieces["d5"], "p")
        self.assertEqual(self.c.layout.pieces["e4"], "P")
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

    def test_one_turn_with_no_room_is_refused_once_homed(self):
        self.c.one_turn("x", -1)                         # at home: past it is the frame
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["x"], 0.0, places=2)      # nothing moved, the count is still true
        self.assertTrue(any("No room for one turn X-" in e["text"] for e in self.c.log))
        self.c.exchange("G92 X450")                      # near the far end: 40 more is past the 470 limit
        self.c.one_turn("x", 1)
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["x"], 450.0, places=2)
        self.assertTrue(any("No room for one turn X+" in e["text"] for e in self.c.log))
        self.c.one_turn("x", -1)                         # with room it runs
        wait(self.c)
        self.assertAlmostEqual(self.c.query_status()["x"], 410.0, places=2)

    def test_one_square_jogs_go_centre_to_centre(self):
        self.c.goto("a1")
        wait(self.c)
        sq, sqy = self.c.map.square, self.c.map.square_y
        self.c.jog(0.0, sqy)                             # what the page sends for "1 square" then the up arrow
        wait(self.c)
        x, y = self.c.query_status()["x"], self.c.query_status()["y"]
        ax, ay = self.c.map.square_mm("a2")
        self.assertAlmostEqual(x, ax, delta=0.3)
        self.assertAlmostEqual(y, ay, delta=0.3)
        self.c.jog(sq, 0.0)
        wait(self.c)
        bx, by = self.c.map.square_mm("b2")
        self.assertAlmostEqual(self.c.query_status()["x"], bx, delta=0.3)

    def test_one_turn_before_homing_relabels_the_spot_and_runs(self):
        c = cs.Console(dict(cs.gd.load_config(cs.gd.DEFAULT_CONFIG), mm_per_turn=[40.0, 40.0], microsteps=8), sim_speed=40)
        c.settings.update(FAST)
        c.connect("sim")
        wait(c)
        try:
            c.exchange("$0=10")
            c.board_settings["$0"] = 10.0
            c.one_turn("x", -1)                          # not homed: the count means nothing yet, so the turn just runs
            wait(c)
            self.assertFalse(any(e["kind"] == "error" for e in c.log), [e["text"] for e in c.log][-4:])
            self.assertAlmostEqual(c.query_status()["x"], -40.0, places=2)
        finally:
            wait(c)
            c.disconnect()

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

    def test_any_square_can_say_its_centre_is_here(self):
        self.c.exchange("G1 X419.75 Y398.75 F1200")        # h8 is drawn at 418.75, 393.75
        time.sleep(0.8)
        report = self.c.a1_here("h8")
        self.assertIn("h8 is here", report[0])
        self.assertIn("+1.0 mm in X and +5.0 mm in Y", report[0])
        self.assertEqual(self.c.map.square_mm("h8"), (419.75, 398.75))
        self.assertEqual(self.c.map.square_mm("a1"), (52.25, 31.25), "the whole board moved with it")
        self.assertTrue(self.c.config["calibrated_on_pi"])
        with self.assertRaises(cs.gd.GantryError):
            self.c.a1_here("z9")

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

    def test_jog_and_go_to_leave_the_arm_up(self):
        self.c.servo(True)
        self.assertEqual(self.c.query_status()["servo"], "up")
        self.c.jog(10, 0)
        wait(self.c)
        st = self.c.query_status()
        self.assertEqual(st["servo"], "up", "a jog does not lower the arm")
        self.assertAlmostEqual(st["x"], 10.0, places=2)
        self.assertFalse(any(e["kind"] == "error" for e in self.c.log), [e["text"] for e in self.c.log if e["kind"] == "error"])
        self.c.goto("b2")
        wait(self.c)
        st = self.c.query_status()
        self.assertEqual(st["servo"], "up", "Go to does not lower it either")
        self.assertEqual((round(st["x"], 2), round(st["y"], 2)), tuple(round(v, 2) for v in self.c.map.square_mm("b2")))
        self.c.one_turn("x", 1)
        wait(self.c)
        self.assertEqual(self.c.query_status()["servo"], "up", "nor does One turn")
        self.c.servo(False)
        self.assertEqual(self.c.query_status()["servo"], "down")

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

    def test_a_capture_is_by_hand_even_with_a_lane_the_path_finder_refuses(self):
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
        self.assertIn("by hand", preview["steps"][0]["text"])
        self.assertFalse(any(":" in k for k in preview["after"].pieces), "nothing docked")
        self.c.preview = None

    def test_tune_a_square_by_nudging_it(self):
        e4 = self.c.map.square_mm("e4")
        self.c.tune_goto("e4")
        wait(self.c)
        self.assertEqual(self.c.here(), e4)
        self.c.jog(2.0, 0.0)
        wait(self.c)
        self.c.jog(0.0, -1.0)
        wait(self.c)
        fit = self.c.tune_mark()
        self.assertEqual(fit["count"], 1)
        self.assertEqual(self.c.map.square_mm("e4"), (e4[0] + 2.0, e4[1] - 1.0))
        a1 = self.c.map.square_mm("a1")
        self.assertAlmostEqual(a1[0], self.c.map.layout_mm("a1")[0] + 2.0, places=6, msg="one answer shifts the whole board")
        # a second square, right where the board now puts it: confirmed, not moved
        self.c.tune_goto("h8")
        wait(self.c)
        self.assertEqual(self.c.tune_mark()["count"], 2)
        view = self.c.view(0)["tune"]
        self.assertEqual(sorted(view["answers"]), ["e4", "h8"])
        self.assertEqual(view["squares"]["e4"]["at"], [round(e4[0] + 2.0, 2), round(e4[1] - 1.0, 2)])
        self.assertIn("Pretend board, not saved", " ".join(e["text"] for e in self.c.log))
        self.assertNotIn("square_nudges_mm", self.c.config, "pretend answers stay out of the real config")
        self.c.tune_forget("h8")
        self.c.tune_clear()
        self.assertEqual(self.c.map.square_mm("e4"), e4)

    def test_pretend_answers_are_gone_after_disconnecting(self):
        e4 = self.c.map.square_mm("e4")
        self.c.tune_goto("e4")
        wait(self.c)
        self.c.jog(3.0, 0.0)
        wait(self.c)
        self.c.tune_mark()
        self.assertNotEqual(self.c.map.square_mm("e4"), e4)
        self.c.disconnect()
        self.assertEqual(self.c.map.square_mm("e4"), e4, "the real board must not move by the pretend board's answers")
        self.assertEqual(self.c.view(0)["tune"]["answers"], {})

    def test_tune_refuses_an_answer_more_than_a_square_away(self):
        self.c.tune_goto("a1")
        wait(self.c)
        self.c.goto("e4")
        wait(self.c)
        with self.assertRaises(cs.gd.GantryError):
            self.c.tune_mark("a1")

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


class HomeBesideA8ConsoleTests(unittest.TestCase):
    """Home beside a8: the page still counts a1 at y 26.25, the board itself goes the other way."""

    def test_home_shows_beside_a8_and_a_move_lands_mirrored(self):
        if not cs.gd.VIRTUAL_GANTRY.exists():
            self.skipTest("no pretend board")
        os.environ["GANTRY_HOME_BESIDE"] = "a8"
        try:
            config = dict(cs.gd.load_config(cs.gd.DEFAULT_CONFIG), mm_per_turn=[40.0, 40.0], microsteps=8)
            c = cs.Console(config, sim_speed=40)
            c.settings.update(FAST, travel_speed=60.0, carry_speed=40.0)
            c.connect("sim")
            wait(c)
            try:
                c.exchange("$0=10")
                c.board_settings["$0"] = 10.0
                c.home_manual()
                wait(c)
                self.assertAlmostEqual(c.query_status()["y"], 420.0, places=1)   # home: the page draws it by a8
                c.one_turn("y", -1)                      # - is toward home: at home there is no room, refused
                wait(c)
                self.assertAlmostEqual(c.query_status()["y"], 420.0, places=1)
                self.assertTrue(any("No room for one turn Y-" in e["text"] for e in c.log), [e["text"] for e in c.log][-3:])
                c.one_turn("y", 1)                       # + is away from home: rank 1's way, never into the frame
                wait(c)
                self.assertLess(c.query_status()["y"], 420.0 - 30)
                c.return_home()
                wait(c)
                c.jog(5.0, 0.0)                          # at home Y reads a hair past 420: the jog still goes
                wait(c)
                self.assertAlmostEqual(c.query_status()["x"], 5.0, delta=0.3)
                c.return_home()
                wait(c)
                c.goto("a8")
                wait(c)
                self.assertAlmostEqual(c.query_status()["y"], 393.75, places=1)
                c.link.flip_y = None
                self.assertAlmostEqual(c.query_status()["y"], 26.25, places=1)   # the board's own count: next to home
                c.link.flip_y = 420.0
                c.return_home()
                wait(c)
                self.assertAlmostEqual(c.query_status()["y"], 420.0, places=1)   # home again, beside a8
            finally:
                wait(c)
                c.disconnect()
        finally:
            os.environ["GANTRY_HOME_BESIDE"] = "a1"
