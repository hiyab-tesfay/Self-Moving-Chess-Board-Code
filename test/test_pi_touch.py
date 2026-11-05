import unittest

import chess

from move_observation import occupied_squares
from pi_touch import GantryError, MoveRejected, TouchSession


def after(board: chess.Board, uci: str) -> frozenset[str]:
    result = board.copy(stack=False)
    result.push_uci(uci)
    return occupied_squares(result)


class FakeGantry:
    """Stands in for pi_gantry.GantryPlayer: records every call in order, moves nothing."""

    def __init__(self, log=None, fail_play: str | None = None, fail_reset: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.log = log if log is not None else []
        self.fail_play = fail_play
        self.fail_reset = fail_reset
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
        return ["Every piece is home."]

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


class GantrySessionTest(unittest.TestCase):
    """The in-person game with a gantry: it learns the start position from the camera, follows the person's
    move, carries Stockfish's move, and the camera checks the board after every move either way."""

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
        with self.assertRaises(MoveRejected) as rejected:
            session.verify_computer(occupied_squares(session.game.board))
        self.assertIn("missing on e5", str(rejected.exception))
        self.assertIn("extra on e7", str(rejected.exception))
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

    def test_the_pieces_go_home_when_the_game_ends(self):
        gantry = FakeGantry()
        session = TouchSession("beginner", gantry=gantry)
        self.assertEqual(session.put_pieces_back(), [], "before the camera verified the start position the gantry knows nothing")
        self.assertEqual(gantry.names, [])
        session.game.board.set_fen("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        session.verify_setup(occupied_squares(session.game.board))
        self.assertEqual(session.scan_human(after(session.game.board, "f7f8")), "Qf8#")
        self.assertEqual(session.phase, "finished")
        said = []
        self.assertEqual(session.put_pieces_back(say=said.append), ["Every piece is home."])
        self.assertEqual(gantry.names, ["new_game", "human_moved", "reset"])
        self.assertEqual(said, ["Carrying the pieces home"])
        # Resigning mid-game is the same.
        session = self.start(FakeGantry())
        session.scan_human(after(session.game.board, "e2e4"))
        session.resign()
        self.assertEqual(session.put_pieces_back(), ["Every piece is home."])
        self.assertEqual(session.gantry.names[-1], "reset")

    def test_promotion_and_capture_moves_reach_the_gantry_as_chess_moves(self):
        gantry = FakeGantry()
        session = TouchSession("beginner", gantry=gantry)
        session.game.board.set_fen("8/P7/8/8/8/8/7k/K7 w - - 0 1")
        session.verify_setup(occupied_squares(session.game.board))
        self.assertIsNone(session.scan_human(after(session.game.board, "a7a8q")))
        self.assertEqual(session.phase, "choose")
        self.assertEqual(session.select_move(chess.Move.from_uci("a7a8n")), "a8=N")
        self.assertEqual(gantry.calls[-1][0::2], ("human_moved", "a7a8n"))


if __name__ == "__main__":
    unittest.main()
