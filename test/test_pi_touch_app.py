"""The touch app's game flow without a display: the worker runs inline, the camera is scripted, the gantry is a fake.

TouchApp.__init__ runs for real with tkinter replaced by mocks; the scan goes through the app's own
photo pipeline (two-photo consensus, retries) with the photo analysis replaced by scripted observations.
"""
import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chess
import numpy as np

import pi_touch
from board_vision import VisionError
from move_observation import occupied_squares
from pi_touch import TouchApp, TouchSession, _as_observation

try:
    from .test_pi_touch import FakeGantry
except ImportError:   # run from inside test/
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


class FakePlayer:
    def __init__(self, log, moves):
        self.log, self.moves = log, list(moves)
        self.closed = False

    def choose_move(self, board, level):
        self.log.append("stockfish")
        return chess.Move.from_uci(self.moves.pop(0))

    def close(self):
        self.closed = True


CORNERS = [[0.0, 0.0], [80.0, 0.0], [80.0, 80.0], [0.0, 80.0]]


class HeadlessApp(TouchApp):
    def __init__(self, directory: Path, camera_sees: list, log: list) -> None:
        self.camera_sees, self.log, self.messages, self.screens = camera_sees, log, [], 0
        self.view = None   # what every photo of the current scan shows
        args = argparse.Namespace(calibration=directory, camera="usb", device=0,
                                  corners=[point[:] for point in CORNERS], windowed=True)
        with mock.patch.object(pi_touch, "tk"):
            TouchApp.__init__(self, mock.MagicMock(), args)
        self.camera = mock.Mock()   # capture() writes nothing; read_image is patched
        self.welcome_screen = False

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

    def scan(self):                # the camera, scripted: every photo of this scan shows the next view
        if (self.session is not None and not self.busy and
                self.session.phase in ("setup", "human", "computer", "remove")):
            self.log.append("camera")
            self.view = self.camera_sees.pop(0)
        TouchApp.scan(self)


class ScreenApp(HeadlessApp):
    """A HeadlessApp whose render() runs for real, recording the panel's heading and button labels."""

    def __init__(self, directory: Path, camera_sees: list, log: list) -> None:
        self.title = self.detail = None
        self.buttons: list[str] = []
        super().__init__(directory, camera_sees, log)

    def render(self):
        self.screens += 1
        self.title = self.detail = None
        self.buttons = []
        with mock.patch.object(pi_touch, "tk"):
            TouchApp.render(self)

    def heading(self, title, detail=""):
        self.title, self.detail = title, detail

    def button(self, label, command, *, accent=False, short=False):
        self.buttons.append(label)


# After 1.e4 d5 the person plays exd5 and Stockfish answers Qxd5: the white pawn must come off d5 first.
EXCHANGE = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2"


class GameFlowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = []
        self.apps = []
        photo = np.zeros((80, 80, 3), np.uint8)
        for target, kwargs in ((pi_touch, dict(read_image=mock.Mock(return_value=photo))),
                               (pi_touch, dict(observe=lambda _reference, _photo, _calibration, _corners:
                                               (self.current_view(), None)))):
            patcher = mock.patch.multiple(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def current_view(self):
        return self.apps[-1].view

    def new_app(self, cls=HeadlessApp):
        app = cls(Path(self.directory.name), [], self.log)
        app.calibration = mock.Mock(scoring="robust_occupancy_v2", corners=CORNERS, rotation=0)
        app.reference = np.zeros((80, 80, 3), np.uint8)
        self.apps.append(app)
        return app

    def app_in_game(self, gantry, fen=None, stockfish_moves=("e7e5",), cls=HeadlessApp):
        app = self.new_app(cls)
        app.gantry = gantry
        app.player = FakePlayer(self.log, stockfish_moves)
        app.session = TouchSession("beginner", gantry=gantry)
        if fen:
            app.session.game.board.set_fen(fen)
        app.camera_sees.append(sees(app.session.game.board))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        return app

    def test_the_scan_takes_two_photos_and_agrees_on_them(self):
        app = self.app_in_game(None)
        self.assertEqual(app.scan_attempts, 2, "the teammate's two-photo check runs for real here")

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

    def test_the_gantry_screen_says_keep_hands_clear(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry, cls=ScreenApp)
        app.session.scan_human(after(app.session.game.board, "e2e4"))
        app.session.announce_computer(chess.Move.from_uci("e7e5"))
        with mock.patch.object(app, "submit"):   # the gantry is still moving
            app.carry_computer_move()
        app.render()
        self.assertEqual((app.title, app.detail), ("Gantry moving e7e5…", "Keep hands clear of the board."))
        self.assertEqual(app.buttons, ["End Game"])

    def test_a_camera_mismatch_after_the_gantry_moved_is_shown_on_the_photo(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry)
        board = app.session.game.board.copy(stack=False)   # the start position; the session's board moves on
        app.camera_sees.extend([after(board, "e2e4"), after(board, "e2e4")])   # after the gantry: still no reply
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "computer")
        self.assertEqual(app.scan_feedback.title, "COMPUTER MOVE MISMATCH")
        self.assertIn("Expected: Black pawn e7 → e5", app.scan_feedback.detail)
        self.assertEqual(app.session.game.board.turn, chess.BLACK, "the position waits for the pieces to be fixed")
        # The person fixes the pieces and taps Correct Move & Verify: the same check again.
        replied = board.copy(stack=False)
        replied.push_uci("e2e4")
        app.camera_sees.append(after(replied, "e7e5"))
        app.retake_scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        self.assertIsNone(app.scan_feedback)

    def test_a_gantry_error_shows_the_by_hand_instruction_and_the_camera_still_checks(self):
        gantry = FakeGantry(self.log, fail_play="The gantry lost a step.")
        app = self.app_in_game(gantry, cls=ScreenApp)
        board = app.session.game.board
        app.camera_sees.append(after(board, "e2e4"))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "computer")
        self.assertFalse(app.busy, "no automatic camera check: the person moves first")
        self.assertEqual(app.session.gantry_note, "The gantry lost a step.")
        self.assertIn("Move e7 to e5 by hand", app.messages[-1])
        self.assertEqual(self.log[-1], "play", "the camera did not run on its own")
        self.assertEqual(app.title, "Computer's move")
        self.assertTrue(app.detail.startswith("The gantry could not make this move: The gantry lost a step. Move the Black piece"))
        self.assertEqual(app.buttons, ["Verify Computer Move", "End Game"])
        app.camera_sees.append(after(board, "e7e5"))
        app.scan()             # Verify Computer Move
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        self.assertEqual(gantry.names[-1], "new_game", "the gantry's picture follows the camera-verified board")

    def test_the_pieces_go_home_when_stockfish_mates(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry, fen="rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
                               stockfish_moves=("d8h4",))
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
        self.assertEqual(app.messages[-1], "Stockfish wins by checkmate. The pieces are back on their starting squares.")

    def test_end_game_mid_game_resigns_and_puts_the_pieces_back(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry, cls=ScreenApp)
        app.end_game()
        self.assertEqual(app.session.phase, "finished")
        self.assertTrue(app.resetting and app.busy, "the Putting the pieces back… screen")
        self.assertEqual(app.gantry_activity, "Putting the pieces back…")
        self.assertEqual((app.title, app.buttons), ("Putting the pieces back…", ["New Game"]))
        app.poll_results()
        self.assertEqual(gantry.names[-1], "reset")
        self.assertFalse(app.resetting or app.busy)
        self.assertEqual(app.messages[-1], "You ended the game. Stockfish wins. The pieces are back on their starting squares.")
        app.end_game()   # End Game again on the finished screen does not reset twice
        app.poll_results()
        self.assertEqual(gantry.names.count("reset"), 1)

    def test_a_failed_reset_asks_for_the_pieces_by_hand(self):
        gantry = FakeGantry(self.log, fail_reset="The gantry stalled.")
        app = self.app_in_game(gantry)
        app.end_game()
        app.poll_results()
        self.assertFalse(app.resetting or app.busy)
        self.assertIn("The gantry could not put the pieces back: The gantry stalled.", app.messages[-1])
        self.assertIn("Set up the starting position by hand", app.messages[-1])

    def test_new_game_mid_game_puts_the_pieces_back_and_shows_the_menu(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry)
        app.back_to_menu()
        self.assertIsNone(app.session)
        self.assertTrue(app.resetting and app.busy)
        app.poll_results()
        self.assertEqual(gantry.names[-1], "reset")
        self.assertFalse(app.resetting or app.busy)
        self.assertEqual(app.messages[-1], "The pieces are back on their starting squares.")
        # New Game on the "Putting the pieces back…" screen stops waiting for it.
        app.session = TouchSession("beginner", gantry=gantry)
        app.session.verify_setup(occupied_squares(app.session.game.board))
        app.end_game()
        self.assertTrue(app.busy)
        app.back_to_menu()
        self.assertFalse(app.busy or app.resetting)

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
        app.end_game()
        self.assertFalse(app.busy or app.resetting, "no gantry: nothing to put back")
        self.assertEqual(app.messages[-1], "You ended the game. Stockfish wins.")

    def test_a_capture_without_a_gantry_is_made_by_hand_as_before(self):
        app = self.app_in_game(None, fen=EXCHANGE, stockfish_moves=("d8d5",), cls=ScreenApp)
        board = app.session.game.board
        app.camera_sees.append(after(board, "e4d5"))
        app.scan()
        app.poll_results()
        self.assertEqual((app.session.phase, app.session.removal), ("computer", None))
        self.assertEqual(app.messages[-1], "Stockfish: Qxd5. Move d8 to d5. Remove the captured piece.")
        self.assertEqual(app.title, "Computer's move")
        self.assertEqual(app.detail, "Move the Black piece as shown, then remove your hands. Remove the captured piece.")
        self.assertEqual(app.buttons, ["Verify Computer Move", "End Game"])
        self.assertEqual(self.log, ["camera", "camera", "stockfish"])

    def test_a_capture_waits_for_the_piece_to_be_taken_off_then_the_gantry_plays(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry, fen=EXCHANGE, stockfish_moves=("d8d5",), cls=ScreenApp)
        board = app.session.game.board.copy(stack=False)
        exchanged = board.copy(stack=False)
        exchanged.push_uci("e4d5")
        instruction = "Stockfish plays Qxd5. Take the white pawn off d5, then tap Check."
        app.camera_sees.append(after(board, "e4d5"))
        app.scan()
        app.poll_results()   # the person's exd5 → Stockfish's Qxd5 → the Remove the piece screen, the gantry waits
        self.assertEqual(app.session.phase, "remove")
        self.assertEqual(self.log, ["camera", "new_game", "camera", "human_moved", "stockfish"])
        self.assertFalse(app.busy)
        self.assertEqual(app.messages[-1], instruction)
        self.assertEqual((app.title, app.detail), ("Remove the piece", instruction))
        self.assertEqual(app.buttons, ["Check", "End Game"])
        # Check while the pawn is still on d5: the photo comes up with the reason, nothing moved.
        app.camera_sees.append(after(board, "e4d5"))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "remove")
        self.assertEqual(app.scan_feedback.title, "REMOVE THE PIECE")
        self.assertEqual(app.scan_feedback.detail, "d5 still has a piece on it. Take the white pawn off d5, then tap Check.")
        self.assertEqual(app.buttons, ["Check Again", "Adjust Photo Corners", "End Game"])
        self.assertNotIn("play", self.log)
        self.assertFalse(app.busy)
        # Check again once the pawn is off: the gantry plays Qxd5, then the usual camera verification.
        app.camera_sees.extend([_as_observation(occupied_squares(exchanged) - {"d5"}), after(exchanged, "d8d5")])
        app.retake_scan()
        app.poll_results()
        self.assertEqual(self.log[-4:], ["camera", "camera", "play", "camera"])
        self.assertEqual(gantry.names.count("play"), 1)
        self.assertEqual(gantry.calls[-1], ("play", exchanged.fen(), "d8d5"))
        self.assertEqual(app.session.phase, "human")
        self.assertEqual(app.session.game.board.fen(), after_fen(exchanged, "d8d5"))
        self.assertIsNone(app.scan_feedback)
        self.assertIn("d5 is clear.", app.messages)
        self.assertIn("Gantry moving d8d5… keep hands clear of the board.", app.messages)
        self.assertEqual(app.messages[-1], "Computer move Qxd5 verified. Your turn. Carried d8d5.")
        self.assertEqual(app.buttons, ["Scan Move", "End Game"])

    def test_an_unclear_capture_square_is_photographed_again(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry, fen=EXCHANGE, stockfish_moves=("d8d5",))
        board = app.session.game.board.copy(stack=False)
        exchanged = board.copy(stack=False)
        exchanged.push_uci("e4d5")
        app.camera_sees.append(after(board, "e4d5"))
        app.scan()
        app.poll_results()
        unclear = pi_touch.Observation(occupied_squares(exchanged) - {"d5"}, ("d5",), {})
        app.camera_sees.append(unclear)
        app.scan()
        app.poll_results()
        self.assertEqual(app.scan_attempts, pi_touch.MAX_SCAN_ATTEMPTS, "an unclear square is retried like any unclear photo")
        self.assertEqual(app.session.phase, "remove")
        self.assertNotIn("play", self.log)

    def test_reset_notes_stay_on_the_setup_screen_until_the_camera_verifies_it(self):
        gantry = FakeGantry(self.log, reset_notes=["Put a black pawn on e7."])
        app = self.app_in_game(gantry, cls=ScreenApp)
        app.end_game()
        app.poll_results()
        self.assertEqual(app.reset_notes, ("Put a black pawn on e7.",))
        self.assertEqual(app.messages[-1], "You ended the game. Stockfish wins. The pieces are back on their "
                                           "starting squares. Put a black pawn on e7.")
        app.back_to_menu()
        self.assertEqual(app.reset_notes, ("Put a black pawn on e7.",), "the notes wait through the menu")
        app.session = TouchSession("beginner", gantry=gantry)
        app.render()
        self.assertEqual(app.title, "Ready to play?")
        self.assertTrue(app.detail.startswith("Put a black pawn on e7. Place all 32 pieces"))
        app.camera_sees.append(sees(chess.Board()))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        self.assertEqual(app.reset_notes, (), "the camera saw all 32 pieces")

    def start_game(self, app, from_env, **patches):
        calibration = (app.calibration, app.reference)
        with mock.patch.object(pi_touch, "GantryPlayer", mock.Mock(from_env=from_env)), \
                mock.patch.object(pi_touch, "load_calibration", return_value=calibration), \
                mock.patch.object(pi_touch, "StockfishPlayer", lambda: FakePlayer(self.log, [])), \
                mock.patch.object(pi_touch, "read_image", side_effect=VisionError("no photo")), \
                mock.patch.object(HeadlessApp, "saved_calibration_available", return_value=True):
            app.start_game()
            app.poll_results()

    def test_the_first_game_opens_the_gantry_and_a_failure_means_playing_by_hand(self):
        app = self.new_app()
        opened = FakeGantry(self.log)
        self.start_game(app, lambda: opened)
        self.assertIs(app.gantry, opened)
        self.assertEqual(opened.names, ["connect"])
        self.assertIs(app.session.gantry, opened)
        self.assertEqual(app.session.phase, "setup")
        self.assertEqual(app.messages[-1], "Place all 32 pieces in their starting squares, then tap Scan Starting Board.")
        # The next game keeps the open gantry.
        app.back_to_menu()
        self.start_game(app, lambda: self.fail("the open gantry is reused"))
        self.assertIs(app.session.gantry, opened)

        def failing():
            raise pi_touch.GantryError("No gantry on /dev/ttyACM0.")

        app.back_to_menu()
        app.gantry = None
        broken = mock.Mock(connect=failing)
        self.start_game(app, lambda: broken)
        self.assertIsNone(app.gantry)
        self.assertIsNone(app.session.gantry)
        broken.close.assert_called_once()
        self.assertEqual(app.messages[-1], "No gantry on /dev/ttyACM0. Playing without the gantry: move Black's pieces "
                                           "by hand. Place all 32 pieces in their starting squares, then tap Scan Starting Board.")

    def test_without_chess_gantry_port_there_is_no_gantry(self):
        app = self.new_app()
        environment = {key: value for key, value in os.environ.items() if key != "CHESS_GANTRY_PORT"}
        calibration = (app.calibration, app.reference)
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(pi_touch, "load_calibration", return_value=calibration), \
                mock.patch.object(pi_touch, "StockfishPlayer", lambda: FakePlayer(self.log, [])), \
                mock.patch.object(pi_touch, "read_image", side_effect=VisionError("no photo")), \
                mock.patch.object(HeadlessApp, "saved_calibration_available", return_value=True):
            app.start_game()
            app.poll_results()
        self.assertIsNone(app.gantry)
        self.assertIsNone(app.session.gantry)
        self.assertEqual(app.messages[-1], "Place all 32 pieces in their starting squares, then tap Scan Starting Board.")

    def test_a_start_verified_by_the_calibration_tells_the_gantry(self):
        app = self.new_app()
        app.fresh_starting_reference = np.zeros((80, 80, 3), np.uint8)
        opened = FakeGantry(self.log)
        self.start_game(app, lambda: opened)
        self.assertEqual(app.session.phase, "human")
        self.assertTrue(app.session.layout_known)
        self.assertEqual(opened.names, ["connect", "new_game"])

    def test_a_stale_start_closes_only_a_gantry_it_opened(self):
        app = self.new_app()
        player, kept, fresh = mock.Mock(), FakeGantry(), FakeGantry()
        app.gantry = kept
        app.discard_start((None, None, player, None, kept, None))
        self.assertFalse(kept.closed)
        app.discard_start((None, None, player, None, fresh, None))
        self.assertTrue(fresh.closed)
        app.discard_start((None, None, player, None))   # the shape without a gantry still works
        self.assertEqual(player.close.call_count, 3)

    def test_closing_mid_game_puts_the_pieces_back_then_closes_the_link(self):
        gantry = FakeGantry(self.log)
        app = self.app_in_game(gantry)
        app.executor.shutdown(wait=True)
        app.executor = mock.Mock(submit=lambda work, *args: work(*args))
        app.close()
        self.assertEqual(gantry.names[-2:], ["reset", "close"])
        self.assertIsNone(app.gantry)


