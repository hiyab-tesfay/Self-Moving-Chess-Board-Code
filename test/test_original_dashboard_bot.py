"""Original console human-White/bot-Black routing, without hardware or motor simulation."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gantry/console'))
import console_server as cs
from bot_black import BlackBot


class OriginalDashboardBotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.object(cs, 'SETTINGS_FILE', Path(folder.name) / 'settings.json'), \
                patch.object(cs.threading, 'Thread'):
            config = dict(cs.gd.load_config(cs.gd.HERE / 'gantry_config.json'), manual_black_moves=True)
            self.c = cs.Console(config)
        self.player = Mock()
        self.player.rating_range = (1320, 3190)
        self.player.choose_rated_move.return_value = chess.Move.from_uci('e7e5')
        self.c.bot = BlackBot(self.c, self.player)
        self.c.require = Mock()
        def start(_name, fn, *args):
            self.c.stop_flag.clear()
            self.c.task = {'name': _name}
            try:
                fn(*args)
            finally:
                self.c.task = None
        self.c.start = Mock(side_effect=start)
        self.jobs = []
        def job(preview, _first):
            self.jobs.append(preview)
            self.c._commit(preview)
        self.c._run_job = Mock(side_effect=job)
        self.handler = cs.Handler.__new__(cs.Handler)
        self.handler.console = self.c

    def test_white_is_bookkeeping_black_is_one_engine_selected_carry(self):
        preview = self.c.plan_move('e2', 'e4', None, 'chess')
        self.assertTrue(preview['manual_only'])
        self.assertEqual(preview['carries'], 0)
        self.assertEqual(preview['routed'], [])
        self.handler._auto(preview)
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.jobs[0]['manual_only'])
        self.assertEqual(self.jobs[0]['uci'], 'e7e5')
        self.assertEqual(self.jobs[0]['carries'], 1)
        board, rating = self.player.choose_rated_move.call_args.args
        self.assertEqual(board.turn, chess.BLACK)
        self.assertEqual(rating, 1500)
        self.assertEqual([move.uci() for move in self.c.game.move_stack], ['e2e4', 'e7e5'])

    def test_no_automatic_move_on_start_or_white_turn(self):
        self.c.bot.reply()
        self.player.choose_rated_move.assert_not_called()
        self.c._run_job.assert_not_called()

    def test_serial_checks_never_hold_the_state_lock_needed_by_the_poller(self):
        def require(**_kwargs):
            self.assertFalse(self.c.lock._is_owned(), 'serial I/O under the state lock can deadlock the poller')
        self.c.require.side_effect = require
        preview = self.c.plan_move('e2', 'e4', None, 'chess')
        self.handler._auto(preview)
        self.assertEqual(len(self.jobs), 1)

    def test_black_cannot_be_selected_by_human_chess_endpoint(self):
        self.c.game.push_uci('e2e4')
        with self.assertRaisesRegex(cs.gd.GantryError, 'controlled by the bot'):
            self.c.plan_move('e7', 'e5', None, 'chess')
        self.c._run_job.assert_not_called()

    def test_stopping_while_thinking_discards_late_engine_move(self):
        def late_move(*_args):
            self.c.stop_flag.set()
            return chess.Move.from_uci('e7e5')
        self.player.choose_rated_move.side_effect = late_move
        preview = self.c.plan_move('e2', 'e4', None, 'chess')
        with self.assertRaises(cs.Stopped):
            self.handler._auto(preview)
        self.c._run_job.assert_not_called()

    def test_rating_is_used_and_busy_changes_are_refused(self):
        self.c.bot.configure({'rating': 1800})
        preview = self.c.plan_move('e2', 'e4', None, 'chess')
        with self.assertRaises(ValueError):
            self.c.bot.configure({'rating': 1900})
        self.handler._auto(preview)
        self.assertEqual(self.player.choose_rated_move.call_args.args[1], 1800)

    def test_manual_free_carry_requires_pause_and_legacy_mode_still_works(self):
        with self.assertRaisesRegex(cs.gd.GantryError, 'Pause the bot'):
            self.c.plan_move('e2', 'e4', None, 'free')
        self.c.bot.configure({'enabled': False})
        preview = self.c.plan_move('e2', 'e4', None, 'free')
        self.assertGreater(preview['carries'], 0)
        self.c.preview = None
        self.c.bot = None
        preview = self.c.plan_move('e2', 'e4', None, 'chess')
        self.assertFalse(preview['manual_only'])
        self.assertGreater(preview['carries'], 0)

    def test_black_capture_keeps_the_existing_person_removal_prompt(self):
        self.c.game = chess.Board('4k3/8/8/3p4/4P3/8/8/4K3 b - - 0 1')
        self.c.layout = self.c.new_layout(self.c.game)
        self.player.choose_rated_move.return_value = chess.Move.from_uci('d5e4')
        self.c.bot.reply()
        steps = self.jobs[0]['routed']
        self.assertEqual(steps[0]['op'], 'remove')
        self.assertEqual(steps[0]['square'], 'e4')
        self.assertEqual(sum(step['op'] == 'carry' for step in steps), 1)
