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
from pi_online import OnlineSession, OnlineSettings
from pi_touch import TouchApp, TouchSession, _as_observation
from test_pi_online import snapshot
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


class ScreenApp(HeadlessApp):
    """A HeadlessApp whose render() runs for real, recording the panel's heading and button labels."""

    def __init__(self, directory: Path, camera_sees: list, log: list) -> None:
        super().__init__(directory, camera_sees, log)
        self.calibration_flow, self.startup_calibration = None, False   # the menu, as after Use Saved Calibration
        self.panel = mock.Mock(winfo_children=lambda: [])
        self.title = self.detail = None
        self.always = False   # the heading's detail is shown on the small screen too
        self.buttons: list[str] = []

    def render(self):
        self.screens += 1
        self.title = self.detail = None
        self.always = False
        self.buttons = []
        with mock.patch.object(pi_touch.tk, "Label"), mock.patch.object(pi_touch.tk, "Frame"), \
                mock.patch.object(pi_touch.tk, "Scale"):
            TouchApp.render(self)

    def heading(self, title, detail="", *, always=False):
        self.title, self.detail, self.always = title, detail, always

    def button(self, label, command, *, accent=False, short=False):
        self.buttons.append(label)


# After 1.e4 d5 the person plays exd5 and Stockfish answers Qxd5: the white pawn must come off d5 first.
EXCHANGE = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2"
REMOVE_D5 = {"state": "waiting", "stage": "remove", "current": "d8d5", "jobId": "job-7",
             "instructions": ["Take the white pawn off d5, then press Check."]}


class GameFlowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.log = []

    def tearDown(self):
        self.directory.cleanup()

    def app_in_game(self, gantry, fen=None, stockfish_moves=("e7e5",), cls=HeadlessApp):
        app = cls(Path(self.directory.name), [], self.log)
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
        self.assertEqual(app.messages[-1], "Stockfish wins by checkmate. The pieces are back on their starting squares.")

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

    def test_a_capture_without_a_gantry_is_made_by_hand_as_before(self):
        app = self.app_in_game(None, fen=EXCHANGE, stockfish_moves=("d8d5",), cls=ScreenApp)
        board = app.session.game.board
        app.camera_sees.append(after(board, "e4d5"))
        app.scan()
        app.poll_results()
        self.assertEqual((app.session.phase, app.session.removal), ("computer", None))
        self.assertEqual(app.messages[-1], "Stockfish: Qxd5. Move d8 to d5. Remove the captured piece.")
        self.assertEqual(app.title, "Stockfish: Qxd5")
        self.assertEqual(app.detail, "Move d8 to d5 on the board. Remove the captured piece.")
        self.assertEqual(app.buttons, ["Verify Computer Move", "Reset Board", "End Game"])
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
        self.assertTrue(app.always, "the instruction is on the panel of the small screen too, not only in the status bar")
        self.assertEqual(app.buttons, ["Check", "Reset Board", "End Game"])
        # Check while the pawn is still on d5: the photo comes up with the reason, Check stays, nothing moved.
        app.camera_sees.append(after(board, "e4d5"))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "remove")
        self.assertEqual(app.scan_feedback.title, "REMOVE THE PIECE")
        self.assertEqual(app.scan_feedback.detail, "d5 still has a piece on it. Take the white pawn off d5, then tap Check.")
        self.assertEqual(app.buttons, ["Check", "Adjust Photo Corners", "End Game"])
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
        self.assertEqual(app.buttons, ["Scan Move", "Reset Board", "End Game"])

    def test_reset_notes_stay_on_the_setup_screen_until_the_camera_verifies_it(self):
        gantry = FakeGantry(self.log, reset_notes=["Put a black pawn on e7."])
        app = self.app_in_game(gantry, cls=ScreenApp)
        app.reset_board()
        app.reset_board()
        app.poll_results()
        self.assertEqual(app.session.phase, "setup")
        self.assertEqual(app.reset_notes, ("Put a black pawn on e7.",))
        self.assertEqual(app.messages[-1], "Board reset. Put a black pawn on e7. Place the pieces in their starting squares and scan the board.")
        self.assertEqual((app.title, app.detail), ("Set up the board", "Put a black pawn on e7. Place all 32 pieces in the starting position, then scan."))
        self.assertTrue(app.always, "the notes are on the panel of the small screen too")
        self.assertEqual(app.buttons, ["Scan Starting Board", "Cancel Game"])
        app.camera_sees.append(sees(chess.Board()))
        app.scan()
        app.poll_results()
        self.assertEqual(app.session.phase, "human")
        self.assertEqual(app.reset_notes, (), "the camera saw all 32 pieces")
        self.assertEqual(app.setup_instructions(), "Place all 32 pieces in the starting position, then scan.")
        # After a game ends the notes wait through the menu for the next game's Set up the board screen.
        app.end_game()
        app.poll_results()
        self.assertEqual(app.session.phase, "finished")
        self.assertEqual(app.messages[-1], "You ended the game. Stockfish wins. The pieces are back on their starting squares. Put a black pawn on e7.")
        app.back_to_menu()
        self.assertEqual(app.reset_notes, ("Put a black pawn on e7.",))
        self.assertTrue(app.setup_instructions().startswith("Put a black pawn on e7. "))
        # Without notes the setup screen is as before: its detail stays off the small screen.
        app.reset_notes = ()
        app.session = TouchSession("beginner", gantry=gantry)
        app.render()
        self.assertEqual((app.title, app.detail, app.always),
                         ("Set up the board", "Place all 32 pieces in the starting position, then scan.", False))

    def test_online_remove_shows_check_which_confirms_the_job(self):
        confirmed = []
        settings = OnlineSettings("t" * 64, "http://127.0.0.1:1", "https://chess.example.org", "ABCD")

        def confirm(job_id):
            confirmed.append(job_id)
            return snapshot(clients=1, started=True, turn="w", motion={"state": "moving", "stage": "gantry", "current": "d8d5"})

        app = ScreenApp(Path(self.directory.name), [], self.log)
        holder = OnlineSession(settings, app.results, app.generation, client=mock.Mock(confirm=confirm))
        holder.verified = True   # the camera saw the starting position; the website is playing
        app.online_session = holder
        app.handle_online(snapshot(clients=1, started=True, turn="w", motion={"state": "waiting", "instructions": ["Move the black pawn from e7 to e5."]}), None)
        self.assertEqual(app.buttons, ["End Game", "Back to Menu"], "a move to make by hand has no Check")
        self.assertFalse(app.always)
        app.handle_online(snapshot(clients=1, started=True, turn="w", motion=REMOVE_D5), None)
        self.assertEqual((app.title, app.detail), ("Remove the piece", "Take the white pawn off d5, then press Check."))
        self.assertTrue(app.always, "which piece to take off is on the panel of the small screen too")
        self.assertEqual(app.messages[-1], "Waiting for the piece to be removed…")
        self.assertEqual(app.buttons, ["Check", "End Game", "Back to Menu"])
        # The piece is still there: the next heartbeat carries the reason, the panel shows it, Check stays.
        app.handle_online(snapshot(clients=1, started=True, turn="w", motion=dict(REMOVE_D5, error="d5 still has a piece on it. Take it off, then tap Check.")), None)
        self.assertEqual(app.detail, "d5 still has a piece on it. Take it off, then tap Check.")
        self.assertEqual(app.messages[-1], "d5 still has a piece on it. Take it off, then tap Check.")
        self.assertEqual(app.buttons, ["Check", "End Game", "Back to Menu"])
        # Check: the worker POSTs /api/board-confirm with the job; meanwhile the buttons wait.
        app.confirm_online_removal()
        self.assertTrue(holder.confirming)
        self.assertEqual(app.detail, "Checking the board with the camera…")
        self.assertEqual(app.buttons, ["Back to Menu"])
        app.confirm_online_removal()   # a second tap while the first is on its way does nothing
        app.poll_results()
        self.assertEqual(confirmed, ["job-7"])
        self.assertFalse(holder.confirming)
        self.assertEqual(holder.view.title, "Gantry moving d8d5", "the server's answer is shown at once")
        self.assertEqual((app.messages[-1], app.message_color), ("Gantry moving d8d5…", app.GREEN))
        self.assertEqual(app.buttons, ["End Game", "Back to Menu"])
        self.assertFalse(app.busy)
        # A Check while the piece is still there: the server stays waiting, its reason in red, Check stays.
        still = "d5 still has a piece on it. Take it off, then tap Check."
        holder.client.confirm = lambda job_id: snapshot(clients=1, started=True, turn="w", motion=dict(REMOVE_D5, error=still))
        app.handle_online(snapshot(clients=1, started=True, turn="w", motion=REMOVE_D5), None)
        app.confirm_online_removal()
        app.poll_results()
        self.assertFalse(holder.confirming)
        self.assertEqual((app.messages[-1], app.message_color), (still, app.RED))
        self.assertEqual((app.title, app.detail), ("Remove the piece", still))
        self.assertEqual(app.buttons, ["Check", "End Game", "Back to Menu"])
        # Back to Menu while a Check is on its way: its answer is ignored (the generation moved on).
        app.handle_online(snapshot(clients=1, started=True, turn="w", motion=REMOVE_D5), None)
        app.confirm_online_removal()
        app.leave_online()
        app.poll_results()
        self.assertIsNone(app.online_session)
        self.assertEqual(app.messages[-1], "Online mode off. Choose a difficulty to play on the board.")

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
