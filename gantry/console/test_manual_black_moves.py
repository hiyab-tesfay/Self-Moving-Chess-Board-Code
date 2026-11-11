"""Black's manually played moves update the game without motor commands."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import chess
import console_server as cs


class ManualBlackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(cs, "SETTINGS_FILE", Path(tmp.name) / "settings.json")
        patch.start()
        self.addCleanup(patch.stop)
        config = dict(cs.gd.load_config(cs.gd.HERE / "gantry_config.json"), manual_black_moves=True)
        self.c = cs.Console(config)

    def position(self, fen):
        self.c.game = chess.Board(fen)
        self.c.layout = self.c.new_layout(self.c.game)

    def test_black_moves_never_use_the_link_and_handle_special_moves(self):
        cases = [
            ("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1", "e7e5"),
            ("4k3/8/8/3p4/4P3/8/8/4K3 b - - 0 1", "d5e4"),
            ("4k2r/8/8/8/8/8/8/4K3 b k - 0 1", "e8g8"),
            ("4k3/8/8/8/3pP3/8/8/4K3 b - e3 0 1", "d4e3"),
            ("4k3/8/8/8/8/8/p7/4K3 b - - 0 1", "a2a1q"),
        ]
        for fen, uci in cases:
            with self.subTest(uci=uci):
                self.position(fen)
                expected = self.c.game.copy()
                expected.push_uci(uci)
                with mock.patch.object(self.c, "exchange") as exchange, mock.patch.object(self.c, "start") as start:
                    preview = self.c.plan_move(uci[:2], uci[2:4], uci[4:] or None, "chess")
                    self.assertTrue(preview["manual_only"])
                    self.assertEqual(preview["carries"], 0)
                    self.c.run_preview()  # even an explicit Run must never move Black
                    exchange.assert_not_called()
                    start.assert_not_called()
                self.assertEqual(self.c.game.fen(), expected.fen())
                self.assertEqual(self.c.layout.pieces, self.c.new_layout(expected).pieces)
                with mock.patch.object(self.c, "exchange") as exchange, mock.patch.object(self.c, "start") as start:
                    self.c.plan_undo()
                    self.c.run_preview()
                    exchange.assert_not_called()
                    start.assert_not_called()
                self.assertEqual(self.c.game.fen(), chess.Board(fen).fen())
                self.assertEqual(self.c.layout.pieces, self.c.new_layout(chess.Board(fen)).pieces)

    def test_page_records_black_even_offline_with_auto_run_off(self):
        self.position("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")
        self.c.settings["auto_run"] = False
        preview = self.c.plan_move("e7", "e5", None, "chess")
        handler = object.__new__(cs.Handler)
        handler.console = self.c
        result = handler._auto(preview)
        self.assertTrue(result["ran"])
        self.assertIsNone(self.c.preview)
        self.assertEqual(self.c.game.peek().uci(), "e7e5")

    def test_white_and_free_moves_still_plan_carries(self):
        white = self.c.plan_move("e2", "e4", None, "chess")
        self.assertFalse(white["manual_only"])
        self.assertGreater(white["carries"], 0)
        free = self.c.plan_move("a7", "a5", None, "free")
        self.assertFalse(free.get("manual_only", False))
        self.assertGreater(free["carries"], 0)


if __name__ == "__main__":
    unittest.main()
