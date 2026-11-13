"""Engine option and shared rating checks; no motor simulation or hardware."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import chess
from chess_game import StockfishPlayer
from touch_companion import CompanionBridge
from test import test_integrated_touch as flow
from test.test_pi_touch_app import after


class RatingTests(unittest.TestCase):
    def player(self):
        player = StockfishPlayer.__new__(StockfishPlayer)
        player.engine = Mock()
        player.engine.options = {"UCI_Elo": SimpleNamespace(min=1320, max=3190),
                                 "UCI_LimitStrength": True, "Skill Level": True}
        player.engine.play.return_value = SimpleNamespace(move=chess.Move.from_uci("e7e5"))
        return player

    def test_rating_configures_real_engine_options_and_legacy_resets_them(self):
        player = self.player()
        board = chess.Board()
        board.push_uci("e2e4")
        self.assertEqual(player.rating_range, (1320, 3190))
        self.assertEqual(player.choose_rated_move(board, 1700).uci(), "e7e5")
        player.engine.configure.assert_called_with({"UCI_LimitStrength": True, "UCI_Elo": 1700})
        player.choose_move(board, "beginner")
        player.engine.configure.assert_called_with({"Skill Level": 0, "UCI_LimitStrength": False})

    def test_white_and_invalid_rating_never_reach_engine(self):
        player = self.player()
        for rating in (True, 1700.5, 100, 4000, 1700):
            with self.assertRaises(ValueError):
                player.choose_rated_move(chess.Board(), rating)
        player.engine.play.assert_not_called()

    def test_companion_rating_validation_and_pending_deduplication(self):
        bridge = CompanionBridge()
        bridge.publish({"difficulty": {"enabled": True, "min": 1320, "max": 3190}, "busy": False})
        revision = bridge.snapshot()["revision"]
        for invalid in (True, "1500", 1500.5, 4000, 100):
            self.assertFalse(bridge.submit("rating", revision, invalid))
        self.assertTrue(bridge.submit("rating", revision, 1800))
        self.assertFalse(bridge.submit("rating", revision, 1900))
        self.assertEqual(bridge.next_action(), ("rating", revision, 1800))
        bridge.complete_action()
        bridge.publish({"difficulty": {"enabled": False, "min": 1320, "max": 3190}})
        self.assertFalse(bridge.submit("rating", bridge.snapshot()["revision"], 1800))


class RatedGameTests(unittest.TestCase):
    setUp = flow.ConfirmFlowTests.setUp

    def test_laptop_rating_drives_black_only(self):
        app = self.app
        app.player.rating_range = (1320, 3190)
        app.player.choose_rated_move = Mock(return_value=chess.Move.from_uci("e7e5"))
        app.companion = SimpleNamespace(bridge=CompanionBridge())
        app.publish_companion()
        bridge = app.companion.bridge
        self.assertTrue(bridge.submit("rating", bridge.snapshot()["revision"], 1800))
        app.dispatch_companion()
        self.assertEqual(app.bot_rating, 1800)
        self.assertEqual(self.log, [], "Changing difficulty never moves the gantry")
        app.camera_sees.append(after(app.session.game.board, "e2e4"))
        app.confirm_move()
        app.poll_results()
        board, rating = app.player.choose_rated_move.call_args.args
        self.assertEqual(board.turn, chess.BLACK)
        self.assertEqual(rating, 1800)
        self.assertEqual(self.log.count("human_moved"), 1)
        self.assertEqual(self.log.count("play"), 1)