@unittest.skipUnless(os.environ.get("CHESS_GANTRY_PORT") == "sim", "needs CHESS_GANTRY_PORT=sim and CHESS_GANTRY_DIR")
class SimulatedGantryTest(GameFlowTest.__base__):
    """The capture flow end to end with the real pi_gantry and the pretend board (CHESS_GANTRY_PORT=sim)."""

    def test_capture_with_the_pretend_gantry(self):
        gantry = pi_touch.GantryPlayer.from_env() if pi_touch.GantryPlayer is not None else None
        if gantry is None:
            self.skipTest("pi_gantry could not open the pretend gantry (CHESS_GANTRY_DIR?)")
        self.addCleanup(gantry.close)
        gantry.connect()
        flow = GameFlowTest("test_the_scan_takes_two_photos_and_agrees_on_them")
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        log = flow.log
        app = flow.app_in_game(gantry, fen=EXCHANGE, stockfish_moves=("d8d5",))
        board = app.session.game.board.copy(stack=False)
        exchanged = board.copy(stack=False)
        exchanged.push_uci("e4d5")
        app.camera_sees.extend([after(board, "e4d5"), _as_observation(occupied_squares(exchanged) - {"d5"}),
                                after(exchanged, "d8d5")])
        app.scan()
        app.poll_results()
        self.assertEqual((app.session.phase, app.session.removal), ("remove", ("d5", "P")))
        self.assertEqual(log, ["camera", "camera", "stockfish"])
        app.scan()           # Check: d5 is empty → the pretend gantry carries the queen → the camera verifies
        app.poll_results()
        self.assertEqual(app.session.phase, "human", app.messages[-3:])
        self.assertEqual(app.session.game.board.fen(), after_fen(exchanged, "d8d5"))
        self.assertTrue(any(message.startswith("Gantry moved d8d5") for message in app.messages))
        self.assertEqual(gantry.expected_squares(), set(occupied_squares(app.session.game.board)))


if __name__ == "__main__":
    unittest.main()
