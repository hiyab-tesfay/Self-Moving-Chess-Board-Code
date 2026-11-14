"""Confirm-button flow checks with recorded observations; no motor simulator or hardware."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import chess
import numpy as np

import pi_standalone
import pi_touch
from pi_touch import TouchSession, MoveRejected
from move_observation import occupied_squares
from touch_companion import CompanionBridge
from test.test_pi_touch import FakeGantry
from test.test_pi_touch_app import HeadlessApp, FakePlayer, CORNERS, after, sees


class ConfirmFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = []
        self.app = HeadlessApp(Path(self.temp.name), [], self.log)
        self.app.args.standalone = True
        self.app.calibration = SimpleNamespace(scoring="robust_occupancy_v2", corners=CORNERS, rotation=0)
        self.photo = np.zeros((800, 800, 3), np.uint8)
        self.app.reference = self.photo
        self.app.gantry = FakeGantry(self.log)
        self.app.gantry.stop = mock.Mock()
        self.app.player = FakePlayer(self.log, ["e7e5"])
        self.app.session = TouchSession("beginner", self.app.gantry)
        self.app.session.verify_setup(occupied_squares(chess.Board()))
        self.log.clear()
        for patcher in (mock.patch.object(pi_touch, "read_image", return_value=self.photo),
                        mock.patch.object(pi_touch, "observe", side_effect=lambda *_args:
                                          (self.app.view, self.photo))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_white_waits_for_confirm_and_black_waits_for_camera_confirmation(self):
        app = self.app
        start = app.session.game.board.copy()
        app.camera_sees.append(after(start, "e2e4"))
        app.poll_results()
        self.assertEqual(app.session.game.board.fen(), start.fen())
        self.assertEqual(self.log, [])
        app.confirm_move()
        app.poll_results()
        self.assertEqual(app.session.phase, "computer")
        self.assertEqual(self.log.count("human_moved"), 1)
        self.assertEqual(self.log.count("play"), 1)
        self.assertEqual(self.log.count("camera"), 1, "Black does not auto-scan before Confirm")
        self.assertEqual(app.session.game.board.turn, chess.BLACK)
        app.camera_sees.append(after(app.session.game.board, "e7e5"))
        app.confirm_move()
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        self.assertEqual(app.session.game.board.turn, chess.WHITE)
        self.assertEqual(app.session.game.san_moves, ["e4", "e5"])

    def test_laptop_confirm_uses_the_same_session_and_rejects_double_press(self):
        app = self.app
        app.companion = SimpleNamespace(bridge=CompanionBridge())
        app.publish_companion()
        revision = app.companion.bridge.snapshot()["revision"]
        app.camera_sees.append(after(app.session.game.board, "e2e4"))
        self.assertTrue(app.companion.bridge.submit("confirm", revision))
        self.assertFalse(app.companion.bridge.submit("confirm", revision))
        app.poll_results()
        self.assertEqual(app.session.phase, "computer")
        self.assertEqual(app.session.game.san_moves, ["e4"])
        self.assertEqual(self.log.count("play"), 1)

    def test_unchanged_or_unreadable_board_does_not_start_stockfish_or_motion(self):
        self.app.camera_sees.append(sees(self.app.session.game.board))
        self.app.confirm_move()
        self.app.poll_results()
        self.assertEqual(self.app.session.phase, "human")
        self.assertNotIn("stockfish", self.log)
        self.assertNotIn("play", self.log)

    def test_stop_invalidates_pending_engine_results_and_disables_confirm(self):
        app = self.app
        app.session.scan_human(after(app.session.game.board, "e2e4"))
        app.results.put((app.generation, "engine", chess.Move.from_uci("e7e5"), None))
        app.stop_gantry()
        app.poll_results()
        app.gantry.stop.assert_called_once()
        self.assertNotIn("play", self.log)
        app.companion = SimpleNamespace(bridge=CompanionBridge())
        app.publish_companion()
        self.assertFalse(app.companion.bridge.snapshot()["can_confirm"])

    def test_ending_a_standalone_game_does_not_reset_pieces(self):
        self.app.end_game()
        self.assertNotIn("reset", self.log)

    def test_promoted_pawn_requires_explicit_replacement_acknowledgement(self):
        app = self.app
        app.session.game.board = chess.Board("7k/8/8/8/8/8/p7/7K b - - 0 1")
        app.session.phase = "thinking"
        move = chess.Move.from_uci("a2a1q")
        app.session.announce_computer(move)
        app.session.gantry_moved([])
        board = app.session.game.board.copy()
        board.push(move)
        with self.assertRaises(MoveRejected):
            app.session.verify_computer(occupied_squares(board))
        with mock.patch.object(app, "scan") as scan:
            app.confirm_move()
            scan.assert_called_once()
        self.assertFalse(app.session.promotion_pending)

    def test_capture_waits_for_removed_piece_to_be_camera_verified(self):
        app = self.app
        app.session.game.board = chess.Board("rnbqkbnr/ppp1pppp/8/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2")
        app.session.phase = "thinking"
        app.session.announce_computer(chess.Move.from_uci("d8d5"))
        self.assertEqual(app.session.phase, "remove")
        self.assertNotIn("play", self.log)
        with self.assertRaises(MoveRejected):
            app.session.verify_removal(occupied_squares(app.session.game.board))
        remaining = occupied_squares(app.session.game.board) - {"d5"}
        self.assertEqual(app.session.verify_removal(remaining), "d5")
        self.assertEqual(app.session.phase, "computer")
        self.assertNotIn("play", self.log, "The camera result is followed by the UI's single carry action")


class StandaloneSetupTests(unittest.TestCase):
    def test_separate_camera_config_keeps_measurements_and_manual_dashboard_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target, settings = [Path(folder) / name for name in ("source.json", "camera.json", "speeds.json")]
            source.write_text(json.dumps({"square_mm": 53, "mm_per_turn": [38.68, 38.68],
                                          "manual_black_moves": True, "servo_deg": {"up": 122, "down": 37}}))
            settings.write_text(json.dumps({"travel_speed": 40, "carry_speed": 25}))
            pi_standalone.prepare_config(source, target, settings)
            config = json.loads(target.read_text())
            self.assertEqual(config["home_square"], "e7")
            self.assertTrue(config["manual_home_on_connect"])
            self.assertFalse(config["home_on_start"])
            self.assertFalse(config["manual_black_moves"])
            self.assertEqual(config["square_mm"], 53)
            self.assertEqual(config["carry_speed_mm_s"], 25)
            self.assertTrue(json.loads(source.read_text())["manual_black_moves"])
            config["square_mm"] = 53.2
            target.write_text(json.dumps(config))
            pi_standalone.prepare_config(source, target, settings)
            self.assertEqual(json.loads(target.read_text())["square_mm"], 53.2)

    def test_dashboard_handover_refuses_to_interrupt_a_running_task(self):
        state = {"task": "carrying", "status": {"state": "Run"}}
        with mock.patch.object(pi_standalone.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
                mock.patch.object(pi_standalone.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(state).encode())):
            with self.assertRaisesRegex(RuntimeError, "busy"):
                pi_standalone.release_dashboard()
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
