"""The touch app's game flow without a display: the worker runs inline, the camera is scripted, the gantry is a fake.

TouchApp.init_state() sets up everything but the widgets; this subclass replaces the few methods that touch Tk.
"""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chess

import pi_touch
from move_observation import occupied_squares
from pi_online import OnlineSettings
from pi_touch import TouchApp, TouchSession, _as_observation
from test_pi_touch import FakeGantry


def after(board: chess.Board, uci: str):
    """What the camera reports after `uci` is played on `board`."""
    result = board.copy(stack=False)
    result.push_uci(uci)
    return _as_observation(occupied_squares(result))


def sees(board: chess.Board):
    return _as_observation(occupied_squares(board))


def after_fen(board: chess.Board, uci: str) -> str:
    result = board.copy(stack=False)
    result.push_uci(uci)
    return result.fen()


class FakeRoot:
    def after(self, _delay, _callback=None):
        return None

    def destroy(self):
        pass


class FakePlayer:
    def __init__(self, log, moves):
        self.log, self.moves = log, list(moves)
        self.closed = False

    def choose_move(self, board, level):
        self.log.append("stockfish")
        return chess.Move.from_uci(self.moves.pop(0))

    def close(self):
        self.closed = True


class HeadlessApp(TouchApp):
    def __init__(self, directory: Path, camera_sees: list, log: list) -> None:
        self.root = FakeRoot()
        self.init_state(argparse.Namespace(calibration=directory, camera="usb", device=0, corners=None, windowed=True))
        self.compact, self.panel_width = True, 235
        self.camera_sees, self.log, self.messages, self.screens = camera_sees, log, [], 0

    def say(self, message, color=None):
        self.message, self.message_color = message, color
        self.messages.append(message)

    def render(self):
        self.screens += 1

    def draw_board(self):
        pass

    def submit(self, tag, work):   # the worker, inline: results land on the queue for poll_results
        generation = self.generation
        try:
            self.results.put((generation, tag, work(), None))
        except Exception as exc:   # noqa: BLE001 - mirrors TouchApp.submit
            self.results.put((generation, tag, None, exc))

    def scan(self):                # the camera, scripted: the next observation in camera_sees
        if self.session is None or self.busy:
            return
        self.log.append("camera")
        self.scan_feedback = None
        self.scan_photo = "photo"  # the review screen shows the photo while a move is rejected
        self.busy = True
        self.results.put((self.generation, "scan", self.camera_sees.pop(0), None))


class GameFlowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.log = []

    def tearDown(self):
        self.directory.cleanup()

    def app_in_game(self, gantry, fen=None, stockfish_moves=("e7e5",)):
        app = HeadlessApp(Path(self.directory.name), [], self.log)
        app.gantry = gantry
        app.player = FakePlayer(self.log, stockfish_moves)
        app.calibration = app.reference = object()
        app.session = TouchSession("beginner", gantry=gantry)
        if fen:
            app.session.game.board.set_fen(fen)
        app.camera_sees.append(sees(app.session.game.board))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        return app

    def test_stockfish_move_is_carried_by_the_gantry_then_verified_by_the_camera(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry)
        board = app.session.game.board
        replied = board.copy(stack=False)
        replied.push_uci("e2e4")
        # What the camera will see: the person's move, then the board after the gantry carried Stockfish's reply.
        app.camera_sees.extend([after(board, "e2e4"), after(replied, "e7e5")])
        app.scan()
        app.poll_results()   # the person's move → human_moved → Stockfish → announce → gantry.play → camera → verified
        self.assertEqual(self.log, ["camera", "new_game", "camera", "human_moved", "stockfish", "play", "camera"])
        self.assertEqual(app.session.phase, "human")
        self.assertEqual(app.session.game.board.fen(), after_fen(replied, "e7e5"))
        self.assertFalse(app.busy)
        self.assertIsNone(app.gantry_activity)
        self.assertIn("Gantry moving e7e5… keep hands clear of the board.", app.messages)
        self.assertIn("Carrying e7e5", app.messages, "the gantry's progress lines reach the status bar")
        self.assertIn("Gantry moved e7e5. Checking the board with the camera…", app.messages)
        self.assertEqual(app.messages[-1], "Computer move e5 verified. Your turn. Carried e7e5.")

    def test_a_camera_mismatch_after_the_gantry_moved_is_shown_on_the_photo(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry)
        board = app.session.game.board.copy(stack=False)   # the start position; the session's board moves on
        app.camera_sees.extend([after(board, "e2e4"), after(board, "e2e4")])   # after the gantry: still no reply on the board
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "computer")
        self.assertEqual(app.scan_feedback.title, "CHECK COMPUTER MOVE")
        self.assertIn("missing on e5", app.scan_feedback.detail)
        self.assertIn("extra on e7", app.scan_feedback.detail)
        self.assertEqual(app.session.game.board.turn, chess.BLACK, "the position waits for the pieces to be fixed")
        # The person fixes the pieces and taps Retake Photo: the same check again.
        replied = board.copy(stack=False)
        replied.push_uci("e2e4")
        app.camera_sees.append(after(replied, "e7e5"))
        app.retake_scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        self.assertIsNone(app.scan_feedback)

    def test_a_gantry_error_shows_the_by_hand_instruction_and_the_camera_still_checks(self):
        gantry = FakeGantry(self.log, fail_play="The gantry lost a step.")
        app = self.app_in_game(gantry)
        board = app.session.game.board
        app.camera_sees.append(after(board, "e2e4"))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "computer")
        self.assertFalse(app.busy, "no automatic camera check: the person moves first")
        self.assertEqual(app.session.gantry_note, "The gantry lost a step.")
        self.assertIn("Move e7 to e5 by hand", app.messages[-1])
        self.assertEqual(self.log[-1], "play", "the camera did not run on its own")
        app.camera_sees.append(after(board, "e7e5"))
        app.scan()             # Verify Computer Move
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        self.assertEqual(gantry.names[-1], "new_game", "the gantry's picture follows the camera-verified board")

    def test_the_pieces_go_home_when_stockfish_mates(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry, fen="rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2", stockfish_moves=("d8h4",))
        board = app.session.game.board
        pushed = board.copy(stack=False)
        pushed.push_uci("g2g4")
        app.camera_sees.extend([after(board, "g2g4"), after(pushed, "d8h4")])
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "finished")
        self.assertEqual(app.session.result, "Stockfish wins by checkmate.")
        self.assertEqual(self.log[-3:], ["play", "camera", "reset"])
        self.assertFalse(app.resetting)
        self.assertFalse(app.busy)
        self.assertEqual(app.messages[-1], "Stockfish wins by checkmate. The pieces are back on their starting squares. Every piece is home.")

    def test_end_game_mid_game_resigns_and_puts_the_pieces_back(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry)
        app.end_game()
        self.assertEqual(app.session.phase, "finished")
        self.assertTrue(app.resetting and app.busy, "the Putting the pieces back… screen")
        self.assertEqual(app.gantry_activity, "Putting the pieces back…")
        app.poll_results()
        self.assertEqual(gantry.names[-1], "reset")
        self.assertFalse(app.resetting or app.busy)
        app.end_game()   # End Game again on the finished screen does not reset twice
        app.poll_results()
        self.assertEqual(gantry.names.count("reset"), 1)

    def test_reset_board_needs_a_second_tap_then_starts_over(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry)
        first = app.session
        app.reset_board()
        self.assertIs(app.session, first, "one tap only arms the button")
        self.assertTrue(app.reset_armed())
        self.assertEqual(app.reset_label("Reset Board"), "Tap Again to Confirm")
        self.assertIn("Tap Reset Board again", app.messages[-1])
        app.reset_board()
        self.assertFalse(app.reset_armed())
        self.assertTrue(app.resetting)
        app.poll_results()
        self.assertEqual(gantry.names[-1], "reset")
        self.assertIsNot(app.session, first)
        self.assertEqual(app.session.phase, "setup", "the game starts over with the usual start-position check")
        self.assertIs(app.session.gantry, gantry)
        self.assertFalse(app.busy)
        # A failed reset still starts over: the person sets the board up by hand and the camera checks it.
        gantry.fail_reset = "The gantry stalled."
        app.camera_sees.append(sees(chess.Board()))
        app.scan()
        app.poll_results()
        app.reset_board()
        app.reset_board()
        app.poll_results()
        self.assertEqual(app.session.phase, "setup")
        self.assertIn("The gantry stalled.", app.messages[-1])
        self.assertIn("Place the pieces in their starting squares", app.messages[-1])

    def test_without_a_gantry_reset_board_just_starts_over(self):
        app = self.app_in_game(None)
        app.reset_board()
        app.reset_board()
        self.assertEqual(app.session.phase, "setup")
        self.assertFalse(app.busy)
        self.assertEqual(self.log, ["camera"])

    def test_the_by_hand_flow_without_a_gantry_is_unchanged(self):
        app = self.app_in_game(None)
        board = app.session.game.board
        app.camera_sees.append(after(board, "e2e4"))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "computer")
        self.assertFalse(app.busy, "the person is asked to move e7e5 by hand")
        self.assertEqual(app.messages[-1], "Stockfish: e5. Move e7 to e5.")
        self.assertEqual(self.log, ["camera", "camera", "stockfish"])

    def test_online_closes_the_gantry_and_a_new_game_opens_one(self):
        gantry = FakeGantry(self.log)
        app = HeadlessApp(Path(self.directory.name), [], self.log)
        app.gantry = gantry
        app.calibration_flow, app.startup_calibration = None, False   # the menu, as after Use Saved Calibration
        app.executor.shutdown(wait=True)
        app.executor = mock.Mock(submit=lambda work, *args: work(*args))
        with mock.patch.object(pi_touch.OnlineSettings, "load",
                               return_value=OnlineSettings("t" * 64, "http://127.0.0.1:1", "https://x", None)):
            app.go_online()
        self.assertTrue(gantry.closed)
        self.assertIsNone(app.gantry)
        self.assertIsNotNone(app.online_session)
        self.assertIn("Calibrate", app.online_session.problem or "")
        app.leave_online()
        # The next in-person game opens the gantry in the worker; a GantryError means playing by hand.
        opened = FakeGantry(self.log)
        calibration = (mock.Mock(corners=[[0, 0], [1, 0], [1, 1], [0, 1]]), object())
        with mock.patch.object(pi_touch, "GantryPlayer", mock.Mock(from_env=lambda: opened)), \
                mock.patch.object(pi_touch, "load_calibration", return_value=calibration), \
                mock.patch.object(pi_touch, "StockfishPlayer", lambda: FakePlayer(self.log, [])), \
                mock.patch.object(HeadlessApp, "saved_calibration_available", return_value=True):
            app.start_game()
            app.poll_results()
        self.assertIs(app.gantry, opened)
        self.assertEqual(opened.names, ["connect"])
        self.assertIs(app.session.gantry, opened)

        def failing():
            raise pi_touch.GantryError("No gantry on /dev/ttyACM0.")

        app.back_to_menu()
        app.gantry = None
        broken = mock.Mock(connect=failing)
        with mock.patch.object(pi_touch, "GantryPlayer", mock.Mock(from_env=lambda: broken)), \
                mock.patch.object(pi_touch, "load_calibration", return_value=calibration), \
                mock.patch.object(pi_touch, "StockfishPlayer", lambda: FakePlayer(self.log, [])), \
                mock.patch.object(HeadlessApp, "saved_calibration_available", return_value=True):
            app.start_game()
            app.poll_results()
        self.assertIsNone(app.gantry)
        self.assertIsNone(app.session.gantry)
        self.assertIn("No gantry on /dev/ttyACM0. Playing without the gantry", app.messages[-1])


if __name__ == "__main__":
    unittest.main()
