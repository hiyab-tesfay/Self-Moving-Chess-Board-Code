"""The camera check against the pretend board and a pretend camera that knows a hidden "true" board.

The camera only checks: it never moves the gantry to correct anything and never changes the config, the Tune
answers or the starting position (that is set by hand with Set start here, also tested here).

The true board is the real one (gantry_config.json, 53 mm squares) shifted (+6, -4) mm, stretched 1 % along X and
turned 0.4 degrees. The pretend camera reads where the pretend board's magnet is and answers where the piece is
(a chess piece by default, the printed token with camera_cal_target "token"): it lies where it was put until the magnet comes on under it (within 15 mm), then follows the magnet,
trailing 1.5 mm behind the last move's direction, and a move under 0.4 mm does not stir it (static friction).
"""
from pathlib import Path
import copy
import json
import math
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console_server as cs  # noqa: E402

REAL_CONFIG = cs.gd.HERE / "gantry_config.json"
FAST = {"servo_settle": 0.02, "grab_settle": 0.02, "release_settle": 0.02}

try:
    from camera_locate import LocateError  # noqa: E402
except Exception:   # camera_locate missing, or OpenCV missing: the console only checks the class name
    class LocateError(Exception):
        pass


def wait(console, seconds=120.0, until_waiting=False):
    deadline = time.monotonic() + seconds
    while console.task and not (until_waiting and console.waiting):
        if time.monotonic() > deadline:
            raise AssertionError(f"still busy: {console.task}")
        time.sleep(0.02)


class FakeLocator:
    OFFSET = (6.0, -4.0)
    SCALE_X = 1.01
    TURN_DEG = 0.4
    LAG = 1.5
    FRICTION = 0.4
    CAPTURE = 15.0

    def __init__(self, console, photo_dir: Path, start=("c6", (7.0, -9.0)), target="piece"):
        self.c = console
        self.target = target
        self.references = []         # one entry per capture_reference(): the prompts shown so far
        self.sq = console.map.square, console.map.square_y
        self.a1 = console.map.layout_mm("a1")       # the config's a1, before any tuning
        self.turn = math.radians(self.TURN_DEG)
        name, (dx, dy) = start
        self.token = list(self.ideal(name))
        self.token[0] += dx
        self.token[1] += dy
        self.captured = False
        self.last = None
        self.calls = 0
        self.fail_at = None          # raise LocateError on this call
        self.bias_after_capture = None
        self.capture = self.CAPTURE
        self.photo_dir = photo_dir
        self.last_photo = None
        original = console.move_to

        def move_to(*args, **kwargs):
            original(*args, **kwargs)
            self.observe()
        console.move_to = move_to

    def ideal(self, square):
        return "abcdefgh".index(square[0]) * self.sq[0], (int(square[1]) - 1) * self.sq[1]

    def true_mm(self, square):
        """Where the square's centre really is, in the gantry's millimetres."""
        return self.to_gantry(*self.ideal(square))

    def to_gantry(self, bx, by):
        x, y = bx * self.SCALE_X, by
        c, s = math.cos(self.turn), math.sin(self.turn)
        return self.a1[0] + self.OFFSET[0] + c * x - s * y, self.a1[1] + self.OFFSET[1] + s * x + c * y

    def to_board(self, gx, gy):
        x, y = gx - self.a1[0] - self.OFFSET[0], gy - self.a1[1] - self.OFFSET[1]
        c, s = math.cos(self.turn), math.sin(self.turn)
        return (c * x + s * y) / self.SCALE_X, -s * x + c * y

    def observe(self):
        st = self.c.status or {}
        here = self.to_board(st.get("x", 0.0), st.get("y", 0.0))
        held = bool(st.get("magnet")) and st.get("servo") == "up"
        if not held:
            self.captured = False
        elif not self.captured:
            if math.dist(here, self.token) <= self.capture:
                self.captured = True
                self.token = list(here)
        elif self.last is not None:
            d = math.dist(here, self.last)
            if d >= self.FRICTION:
                ux, uy = (here[0] - self.last[0]) / d, (here[1] - self.last[1]) / d
                self.token = [here[0] - self.LAG * ux, here[1] - self.LAG * uy]
        self.last = here

    def capture_reference(self):
        """The empty board's photo: records which prompts the person had seen when it was taken."""
        self.references.append([e["text"] for e in self.c.log if e["kind"] == "wait"])

    def locate(self):
        self.c.query_status()
        self.observe()
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise LocateError("The camera cannot see the calibration markers: is something covering a corner?")
        x, y = self.token
        if self.bias_after_capture and self.captured:
            x, y = x + self.bias_after_capture[0], y + self.bias_after_capture[1]
        path = self.photo_dir / "last.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9")
        self.last_photo = str(path)
        return {"board_mm": [x, y], "square": "?", "confidence": 1.0, "photo": str(path), "px": [0, 0]}


class CameraCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not cs.gd.VIRTUAL_GANTRY.exists():
            raise unittest.SkipTest("build the pretend board first: make -C gantry/firmware/host virtual_gantry")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings_file = cs.SETTINGS_FILE
        cs.SETTINGS_FILE = Path(cls.tmp.name) / "settings.json"

    @classmethod
    def tearDownClass(cls):
        cs.SETTINGS_FILE = cls.settings_file
        cls.tmp.cleanup()

    def setUp(self):
        self.env = os.environ.get("GANTRY_HOME_BESIDE")
        os.environ["GANTRY_HOME_BESIDE"] = "a8"          # the real board: home beside a8
        config = dict(cs.gd.load_config(REAL_CONFIG), mm_per_turn=[40.0, 40.0], microsteps=8)   # $100 = 40
        config.pop("square_nudges_mm", None)
        config.pop("start_mm", None)
        self.c = cs.Console(config, sim_speed=40)
        self.c.settings.update(FAST, travel_speed=60.0, carry_speed=40.0)
        self.c.camera_settle = 0.0
        self.c.connect("sim")
        wait(self.c)
        self.assertIsNotNone(self.c.link, [e["text"] for e in self.c.log])
        self.c.exchange("$0=10")
        self.c.board_settings["$0"] = 10.0
        self.c.home_manual()
        self.fake = FakeLocator(self.c, Path(self.tmp.name))
        self.c.make_locator = lambda: self.fake

    def tearDown(self):
        if self.c.task:
            self.c.stop()
        wait(self.c)
        if self.c.link is not None:
            self.c.disconnect()
        if self.env is None:
            os.environ.pop("GANTRY_HOME_BESIDE", None)
        else:
            os.environ["GANTRY_HOME_BESIDE"] = self.env

    def log(self):
        return [e["text"] for e in self.c.log][-8:]

    def answer_prompts(self, done=None, limit=4):
        """Press Done at every prompt until the task ends; the prompts, in order."""
        prompts, seen = [], None
        deadline = time.monotonic() + 120
        while self.c.task:
            if time.monotonic() > deadline:
                raise AssertionError(f"still busy: {self.c.task}")
            waiting = self.c.waiting
            if waiting is not None and waiting is not seen:
                seen = waiting
                prompts.append(waiting["prompt"])
                self.assertLessEqual(len(prompts), limit, prompts)
                (done or self.c.done)()
            time.sleep(0.02)
        return prompts

    def run_check(self, squares=None):
        self.c.camera_check(squares)
        self.prompts = self.answer_prompts()
        self.assertIn("anywhere", self.prompts[-1])

    def assert_safe(self):
        st = self.c.query_status()
        self.assertFalse(st["magnet"])
        self.assertEqual(st["servo"], "down")


    def snapshot(self):
        """Everything a camera check must leave alone."""
        return (copy.deepcopy(self.c.config), copy.deepcopy(self.c._answers()), copy.deepcopy(self.c.sim_nudges),
                self.c.start_mm(), self.c.sim_start, {s: self.c.map.square_mm(s) for s in self.c.map_squares()})

    def record_moves(self):
        moves = []
        original = self.c.move_to

        def move_to(x, y, speed, what, *args, **kwargs):
            moves.append((what, round(x, 3), round(y, 3)))
            original(x, y, speed, what, *args, **kwargs)
        self.c.move_to = move_to
        return moves

    def expected_error(self, square):
        """What the camera should report: the square's ideal centre less where the believed centre really is."""
        bx, by = self.fake.to_board(*self.c.map.square_mm(square))
        ix, iy = self.fake.ideal(square)
        return ix - bx, iy - by

    def test_the_check_measures_every_square_and_changes_nothing(self):
        before = self.snapshot()
        moves = self.record_moves()
        self.run_check()
        cal = self.c.view(0)["camera_cal"]
        self.assertEqual(cal["state"], "done", self.log())
        self.assertEqual(len(self.prompts), 2, self.prompts)
        self.assertEqual(len(self.fake.references), 1)
        self.assertTrue(cal["photo"])
        self.assertEqual(cal["squares"], ["a8", "h8", "h1", "a1", "d5", "e4"])
        # one result per square, in order, all "verify" rows measured where the gantry believes the centres are
        self.assertEqual([r["kind"] for r in cal["results"]], ["verify"] * 6)
        self.assertEqual([r["square"] for r in cal["results"]], cal["squares"])
        worst = max(cal["results"], key=lambda r: r["distance_mm"])
        for row in cal["results"]:
            ex, ey = self.expected_error(row["square"])
            # rank 8 sits on the travel's edge here: the opposite-side approach is clamped, so the lag is only
            # partly cancelled there
            delta = 0.3 if row["square"][1] != "8" else 0.8
            self.assertAlmostEqual(row["error_mm"][0], ex, delta=delta, msg=row)
            self.assertAlmostEqual(row["error_mm"][1], ey, delta=delta, msg=row)
            self.assertAlmostEqual(row["distance_mm"], math.hypot(ex, ey), delta=delta, msg=row)
            self.assertEqual(row["ok"], row["distance_mm"] <= cs.CAL_VERIFY_TOL_MM)
        self.assertGreater(worst["distance_mm"], 3.0)     # the believed board is ~7 mm off the true one
        # the summary: worst square and distance, and how to fix it
        self.assertIn(f"the worst square is {worst['square']}, {worst['distance_mm']:.1f} mm off", cal["message"])
        self.assertIn(f"Off by up to {worst['distance_mm']:.1f} mm: fix it with Tune (nudge by eye, Right place) or "
                      "'this square's centre is here', then check again.", cal["message"])
        # no correction moves: only the pick-up and the fixed visits to each believed centre (both sides)
        whats = [w for w, _, _ in moves]
        self.assertEqual(whats[:2], ["going under the piece", "testing the hold"])
        self.assertEqual(whats[2:], [w for s in cal["squares"] for w in [f"checking {s}"] * 4])
        for square in cal["squares"]:
            x, y = self.c.map.square_mm(square)
            centres = [(mx, my) for w, mx, my in moves if w == f"checking {square}"][1::2]
            self.assertEqual(centres, [(round(x, 3), round(y, 3))] * 2)
        # nothing changed: config, Tune answers, the map, the start
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.c.start_mm())
        self.assertIsNone(self.c.config.get("start_mm"))
        self.assertNotIn("square_nudges_mm", self.c.config)
        # the piece is left on the last square, magnet off, arm down
        self.assert_safe()
        self.assertLess(math.dist(self.c.here(), self.c.map.square_mm("e4")), 0.05)
        self.assertLess(math.dist(self.fake.token, self.fake.to_board(*self.c.map.square_mm("e4"))), 2.0)

    def test_a_calibrated_board_says_so(self):
        self.fake.OFFSET, self.fake.SCALE_X, self.fake.turn = (0.0, 0.0), 1.0, 0.0
        self.run_check(["a8", "h1", "e4"])
        cal = self.c.camera_cal
        self.assertEqual(cal["state"], "done", self.log())
        for row in cal["results"]:
            self.assertLessEqual(row["distance_mm"], cs.CAL_VERIFY_TOL_MM, row)
            self.assertTrue(row["ok"])
        self.assertIn("Within 1.5 mm everywhere: the board is calibrated.", cal["message"])
        self.assertIn("The piece is on e4.", cal["message"])

    def test_the_real_board_config_file_is_untouched(self):
        self.c.disconnect()
        path = Path(self.tmp.name) / "gantry_config.json"
        self.c.config["square_nudges_mm"] = {"e4": [1.0, -0.5], "a8": [-0.25, 0.75]}
        self.c.config.update(start_square="a8", start_mm=[60.0, 400.0])
        path.write_text(json.dumps(self.c.config))
        self.c.config_path = path
        self.c.connect("sim")
        wait(self.c)
        self.c.sim_nudges = None              # as if a real board: anything saved would go to the file
        self.c.sim_start = None
        self.c._rebuild_map()
        self.c.exchange("$0=10")
        self.c.board_settings["$0"] = 10.0
        self.c.home_manual()
        wait(self.c)                          # homing goes to the start by itself
        self.fake = FakeLocator(self.c, Path(self.tmp.name))
        text = path.read_text()
        before = self.snapshot()
        self.run_check(["a8", "h1", "e4"])
        self.assertEqual(self.c.camera_cal["state"], "done", self.log())
        self.assertEqual(path.read_text(), text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.c.config["square_nudges_mm"], {"e4": [1.0, -0.5], "a8": [-0.25, 0.75]})
        self.assertEqual(self.c.start_mm(), [60.0, 400.0])

    def test_a_camera_error_stops_it_with_the_magnet_off_and_the_arm_down(self):
        before = self.snapshot()
        self.fake.fail_at = 6                  # after the pick-up, while checking a8
        self.run_check()
        cal = self.c.camera_cal
        self.assertEqual(cal["state"], "failed")
        self.assertIn("calibration markers", cal["message"])
        self.assertTrue(any("calibration markers" in e["text"] and e["kind"] == "error" for e in self.c.log))
        self.assert_safe()
        self.assertEqual(self.snapshot(), before)

    def test_a_token_the_magnet_cannot_reach_is_not_carried(self):
        self.fake.capture = 0.0
        self.run_check()
        cal = self.c.camera_cal
        self.assertEqual(cal["state"], "failed")
        self.assertIn("could not pick up", cal["message"])
        self.assertIn("camera check again", cal["message"])
        self.assertEqual(sum("did not follow" in e["text"] for e in self.c.log), 2)
        self.assertEqual(cal["results"], [])
        self.assert_safe()

    def test_busy_bad_squares_and_stop(self):
        with self.assertRaises(cs.gd.GantryError):
            self.c.camera_check(["a8", "z9"])
        with self.assertRaises(cs.gd.GantryError):
            self.c.camera_check(["a8", "a8"])
        with self.assertRaises(cs.gd.GantryError):
            self.c.go_start()                  # no starting position set yet
        self.c.camera_check()
        wait(self.c, until_waiting=True)
        self.assertEqual(self.c.camera_cal["state"], "running")
        with self.assertRaises(cs.Busy):
            self.c.camera_check()
        self.assertEqual(self.c.camera_cal["state"], "running")
        self.c.stop()
        wait(self.c)
        self.assertEqual(self.c.camera_cal["state"], "failed")
        self.c.unlock()
        self.assert_safe()

    def test_not_homed_refuses(self):
        self.c.position_lost = True
        self.c.camera_check()
        wait(self.c)
        self.assertEqual(self.c.camera_cal["state"], "failed")
        self.assertIn("Not homed", self.c.camera_cal["message"])
        self.assertEqual(self.fake.calls, 0)

    def test_the_empty_board_is_photographed_before_the_piece_is_put_on(self):
        self.c.camera_check()
        wait(self.c, until_waiting=True)
        first = self.c.waiting
        self.assertEqual(first["prompt"],
                         "Clear the board completely (nothing on it, hands out of view). Press Done.")
        self.assertEqual(self.fake.references, [])
        self.assertEqual(self.fake.calls, 0)
        self.c.done()
        deadline = time.monotonic() + 30
        while self.c.waiting is None or self.c.waiting is first:
            self.assertLess(time.monotonic(), deadline, self.log())
            time.sleep(0.02)
        self.assertEqual(self.c.waiting["prompt"],
                         "Put one chess piece (a pawn or a rook) anywhere on the board. Press Done.")
        # photographed once, after the first prompt and before the second; nothing located yet
        self.assertEqual(self.fake.references, [[first["prompt"]]])
        self.assertEqual(self.fake.calls, 0)
        prompts = self.answer_prompts()
        self.assertEqual(prompts, ["Put one chess piece (a pawn or a rook) anywhere on the board. Press Done."])
        self.assertEqual(self.c.camera_cal["state"], "done", self.log())
        self.assertEqual(len(self.fake.references), 1)
        texts = " ".join(e["text"] for e in self.c.log)
        self.assertIn("The magnet holds the piece", texts)
        self.assertIn("Checking a8", texts)
        self.assertNotIn("centring", texts)
        self.assertNotIn("token", texts)
        self.assert_safe()

    def test_the_printed_token_still_works_with_one_prompt(self):
        self.c.config["camera_cal_target"] = "token"
        self.fake.target = "token"
        self.run_check(["a8", "h1", "e4"])
        self.assertEqual(self.prompts, ["Clear the board and drop the calibration token anywhere on it. Press Done."])
        self.assertEqual(self.fake.references, [])           # no empty-board photo for the token
        self.assertEqual(self.c.camera_cal["state"], "done", self.log())
        self.assertIn("The token is on e4.", self.c.camera_cal["message"])
        self.assertTrue(any("The magnet holds the token" in e["text"] for e in self.c.log))
        self.assert_safe()

    def test_a_locator_without_capture_reference_still_runs(self):
        self.fake.capture_reference = None                  # an older locator
        self.run_check(["a8", "e4"])
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.c.camera_cal["state"], "done", self.log())

    def test_the_default_locator_gets_the_target_and_piece_height_from_the_config(self):
        made = []

        class NewLocator:
            def __init__(self, square_mm, app_dir=None, token_id=10, camera=None, photo_dir=None, *,
                         target="piece", piece_height_mm=45.0, token_height_mm=0.0, hfov_deg=None):
                made.append(dict(square_mm=square_mm, target=target, piece_height_mm=piece_height_mm,
                                 hfov_deg=hfov_deg))

        class OldLocator:
            def __init__(self, square_mm, app_dir=None, token_id=10, camera=None, photo_dir=None, *,
                         token_height_mm=0.0, token_marker_mm=20.0):
                made.append(dict(square_mm=square_mm))

        self.c.make_locator = None
        self.c.config.update(camera_cal_target="piece", camera_cal_piece_height_mm=38.5, camera_hfov_deg=66)
        for cls in (NewLocator, OldLocator):
            fake_module = types.SimpleNamespace(CameraLocator=cls)
            with mock.patch.dict(sys.modules, {"camera_locate": fake_module}):
                self.assertIsInstance(self.c._new_locator(), cls)
        square = (self.c.map.square, self.c.map.square_y)
        self.assertEqual(made, [dict(square_mm=square, target="piece", piece_height_mm=38.5, hfov_deg=66.0),
                                dict(square_mm=square)])
        self.assertTrue(any("only knows the printed token" in e["text"] for e in self.c.log))

    def test_set_start_here_on_the_pretend_board(self):
        self.c.position_lost = True
        with self.assertRaises(cs.gd.GantryError):
            self.c.set_start_here()            # not homed
        self.c.position_lost = False
        self.c.goto("a8")
        wait(self.c)
        self.c.jog(1.5, -2.0)
        wait(self.c)
        here = self.c.here()
        start = self.c.set_start_here()
        self.assertEqual(start, [round(here[0], 2), round(here[1], 2)])
        self.assertEqual(self.c.start_mm(), start)
        self.assertIsNone(self.c.config.get("start_mm"))      # the pretend board never writes the real config
        self.assertEqual(self.c.view(0)["geometry"]["start_mm"], start)
        # after homing the gantry goes to the start by itself
        self.c.home_manual()
        wait(self.c)
        self.assertLess(math.dist(self.c.here(), start), 0.05, self.log())
        self.assert_safe()
        self.c.goto("e4")
        wait(self.c)
        self.c.go_start()
        wait(self.c)
        self.assertLess(math.dist(self.c.here(), start), 0.05, self.log())

    def test_set_start_here_on_a_real_board_saves_the_config(self):
        self.c.disconnect()
        path = Path(self.tmp.name) / "gantry_config.json"
        path.write_text(json.dumps(self.c.config))
        self.c.config_path = path
        self.c.connect("sim")
        wait(self.c)
        self.c.sim_nudges = None
        self.c._rebuild_map()
        self.c.exchange("$0=10")
        self.c.board_settings["$0"] = 10.0
        self.c.home_manual()
        self.c.goto("a8")
        wait(self.c)
        start = self.c.set_start_here()
        saved = json.loads(path.read_text())
        self.assertEqual(saved["start_square"], "a8")
        self.assertEqual(saved["start_mm"], start)
        self.assertEqual(self.c.start_mm(), start)

    def test_the_routes(self):
        cs.Handler.console = self.c
        server = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"

        def post(path, body, origin=base):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Origin": origin})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())

        def get(path):
            try:
                with urllib.request.urlopen(base + path) as r:
                    return r.status, r.headers.get("Content-Type"), r.read()
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, e.headers.get("Content-Type"), e.read()

        try:
            before = self.snapshot()
            self.assertEqual(get("/api/camera-photo")[0], 404)
            self.assertEqual(post("/api/go-start", {})[0], 409)
            for route in ("/api/camera-check", "/api/camera-calibrate"):      # the new name and the old one
                self.assertEqual(post(route, {}, "http://evil.example")[0], 403)
                self.assertEqual(post(route, {"squares": ["a8", "z1"]})[0], 409)
                self.assertEqual(post(route, {"squares": ["a8", "h1"]}), (200, {"ok": True}))
                wait(self.c, until_waiting=True)
                state = json.loads(get("/api/state")[2])
                self.assertEqual(state["camera_cal"]["state"], "running")
                self.assertEqual(state["camera_cal"]["squares"], ["a8", "h1"])
                self.assertEqual(post(route, {})[0], 409)       # busy
                self.answer_prompts(lambda: self.assertEqual(post("/api/done", {})[0], 200))
                state = json.loads(get("/api/state")[2])
                self.assertEqual(state["camera_cal"]["state"], "done", self.log())
                self.assertEqual([r["kind"] for r in state["camera_cal"]["results"]], ["verify", "verify"])
                self.assertTrue(state["camera_cal"]["photo"])
                self.assertIsNone(state["geometry"]["start_mm"])     # a check never sets the start
                self.assertEqual(self.snapshot(), before)
                code, kind, body = get("/api/camera-photo")
                self.assertEqual((code, kind), (200, "image/jpeg"))
                self.assertTrue(body.startswith(b"\xff\xd8"))
                self.fake.references.clear()
                self.fake.token = list(self.fake.to_board(*self.c.here()))    # the piece left on h1
            # Set start here
            self.assertEqual(post("/api/set-start-here", {}, "http://evil.example")[0], 403)
            self.c.goto("a8")
            wait(self.c)
            code, body = post("/api/set-start-here", {})
            self.assertEqual(code, 200)
            self.assertEqual(body["start_mm"], self.c.start_mm())
            self.assertEqual(json.loads(get("/api/state")[2])["geometry"]["start_mm"], self.c.start_mm())
            self.c.goto("e4")
            wait(self.c)
            self.assertEqual(post("/api/go-start", {}), (200, {"ok": True}))
            wait(self.c)
            self.assertLess(math.dist(self.c.here(), self.c.start_mm()), 0.05)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
