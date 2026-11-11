import unittest

import chess

from board_vision import Observation, TemporalObservation
from move_observation import occupied_squares
from pi_touch import GantryError, MoveRejected, TouchSession


def after(board: chess.Board, uci: str) -> frozenset[str]:
    result = board.copy(stack=False)
    result.push_uci(uci)
    return occupied_squares(result)


class FakeGantry:
    """Stands in for pi_gantry.GantryPlayer: records every call in order, moves nothing.

    Like the real one it never takes a captured piece off the board: `removal_needed` names the piece
    the person must remove first (captures and en passant), `play` only carries, and `reset` returns
    the notes for the pieces the person took during the game (`reset_notes`)."""

    def __init__(self, log=None, fail_play: str | None = None, fail_reset: str | None = None,
                 reset_notes=(), fail_human: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.log = log if log is not None else []
        self.fail_play = fail_play
        self.fail_reset = fail_reset
        self.fail_human = fail_human
        self.reset_notes = list(reset_notes)
        self.closed = False

    def _note(self, name, *args) -> None:
        self.calls.append((name, *args))
        self.log.append(name)

    def connect(self) -> None:
        self._note("connect")

    def new_game(self, board=None) -> None:
        self._note("new_game", (board or chess.Board()).fen())

    def human_moved(self, board_before, move) -> None:
        self._note("human_moved", board_before.fen(), move.uci())
        if self.fail_human:
            raise GantryError(self.fail_human)

    def removal_needed(self, board_before, move):
        """(square, symbol) of the piece `move` captures, or None. A question, so it is not in `log`."""
        self.calls.append(("removal_needed", board_before.fen(), move.uci()))
        if not board_before.is_capture(move):
            return None
        square = move.to_square
        if board_before.is_en_passant(move):
            square = chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square))
        return chess.square_name(square), board_before.piece_at(square).symbol()

    def play(self, board_before, move, say=None) -> list[str]:
        self._note("play", board_before.fen(), move.uci())
        if self.fail_play:
            raise GantryError(self.fail_play)
        if say is not None:
            say(f"Carrying {move.uci()}")
        return [f"Carried {move.uci()}."]

    def reset(self, say=None) -> list[str]:
        self._note("reset")
        if self.fail_reset:
            raise GantryError(self.fail_reset)
        if say is not None:
            say("Carrying the pieces home")
        return list(self.reset_notes)

    def expected_squares(self) -> set[str]:
        return set()

    def close(self) -> None:
        self.closed = True
        self._note("close")

    @property
    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class TouchSessionTest(unittest.TestCase):
    def test_setup_and_human_move(self):
        session = TouchSession("beginner")
        with self.assertRaises(MoveRejected):
            session.verify_setup(frozenset())
        session.verify_setup(occupied_squares(session.game.board))
        self.assertEqual(session.phase, "human")
        with self.assertRaises(MoveRejected):
            session.scan_human(occupied_squares(session.game.board))
        with self.assertRaises(MoveRejected):
            session.scan_human(frozenset({"e4"}))
        self.assertEqual(session.scan_human(after(session.game.board, "e2e4")), "e4")
        self.assertEqual(session.phase, "thinking")

    def test_computer_move_requires_physical_verification(self):
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(session.game.board))
        session.scan_human(after(session.game.board, "e2e4"))
        move = chess.Move.from_uci("e7e5")
        self.assertEqual(session.announce_computer(move), "e5")
        with self.assertRaises(MoveRejected):
            session.verify_computer(occupied_squares(session.game.board))
        self.assertEqual(session.game.board.turn, chess.BLACK)
        self.assertEqual(session.verify_computer(after(session.game.board, "e7e5")), "e5")
        self.assertEqual(session.phase, "human")

    def test_promotion_choice_and_result(self):
        session = TouchSession("beginner")
        session.game.board.set_fen("8/P7/8/8/8/8/7k/K7 w - - 0 1")
        session.phase = "human"
        self.assertIsNone(session.scan_human(after(session.game.board, "a7a8q")))
        self.assertEqual(session.phase, "choose")
        self.assertEqual({move.promotion for move in session.choices},
                         {chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT})
        self.assertEqual(session.select_move(chess.Move.from_uci("a7a8q")), "a8=Q")
        self.assertEqual(session.game.board.piece_at(chess.A8).piece_type, chess.QUEEN)

    def test_resign(self):
        session = TouchSession("advanced")
        session.resign()
        self.assertEqual(session.phase, "finished")
        self.assertIn("Stockfish wins", session.result)

    def test_checkmate_announces_winner(self):
        session = TouchSession("beginner")
        session.game.board.set_fen("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        session.phase = "human"
        self.assertEqual(session.scan_human(after(session.game.board, "f7f8")), "Qf8#")
        self.assertEqual(session.phase, "finished")
        self.assertEqual(session.result, "You win by checkmate!")

    def test_without_a_gantry_nothing_changes(self):
        session = TouchSession("beginner")
        self.assertIsNone(session.gantry)
        self.assertFalse(session.layout_known)
        self.assertEqual(session.put_pieces_back(), [])
        with self.assertRaises(ValueError):
            session.carry_computer_move()
        # A capture without a gantry: no remove phase, the person makes the whole move by hand as before.
        session.game.board.set_fen(GantrySessionTest.EXCHANGE)
        session.verify_setup(occupied_squares(session.game.board))
        session.scan_human(after(session.game.board, "e4d5"))
        self.assertEqual(session.announce_computer(chess.Move.from_uci("d8d5")), "Qxd5")
        self.assertEqual((session.phase, session.removal, session.gantry_note), ("computer", None, None))
        self.assertEqual(session.verify_computer(after(session.game.board, "d8d5")), "Qxd5")


class GantrySessionTest(unittest.TestCase):
    """The game with a gantry: it learns the start position, follows the person's move, carries
    Stockfish's move, and the camera checks the board after every move either way."""

    def start(self, gantry) -> TouchSession:
        session = TouchSession("beginner", gantry=gantry)
        session.verify_setup(occupied_squares(session.game.board))
        return session

    def test_order_human_moved_then_stockfish_then_play_then_camera(self):
        gantry = FakeGantry()
        session = self.start(gantry)
        self.assertTrue(session.layout_known)
        self.assertEqual(gantry.calls, [("new_game", chess.Board().fen())], "the start position once the camera verified it")
        start_fen = session.game.board.fen()
        self.assertEqual(session.scan_human(after(session.game.board, "e2e4")), "e4")
        self.assertEqual(gantry.calls[-1], ("human_moved", start_fen, "e2e4"), "the person's move, with the board before it")
        self.assertEqual(session.phase, "thinking")
        gantry.log.append("stockfish")
        move = chess.Move.from_uci("e7e5")
        self.assertEqual(session.announce_computer(move), "e5")
        self.assertEqual(session.phase, "computer")
        said = []
        notes = session.carry_computer_move(say=said.append)
        self.assertEqual(gantry.calls[-1], ("play", session.game.board.fen(), "e7e5"))
        self.assertEqual((notes, said), (["Carried e7e5."], ["Carrying e7e5"]))
        self.assertEqual(session.game.board.turn, chess.BLACK, "the position does not move until the camera agrees")
        session.gantry_moved(notes)
        gantry.log.append("camera")
        with self.assertRaises(MoveRejected):
            session.verify_computer(occupied_squares(session.game.board))
        self.assertEqual(session.phase, "computer", "a mismatch keeps the move pending until the pieces are fixed")
        self.assertEqual(session.verify_computer(after(session.game.board, "e7e5")), "e5")
        self.assertEqual(session.phase, "human")
        self.assertEqual(session.game.board.turn, chess.WHITE)
        self.assertEqual(gantry.log, ["new_game", "human_moved", "stockfish", "play", "camera"])
        self.assertEqual(session.gantry_notes, ("Carried e7e5.",))

    def test_a_gantry_error_falls_back_to_moving_by_hand(self):
        gantry = FakeGantry(fail_play="The gantry lost a step.")
        session = self.start(gantry)
        session.scan_human(after(session.game.board, "e2e4"))
        session.announce_computer(chess.Move.from_uci("e7e5"))
        with self.assertRaises(GantryError) as failed:
            session.carry_computer_move()
        session.gantry_failed(str(failed.exception))
        self.assertEqual((session.phase, session.gantry_note), ("computer", "The gantry lost a step."))
        # The person makes the move by hand; the camera check is the same, and the gantry's picture follows the camera.
        self.assertEqual(session.verify_computer(after(session.game.board, "e7e5")), "e5")
        self.assertEqual(session.phase, "human")
        self.assertIsNone(session.gantry_note)
        self.assertEqual(gantry.calls[-1], ("new_game", session.game.board.fen()))

    def test_a_gantry_that_loses_track_of_the_persons_move_follows_the_board(self):
        gantry = FakeGantry(fail_human="The pieces do not match the game.")
        session = self.start(gantry)
        self.assertEqual(session.scan_human(after(session.game.board, "e2e4")), "e4", "the person's move still counts")
        self.assertEqual(session.phase, "thinking")
        self.assertEqual(gantry.calls[-1], ("new_game", session.game.board.fen()))

    def test_the_pieces_go_home_when_the_game_ends(self):
        gantry = FakeGantry()
        session = TouchSession("beginner", gantry=gantry)
        self.assertEqual(session.put_pieces_back(), [], "before the start position was verified the gantry knows nothing")
        self.assertEqual(gantry.names, [])
        session.game.board.set_fen("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        session.verify_setup(occupied_squares(session.game.board))
        self.assertEqual(session.scan_human(after(session.game.board, "f7f8")), "Qf8#")
        self.assertEqual(session.phase, "finished")
        said = []
        self.assertEqual(session.put_pieces_back(say=said.append), [])
        self.assertEqual(gantry.names, ["new_game", "human_moved", "reset"])
        self.assertEqual(said, ["Carrying the pieces home"])
        # Resigning mid-game is the same; the pieces the person took during the game come back as notes.
        session = self.start(FakeGantry(reset_notes=["Put a black pawn on e7."]))
        session.scan_human(after(session.game.board, "e2e4"))
        session.resign()
        self.assertEqual(session.put_pieces_back(), ["Put a black pawn on e7."])
        self.assertEqual(session.gantry.names[-1], "reset")

    def test_the_start_known_from_the_calibration_tells_the_gantry(self):
        gantry = FakeGantry()
        session = TouchSession("beginner", gantry=gantry)
        session.phase = "human"   # as the app does when the calibration already verified the start
        session.gantry_start_known()
        self.assertTrue(session.layout_known)
        self.assertEqual(gantry.calls, [("new_game", chess.Board().fen())])
        TouchSession("beginner").gantry_start_known()   # without a gantry: nothing

    def test_promotion_and_capture_moves_reach_the_gantry_as_chess_moves(self):
        gantry = FakeGantry()
        session = TouchSession("beginner", gantry=gantry)
        session.game.board.set_fen("8/P7/8/8/8/8/7k/K7 w - - 0 1")
        session.verify_setup(occupied_squares(session.game.board))
        self.assertIsNone(session.scan_human(after(session.game.board, "a7a8q")))
        self.assertEqual(session.phase, "choose")
        self.assertEqual(session.select_move(chess.Move.from_uci("a7a8n")), "a8=N")
        self.assertEqual(gantry.calls[-1][0::2], ("human_moved", "a7a8n"))

    # After 1.e4 d5 the person plays exd5 and Stockfish answers Qxd5: a capture the gantry cannot make
    # until the white pawn is off d5.
    EXCHANGE = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2"

    def capture_pending(self, gantry) -> TouchSession:
        session = TouchSession("beginner", gantry=gantry)
        session.game.board.set_fen(self.EXCHANGE)
        session.verify_setup(occupied_squares(session.game.board))
        self.assertEqual(session.scan_human(after(session.game.board, "e4d5")), "exd5")
        self.assertEqual(session.announce_computer(chess.Move.from_uci("d8d5")), "Qxd5")
        return session

    def test_a_capture_waits_for_the_person_to_take_the_piece_off(self):
        gantry = FakeGantry()
        session = self.capture_pending(gantry)
        board = session.game.board
        self.assertEqual(session.phase, "remove")
        self.assertEqual(session.removal, ("d5", "P"))
        self.assertEqual(session.removal_text(), "the white pawn off d5")
        self.assertEqual(gantry.calls[-1], ("removal_needed", board.fen(), "d8d5"), "asked with the board before the move")
        with self.assertRaises(ValueError):
            session.carry_computer_move()   # the gantry never plays before the camera saw the square empty
        # First Check: the pawn is still there. Nothing moves; the phase stays until the camera sees it gone.
        with self.assertRaises(MoveRejected) as still:
            session.verify_removal(occupied_squares(board))
        self.assertEqual(str(still.exception), "d5 still has a piece on it. Take the white pawn off d5, then tap Check.")
        self.assertEqual(session.phase, "remove")
        self.assertNotIn("play", gantry.names)
        # Second Check: d5 is empty, the queen still on d8. Now the gantry carries the move, once.
        self.assertEqual(session.verify_removal(occupied_squares(board) - {"d5"}), "d5")
        self.assertEqual(session.phase, "computer")
        notes = session.carry_computer_move()
        self.assertEqual(gantry.calls[-1], ("play", board.fen(), "d8d5"))
        self.assertEqual(gantry.names.count("play"), 1)
        self.assertEqual(board.turn, chess.BLACK, "the position moves only when the camera agrees")
        session.gantry_moved(notes)
        # The same camera verification as for any computer move.
        self.assertEqual(session.verify_computer(after(board, "d8d5")), "Qxd5")
        self.assertEqual(session.phase, "human")
        self.assertIsNone(session.removal)
        self.assertEqual(gantry.log, ["new_game", "human_moved", "play"], "nothing was carried before the check passed")

    def test_the_removal_check_reports_other_changes_and_unclear_squares(self):
        session = self.capture_pending(FakeGantry())
        occupied = occupied_squares(session.game.board)
        with self.assertRaises(MoveRejected) as changed:
            session.verify_removal((occupied - {"d5", "a2"}) | {"a3"})
        self.assertIn("missing on a2", str(changed.exception))
        self.assertIn("extra on a3", str(changed.exception))
        self.assertIn("take the white pawn off d5, then tap Check", str(changed.exception))
        with self.assertRaises(MoveRejected) as unclear:
            session.verify_removal(Observation(occupied - {"d5"}, ("d5",), {}))
        self.assertIn("cannot tell whether d5 is empty", str(unclear.exception))
        with self.assertRaises(MoveRejected) as too_many:
            session.verify_removal(Observation(occupied - {"d5"}, ("a1", "b1", "c1"), {}))
        self.assertIn("Too many unclear squares", str(too_many.exception))
        self.assertEqual(session.phase, "remove")
        self.assertEqual(session.verify_removal(Observation(occupied - {"d5"}, ("h8",), {})), "d5",
                         "an unclear square away from the capture does not matter")
        with self.assertRaises(ValueError):
            session.verify_removal(occupied - {"d5"})   # only in the remove phase

    def test_the_removal_check_with_a_temporal_calibration(self):
        session = self.capture_pending(FakeGantry())
        quiet = {square: 2.0 for square in chess.SQUARE_NAMES}
        with self.assertRaises(MoveRejected) as unchanged:
            session.verify_removal_temporal(TemporalObservation((), quiet))
        self.assertIn("does not see d5 emptied", str(unchanged.exception))
        with self.assertRaises(MoveRejected):   # another square changed as much: not a clean removal
            session.verify_removal_temporal(TemporalObservation(("d5", "a2"), dict(quiet, d5=30.0, a2=28.0)))
        self.assertEqual(session.phase, "remove")
        self.assertEqual(session.verify_removal_temporal(TemporalObservation(("d5",), dict(quiet, d5=30.0))), "d5")
        self.assertEqual(session.phase, "computer")

    def test_en_passant_names_the_pawn_beside_the_destination(self):
        gantry = FakeGantry()
        session = TouchSession("beginner", gantry=gantry)
        session.game.board.set_fen("rnbqkbnr/pppp1ppp/8/8/4p3/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        session.verify_setup(occupied_squares(session.game.board))
        session.scan_human(after(session.game.board, "d2d4"))
        self.assertEqual(session.announce_computer(chess.Move.from_uci("e4d3")), "exd3")
        self.assertEqual((session.phase, session.removal), ("remove", ("d4", "P")))
        self.assertEqual(session.verify_removal(occupied_squares(session.game.board) - {"d4"}), "d4")
        session.carry_computer_move()
        self.assertEqual(gantry.calls[-1][0::2], ("play", "e4d3"))

    def test_a_quiet_move_skips_the_remove_phase(self):
        session = self.start(FakeGantry())
        session.scan_human(after(session.game.board, "e2e4"))
        session.announce_computer(chess.Move.from_uci("e7e5"))
        self.assertEqual((session.phase, session.removal), ("computer", None))

    def test_a_gantry_without_removal_needed_never_takes_a_capture(self):
        class OldGantry(FakeGantry):
            removal_needed = None   # an older pi_gantry that would take the captured piece off itself

        session = self.capture_pending(OldGantry())
        self.assertEqual((session.phase, session.removal), ("computer", None))
        self.assertIn("cannot wait for a captured piece", session.gantry_note, "the person makes the capture by hand")
        self.assertEqual(session.verify_computer(after(session.game.board, "d8d5")), "Qxd5")
        self.assertEqual(session.gantry.names[-1], "new_game", "the gantry's picture follows the camera")


if __name__ == "__main__":
    unittest.main()
