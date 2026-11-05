"""The Pi side against the pretend board: the right CARRY lines, the right millimetres in the
right order (servo, magnet, moves), a capture as a removal by the person then one carry, the
safety refusals, and G-code mode for GRBL boards. Builds gantry/firmware/host/virtual_gantry first.

    python3 -m unittest test_gantry_driver    (from gantry/pi)
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gantry_driver as gd  # noqa: E402

try:
    import chess  # noqa: F401
    import carry_plan as cp
except ImportError:  # python-chess is only needed for the whole-game tests
    cp = None

HOST = Path(__file__).resolve().parent.parent / "firmware" / "host"
CONFIG = {
    "port": "sim", "baud": 115200, "square_mm": 50.0, "a1_center_mm": [75.0, 40.0],
    "park_columns_mm": {"w": [43.0, 15.0], "b": [507.0, 535.0]}, "park_rows_mm": None,
    "travel_mm": [450.0, 400.0], "travel_speed_mm_s": 120.0, "carry_speed_mm_s": 60.0,
    "grab_settle_s": 0.3, "release_settle_s": 0.2, "servo": True, "servo_settle_s": 0.3, "home_on_start": True,
}
GCODE = dict(CONFIG, protocol="grbl")
REAL = gd.load_config(gd.DEFAULT_CONFIG)   # gantry_config.json: the board as drawn, which the pretend board also uses
STEPS_PER_MM = 40.0


def job(label, *carries, before=None, pieces=None):
    made = {"id": 1, "label": label, "expect": [], "steps": [{"op": "carry", "piece": p, "from": a, "to": b, "path": path} for p, a, b, path in carries]}
    if before is not None:
        made["before"] = sorted(before)
        made["pieces"] = pieces or {}
    return made


def start_job(label, *carries):
    return job(label, *carries, before=gd.board_paths.occupied_at_start(), pieces=gd.board_paths.pieces_at_start())


def capture_job():
    """After 1.e4 d5: the person takes the pawn off d5, then the gantry plays exd5. Hand-written, no python-chess."""
    pieces = gd.board_paths.pieces_at_start()
    pieces["e4"], pieces["d5"] = pieces.pop("e2"), pieces.pop("d7")
    made = job("e4d5", ("P", "e4", "d5", [[4, 3], [3, 4]]), before=set(pieces), pieces=pieces)
    made["steps"].insert(0, {"op": "remove", "piece": "p", "square": "d5"})
    return made


def texts(lines):
    return [t for t, _ in lines]


class BoardMapTests(unittest.TestCase):
    def setUp(self):
        self.board = gd.BoardMap(CONFIG)

    def test_squares_and_lanes(self):
        self.assertEqual(self.board.square_mm("a1"), (75.0, 40.0))
        self.assertEqual(self.board.square_mm("e4"), (275.0, 190.0))
        self.assertEqual(self.board.point_mm(4.5, 1.5), (300.0, 115.0))

    def test_parking_slots_use_the_configured_columns(self):
        self.assertEqual(self.board.square_mm("w:0"), (43.0, 40.0))
        self.assertEqual(self.board.square_mm("w:9"), (15.0, 90.0))
        self.assertEqual(self.board.square_mm("b:7"), (507.0, 390.0))
        self.assertEqual(self.board.point_mm(-0.5, 0), (59.0, 40.0))

    def test_the_lane_beside_the_board_is_where_the_firmware_puts_it(self):
        # Half-way between the h file and the docking lane, in the path finder as in the firmware.
        board = gd.BoardMap(REAL)
        geometry = board.geometry(REAL)
        self.assertEqual(board.point_mm(7.5, 1.5), (431.25, 110.0))
        self.assertEqual(geometry.board_mm(7.5, 1.5), (431.25, 110.0))
        self.assertEqual(geometry.board_mm(-0.5, 0.5), (38.75, 60.0))
        self.assertEqual(geometry.board_mm(5.5, 0.5), (335.0, 60.0))

    def test_unreachable_points_fail_before_anything_moves(self):
        with self.assertRaises(gd.GantryError):
            gd.plan(job("x", ("p", "h2", "b:0", [[7, 1], [8, 0]])), self.board, GCODE)  # b:0 is at X=507 > 450
        with self.assertRaises(gd.GantryError):
            gd.plan_carry(job("x", ("p", "h2", "b:0", [[7, 1], [8, 0]])), self.board, CONFIG)

    def test_the_board_setup_line_for_the_firmware(self):
        self.assertEqual(gd.BoardMap(REAL).setup_line(), "$B=50,60,35,17.5,452.5,17.5,35")
        self.assertEqual(self.board.setup_line(), "$B=50,75,40,43,507,40,50", "no park rows: the slots follow the ranks")

    def test_places_as_the_firmware_reads_them(self):
        self.assertEqual(gd.place_name([4, 3]), "e4")
        self.assertEqual(gd.place_name([5.5, 0.5]), "5.5,0.5")
        self.assertEqual(gd.place_name([-0.5, 4.5]), "-0.5,4.5")
        self.assertEqual(gd.place_name([-1, 3]), "w:3")
        self.assertEqual(gd.place_name([8, 0]), "b:0")
        with self.assertRaises(gd.GantryError):
            gd.place_name([-2, 0])


class CarryPlanTests(unittest.TestCase):
    """The CARRY lines for the real board (gantry_config.json), no hardware."""

    def setUp(self):
        self.board = gd.BoardMap(REAL)

    def test_a_pawn_push_is_one_plain_carry(self):
        lines = gd.plan_carry(start_job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), self.board, REAL)
        self.assertEqual(texts(lines), ["CARRY e2 e4"])
        self.assertEqual(lines[0][1], "Moving the white pawn from e2 to e4")

    def test_a_knight_goes_through_the_lanes_as_via_points_in_squares(self):
        lines = gd.plan_carry(start_job("g1f3", ("N", "g1", "f3", [[6, 0], [5, 2]])), self.board, REAL)
        self.assertEqual(texts(lines), ["CARRY g1 f3 5.5,0.5 5.5,1.5"])

    def test_a_capture_is_a_removal_by_the_person_then_one_carry(self):
        lines = gd.plan_carry(capture_job(), self.board, REAL)
        self.assertEqual(texts(lines), ["# person removes p from d5", "CARRY e4 d5"], "no command for the removal; the taker goes straight to the emptied square")
        self.assertEqual(lines[0][1], "The person takes the black pawn off d5 (the caller waits for the camera before this job runs)")
        self.assertEqual(lines[1][1], "Moving the white pawn from e4 to d5")
        self.assertFalse(any(":" in t for t in texts(lines)), "no docking slot anywhere")
        gcode = gd.plan(capture_job(), gd.BoardMap(GCODE), GCODE)
        self.assertEqual(texts(gcode)[0], "# person removes p from d5")
        self.assertEqual(sum(1 for t in texts(gcode) if t == "M3"), 1, "one grab in G-code too")
        without = capture_job()
        del without["steps"][0]
        with self.assertRaises(gd.GantryError, msg="without the removal d5 is still occupied and the carry has nowhere to go"):
            gd.plan_carry(without, self.board, REAL)

    @unittest.skipUnless(cp, "needs python-chess")
    def test_the_planner_and_the_driver_agree_on_a_capture(self):
        board, layout = chess.Board(), cp.Layout()
        for uci in ("e2e4", "d7d5"):
            move = chess.Move.from_uci(uci)
            cp.job_for_move(layout, board, move)
            board.push(move)
        lines = gd.plan_carry(cp.job_for_move(layout, board, chess.Move.from_uci("e4d5")), self.board, REAL)
        self.assertEqual(texts(lines), ["# person removes p from d5", "CARRY e4 d5"])

    def test_split_job_empties_a_removed_square(self):
        parts = gd.split_job(capture_job(), self.board, REAL, max_carries=10)
        self.assertEqual(len(parts), 1)
        made = capture_job()
        made["steps"].append({"op": "carry", "piece": "P", "from": "a2", "to": "a3", "path": [[0, 1], [0, 2]]})
        parts = gd.split_job(made, self.board, REAL, max_carries=1)
        self.assertEqual([[s["op"] for s in p["steps"]] for p in parts], [["remove", "carry"], ["carry"]])
        self.assertIn("d5", parts[0]["expect"], "the taker stands on d5")
        self.assertNotIn("e4", parts[0]["expect"])
        self.assertEqual(len(parts[0]["expect"]), 31, "the taken pawn is gone")

    def test_predict_reads_carry_lines(self):
        lines = [("CARRY g1 f3 5.5,0.5 5.5,1.5", "")]
        guess = gd.predict(lines, REAL, STEPS_PER_MM, start=(360.0, 35.0), board=self.board)
        self.assertEqual(guess["steps"], [2000, 4000], "25+0+25 mm in X, 25+50+25 mm in Y from g1")
        self.assertEqual(guess["end_mm"], (310.0, 135.0))
        self.assertGreater(guess["seconds"], 0.3 + 0.2 + 2 * 0.3 + 100 / 60, "dwells, two servo settles and the carry at 60 mm/s")
        without_servo = gd.predict(lines, dict(REAL, servo=False), STEPS_PER_MM, start=(360.0, 35.0), board=self.board)
        self.assertAlmostEqual(guess["seconds"] - without_servo["seconds"], 0.6, places=2)

    def test_too_many_carries_is_a_planning_error(self):
        many = job("runaway", *[("P", "a2", "a3", [[0, 1], [0, 2]])] * 41)
        with self.assertRaises(gd.GantryError) as caught:
            gd.plan_carry(many, self.board, REAL)
        self.assertIn("planning error", str(caught.exception))
        self.assertEqual(len(gd.plan_carry(job("fine", *[("P", "a2", "a3", [[0, 1], [0, 2]])] * 40), self.board, REAL)), 40)

    def test_split_job_keeps_the_pieces_straight(self):
        files = "abcdefgh"
        steps = [("P", f"{f}2", f"{f}3", [[i, 1], [i, 2]]) for i, f in enumerate(files)]
        steps += [("p", f"{f}7", f"{f}6", [[i, 6], [i, 5]]) for i, f in enumerate(files)]
        steps += [("P", f"{f}3", f"{f}4", [[i, 2], [i, 3]]) for i, f in enumerate(files)]
        steps += [("N", "b1", "c3", [[1, 0], [2, 2]])]
        whole = start_job("many", *steps)
        parts = gd.split_job(whole, self.board, REAL, max_carries=10)
        self.assertEqual([sum(1 for s in p["steps"] if s["op"] == "carry") for p in parts], [10, 10, 5])
        self.assertTrue(all("before" not in p for p in parts), "parts are already routed")
        self.assertIn("c3", parts[-1]["expect"])
        self.assertNotIn("b1", parts[-1]["expect"])
        self.assertIn("a4", parts[-1]["expect"])
        self.assertEqual(gd.split_job(start_job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), self.board, REAL, max_carries=10), [start_job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]]))], "a small job is left alone")


class PredictionTests(unittest.TestCase):
    def test_a_pawn_push_is_two_squares_of_steps_in_gcode(self):
        lines = gd.plan(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), gd.BoardMap(GCODE), GCODE)
        self.assertEqual(texts(lines), ["G1 X275.000 Y90.000 F7200", "M3", "G4 P0.300", "G1 X275.000 Y190.000 F3600", "M5", "G4 P0.200"])
        guess = gd.predict(lines, GCODE, STEPS_PER_MM, start=(275.0, 90.0))
        self.assertEqual(guess["steps"], [0, 4000], "100 mm at 40 steps/mm")
        self.assertEqual(guess["end_mm"], (275.0, 190.0))

    def test_status_lines(self):
        state = gd.parse_status("<Run|MPos:12.500,34.000|Mag:1|Homed:1|Servo:up|Prog:3/10|Host:lost>")
        self.assertEqual((state["state"], state["x"], state["y"], state["magnet"], state["homed"], state["servo"], state["prog"], state["host_lost"]),
                         ("Run", 12.5, 34.0, True, True, "up", "3/10", True))
        alarm = gd.parse_status("<Alarm:magnet on too long|MPos:0.000,0.000|Mag:0|Homed:0>")
        self.assertEqual((alarm["state"], alarm["reason"]), ("Alarm", "magnet on too long"))
        grbl = gd.parse_status("<Idle|MPos:1.000,2.000,0.000|FS:0,0>")
        self.assertEqual((grbl["state"], grbl["x"], grbl["y"], grbl["homed"]), ("Idle", 1.0, 2.0, False))
        self.assertIsNone(gd.parse_status("ok"))


@unittest.skipUnless(shutil.which("make") and shutil.which("c++"), "needs a C++ compiler")
class PretendBoardTests(unittest.TestCase):
    config_path = Path(__file__).resolve().parent / ".test_config.json"
    gcode_path = Path(__file__).resolve().parent / ".test_config_gcode.json"

    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-s", "-C", str(HOST), "virtual_gantry"], check=True)
        os.environ["GANTRY_SIM_SPEED"] = "200"
        cls.config_path.write_text(json.dumps(CONFIG))
        cls.gcode_path.write_text(json.dumps(GCODE))

    @staticmethod
    def traces(link):
        moves = [re.search(r"steps=(\d+),(\d+) to X=([\d.]+) Y=([\d.]+)", t) for t in link.traces if t.startswith("# MOVE")]
        mags = [re.search(r"MAG (on|off) X=([\d.]+) Y=([\d.]+)", t) for t in link.traces if t.startswith("# MAG")]
        servos = [re.search(r"SERVO (up|down) X=([\d.]+) Y=([\d.]+)", t) for t in link.traces if t.startswith("# SERVO")]
        return ([(int(m[1]), int(m[2]), float(m[3]), float(m[4])) for m in moves],
                [(m[1], float(m[2]), float(m[3])) for m in mags],
                [(m[1], float(m[2]), float(m[3])) for m in servos])

    def run_job(self, a_job, config=CONFIG):
        """Runs the job on a fresh pretend board; returns the final state, the (moves, mags, servos) traces and the lines sent."""
        link = gd.Link("sim", 115200)
        sent = []
        real_write = link.write
        link.write = lambda text: (sent.append(text), real_write(text))
        try:
            state = gd.run_job(a_job, config, link)
        finally:
            link.close()
        self.last_traces = list(link.traces)
        return state, self.traces(link), sent

    def test_a_pawn_push_homes_then_lifts_grabs_carries_releases_and_lowers_at_the_right_squares(self):
        state, (moves, mags, servos), sent = self.run_job(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])))
        self.assertTrue(state["homed"])
        self.assertIn("$B=50,75,40,43,507,40,50", sent, "the board is told where the squares are first")
        self.assertEqual([s for s in sent if s.startswith("CARRY")], ["CARRY e2 e4"])
        self.assertEqual((state["x"], state["y"]), (275.0, 190.0), "ends over e4")
        self.assertEqual(servos, [("up", 275.0, 90.0), ("down", 275.0, 190.0)], "the arm lifts on e2 and drops on e4")
        self.assertEqual(mags, [("on", 275.0, 90.0), ("off", 275.0, 190.0)], "magnet on at e2, off at e4")
        # The carry itself: 100 mm in Y only, which is 4000 steps on the Y motor, exactly as predicted.
        carry = [m for m in moves if (m[2], m[3]) == (275.0, 190.0)][-1]
        self.assertEqual(carry[:2], (0, 4000))
        self.assertFalse(state["magnet"])
        self.assertEqual(state["servo"], "down")
        order = [t.split()[2] for t in [tr for tr in self.last_traces if tr.startswith(("# SERVO", "# MAG"))]]
        self.assertEqual(order, ["up", "on", "off", "down"], "arm up, magnet on, magnet off, arm down")

    def test_a_capture_is_one_carry_after_the_person_took_the_piece_off(self):
        state, (moves, mags, servos), sent = self.run_job(capture_job())
        self.assertEqual([s for s in sent if s.startswith("CARRY")], ["CARRY e4 d5"], "exactly one carry; nothing is sent for the removal")
        self.assertFalse(any(":" in s for s in sent if not s.startswith("$B")), "no slot anywhere")
        self.assertEqual(mags, [("on", 275.0, 190.0), ("off", 225.0, 240.0)], "picks the taker up on e4, drops it on d5")
        self.assertEqual([s[0] for s in servos], ["up", "down"])
        self.assertEqual((state["x"], state["y"]), (225.0, 240.0))

    def test_a_hand_written_slot_carry_is_still_understood(self):
        # No planner sends a piece to a docking slot any more; the driver and the firmware keep the slots for a later version.
        docked = ("p", "d5", "w:4", [[3, 4], [2.5, 4.5], [-0.5, 4.5], [-1, 4]])
        state, (moves, mags, servos), sent = self.run_job(job("dock", docked))
        self.assertEqual([s for s in sent if s.startswith("CARRY")], ["CARRY d5 w:4 2.5,4.5 -0.5,4.5"])
        self.assertEqual(mags, [("on", 225.0, 240.0), ("off", 43.0, 240.0)], "from d5 into slot w:4: White's column, row 4")
        self.assertEqual((state["x"], state["y"]), (43.0, 240.0))

    def test_a_knight_goes_through_the_lanes_in_one_carry(self):
        state, (moves, mags, servos), sent = self.run_job(start_job("g1f3", ("N", "g1", "f3", [[6, 0], [5, 2]])))
        self.assertEqual([s for s in sent if s.startswith("CARRY")], ["CARRY g1 f3 5.5,0.5 5.5,1.5"])
        self.assertEqual(mags, [("on", 375.0, 40.0), ("off", 325.0, 140.0)], "magnet on at g1, off at f3")
        corners = [(m[2], m[3]) for m in moves]
        self.assertIn((350.0, 65.0), corners, "out to the corner between f1, g1, f2 and g2")
        self.assertIn((350.0, 115.0), corners, "up the lane between f2 and g2")
        self.assertEqual((state["x"], state["y"]), (325.0, 140.0))

    def test_gcode_mode_still_speaks_gcode(self):
        # A GRBL board has no arm. In G-code mode the Pi sequences M3, G4, G1 and M5 itself, with a
        # status poll every 50 ms; at 200x board time that looks like a 10 s gap to the board's 15 s
        # magnet limit, so this one runs the pretend board at 20x (real hardware runs at 1x).
        os.environ["GANTRY_SIM_NO_SERVO"] = "1"
        os.environ["GANTRY_SIM_SPEED"] = "20"
        try:
            state, (moves, mags, servos), sent = self.run_job(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), GCODE)
        finally:
            del os.environ["GANTRY_SIM_NO_SERVO"]
            os.environ["GANTRY_SIM_SPEED"] = "200"
        self.assertIn("G1 X275.000 Y90.000 F7200", sent)
        self.assertIn("M3", sent)
        self.assertNotIn("$B=50,75,40,43,507,40,50", sent, "a GRBL board would not understand $B")
        self.assertEqual(mags, [("on", 275.0, 90.0), ("off", 275.0, 190.0)])
        self.assertEqual(servos, [])
        self.assertEqual((state["x"], state["y"]), (275.0, 190.0))

    def test_the_website_contract_job_on_stdin_exit_code(self):
        result = subprocess.run([sys.executable, str(Path(gd.__file__)), "--port", "sim", "--config", str(self.config_path), "--quiet"],
                                input=json.dumps(job("g1f3", ("N", "g1", "f3", [[6, 0], [5.5, 0.5], [5.5, 1.5], [5, 2]]))),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(reply["ok"])
        self.assertEqual((reply["x_mm"], reply["y_mm"]), (325.0, 140.0), "f3")
        self.assertEqual(reply["servo"], "down")
        bad = subprocess.run([sys.executable, str(Path(gd.__file__)), "--port", "sim", "--config", str(self.config_path), "--quiet"],
                             input=json.dumps(job("h2", ("p", "h2", "b:0", [[7, 1], [8, 0]]))), capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("outside the gantry's travel", json.loads(bad.stdout.strip().splitlines()[-1])["error"])

    def test_a_held_board_is_refused_until_a_person_unlocks_it(self):
        link = gd.Link("sim", 115200)
        try:
            gantry = gd.Gantry(link)
            gantry.command("$X")
            gantry.home()
            gantry.command("G0 X300")
            gantry.command("!")
            self.assertEqual(gantry.status()["state"], "Hold")
            with self.assertRaises(gd.GantryError) as caught:
                gd.run_job(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), CONFIG, link)
            self.assertIn("stopped", str(caught.exception))
            self.assertIn("--unlock", str(caught.exception))
            gantry.unlock()
            state = gd.run_job(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), CONFIG, link)
            self.assertEqual((state["x"], state["y"]), (275.0, 190.0))
        finally:
            link.close()

    def test_a_fault_in_mid_carry_releases_the_magnet_lowers_the_arm_and_stops(self):
        link = gd.Link("sim", 115200)
        sent = []
        real_write = link.write
        link.write = lambda text: (sent.append(text), real_write(text))
        real_wait = gd.Gantry.wait_idle

        def failing_wait(self, timeout, what):
            if what.startswith("Moving"):
                raise gd.GantryError("pretend: the camera saw a hand on the board")
            return real_wait(self, timeout, what)

        try:
            with mock.patch.object(gd.Gantry, "wait_idle", failing_wait):
                with self.assertRaises(gd.GantryError):
                    gd.run_job(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), CONFIG, link)
            after = sent[sent.index("CARRY e2 e4") + 1:]
            self.assertEqual([s for s in after if s in ("M5", "M8", "!")], ["M5", "M8", "!"], "magnet off, arm down, stop")
            state = gd.Gantry(link).status()
            self.assertFalse(state["magnet"])
            self.assertEqual(state["servo"], "down")
        finally:
            link.close()

    def test_host_lost_is_reported_and_refused_until_unlock(self):
        os.environ["GANTRY_SIM_HOST_TIMEOUT_MS"] = "1000"   # the pretend board gives up on the host after 1 s of board time
        link = gd.Link("sim", 115200)
        del os.environ["GANTRY_SIM_HOST_TIMEOUT_MS"]
        try:
            state = gd.run_job(job("e2e4", ("P", "e2", "e4", [[4, 1], [4, 3]])), CONFIG, link)
            self.assertEqual((state["x"], state["y"]), (275.0, 190.0), "the carry finished even though the board lost the host")
            self.assertTrue(state["host_lost"])
            self.assertFalse(state["magnet"])
            with self.assertRaises(gd.GantryError) as caught:
                gd.run_job(job("e4e5", ("P", "e4", "e5", [[4, 3], [4, 4]])), CONFIG, link)
            self.assertIn("lost contact", str(caught.exception))
            gd.Gantry(link).unlock()
            state = gd.run_job(job("e4e5", ("P", "e4", "e5", [[4, 3], [4, 4]])), CONFIG, link)
            self.assertEqual((state["x"], state["y"]), (275.0, 240.0))
        finally:
            link.close()

    def test_calibration_steers_to_a1_and_h1_and_saves_the_squares(self):
        config = dict(CONFIG, square_mm=1.0, a1_center_mm=[0.0, 0.0])
        path = Path(__file__).resolve().parent / ".test_calibrated.json"
        answers = iter(["x+60", "y+35", "bogus", "x+900", "ok", "x+350", "ok"])
        said = []
        link = gd.Link("sim", 115200)
        try:
            gd.calibrate(gd.Gantry(link), config, path, read=lambda prompt: next(answers), write=said.append)
            saved = json.loads(path.read_text())
        finally:
            link.close()
            path.unlink(missing_ok=True)
        self.assertEqual(saved["a1_center_mm"], [60.0, 35.0])
        self.assertEqual(saved["square_mm"], 50.0)
        self.assertEqual(saved["travel_mm"], [450.0, 400.0], "the rest of the config is kept")
        self.assertTrue(any("x+10 moves" in line for line in said), "a typo gets help, not a crash")
        self.assertTrue(any("outside the gantry's travel" in line for line in said), "a jog past the travel is refused")
        self.assertTrue(any("squares 50.00 mm" in line for line in said))
        moves = [t for t in link.traces if t.startswith("# MOVE")]
        self.assertIn("to X=410.000 Y=35.000", moves[-1], "the last jog ends under h1")

    def test_dry_run_prints_the_commands_and_the_prediction(self):
        gcode = subprocess.run([sys.executable, str(Path(gd.__file__)), "--dry-run", "--gcode", "--config", str(self.config_path), "--move", "a2a4"],
                               capture_output=True, text=True)
        self.assertEqual(gcode.returncode, 0, gcode.stderr[-300:])
        self.assertIn("G1 X75.000 Y190.000 F3600", gcode.stdout, "a straight clear run from a2 to a4")
        self.assertIn("motor steps X=", gcode.stdout)
        knight = subprocess.run([sys.executable, str(Path(gd.__file__)), "--dry-run", "--config", str(self.config_path), "--move", "g1f3"],
                                capture_output=True, text=True)
        self.assertEqual(knight.returncode, 0, knight.stderr[-300:])
        self.assertIn("CARRY g1 f3 5.5,0.5 5.5,1.5", knight.stdout, "the knight goes through the lanes")
        self.assertNotIn("(make room)", knight.stdout, "nothing steps aside")
        self.assertIn("Moving the white knight from g1 to f3", knight.stdout)
        self.assertIn("; predicted:", knight.stdout)

    @unittest.skipUnless(cp, "needs python-chess")
    def test_game_and_reset_from_the_command_line(self):
        moves = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "f3e5", "c6e5"]
        dry = subprocess.run([sys.executable, str(Path(gd.__file__)), "--dry-run", "--config", str(gd.DEFAULT_CONFIG), "--game", *moves, "--reset"],
                             capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr[-500:])
        self.assertIn("; job e2e4", dry.stdout)
        self.assertIn("# person removes p from e5", dry.stdout, "Nxe5: the person takes the pawn off, the knight follows")
        self.assertIn("CARRY f3 e5", dry.stdout)
        self.assertIn("# person removes N from e5", dry.stdout, "Nxe5 back: the same for the white knight")
        self.assertFalse(any(":" in line for line in dry.stdout.splitlines() if line.startswith("CARRY")), "no docking slot in any carry")
        self.assertIn("; job reset", dry.stdout)
        self.assertIn("Moving the black knight from e5 to b8", dry.stdout)
        self.assertIn("# Put a white knight on g1.", dry.stdout, "the gantry cannot bring back what the person took")
        self.assertIn("# Put a black pawn on e7.", dry.stdout)
        self.assertIn("jobs, about", dry.stdout)
        played = subprocess.run([sys.executable, str(Path(gd.__file__)), "--port", "sim", "--config", str(gd.DEFAULT_CONFIG), "--quiet", "--game", "e2e4", "e7e5"],
                                capture_output=True, text=True)
        self.assertEqual(played.returncode, 0, played.stderr[-500:])
        reply = json.loads(played.stdout.strip().splitlines()[-1])
        self.assertEqual((reply["x_mm"], reply["y_mm"]), (260.0, 235.0), "ends over e5 on the real board's numbers")

    @classmethod
    def tearDownClass(cls):
        cls.config_path.unlink(missing_ok=True)
        cls.gcode_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
