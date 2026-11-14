"""Manual-home guards using a stub serial protocol; never connect or build a simulator."""
from pathlib import Path
from types import SimpleNamespace
import unittest
import chess
from unittest import mock

from pi_gantry import GantryPlayer, GantryError


class ManualHomeGuardTests(unittest.TestCase):
    def test_white_bot_carry_is_rejected_before_planning_or_hardware(self):
        with mock.patch.object(self.player, "_run") as run, \
                mock.patch.object(self.player.cp, "job_for_move") as plan:
            with self.assertRaisesRegex(GantryError, "White is moved by the human"):
                self.player.play(chess.Board(), chess.Move.from_uci("e2e4"))
            run.assert_not_called()
            plan.assert_not_called()

    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / "gantry/pi"
        self.player = GantryPlayer(directory, "/dev/not-opened", directory / "gantry_config.json")
        self.player.link = SimpleNamespace(memory=None, write=mock.Mock())
        self.state = {"x": 254., "y": 367., "homed": True, "magnet": False,
                      "state": "Idle", "reason": "", "host_lost": False, "servo": "down"}
        self.board = mock.Mock()
        self.board.status.side_effect = lambda: self.state.copy()
        patcher = mock.patch.object(self.player.gd, "Gantry", return_value=self.board)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_never_restores_a_home_and_requires_a_fresh_manual_home(self):
        report = self.player.status()
        self.assertFalse(report["homed"])
        self.board.restore_position.assert_not_called()
        self.board.ensure_homed.assert_not_called()
        with mock.patch.object(self.player.gd, "run_jobs") as run:
            with self.assertRaisesRegex(GantryError, "Set home"):
                self.player._run([])
            run.assert_not_called()

    def test_manual_home_stays_on_e7_even_when_a_start_position_is_saved(self):
        self.player.config["start_mm"] = [42, 420]
        with mock.patch.object(self.player.gd, "set_home_here", return_value=self.state), \
                mock.patch.object(self.player, "_move") as move:
            self.assertTrue(self.player.set_home_here()["homed"])
            move.assert_not_called()

    def test_controller_restart_cannot_silently_restore_position_before_a_carry(self):
        self.player.manual_home_ready = True
        self.state["homed"] = False
        with mock.patch.object(self.player.gd, "run_jobs") as run:
            with self.assertRaisesRegex(GantryError, "lost its position"):
                self.player._run([])
            run.assert_not_called()
        self.assertFalse(self.player.manual_home_ready)
        self.board.restore_position.assert_not_called()

    def test_stop_prevents_queued_carries_until_manual_home(self):
        self.player.stop()
        self.assertEqual([call.args[0] for call in self.player.link.write.call_args_list], ["!", "M5", "M8"])
        with self.assertRaisesRegex(GantryError, "Stopped"):
            self.player._run([])
        self.assertFalse(self.player.manual_home_ready)


if __name__ == "__main__":
    unittest.main()
