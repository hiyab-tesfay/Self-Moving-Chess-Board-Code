import unittest
import tempfile
from pathlib import Path

import chess
import cv2
import numpy as np

from board_vision import VisionError, load_calibration, save_calibration
from move_observation import occupied_squares
from pi_touch import CalibrationFlow, MoveRejected, TouchSession, calibrate_photos


def after(board: chess.Board, uci: str) -> frozenset[str]:
    result = board.copy(stack=False)
    result.push_uci(uci)
    return occupied_squares(result)


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


class CalibrationFlowTest(unittest.TestCase):
    def test_manual_corners_and_saved_calibration(self):
        empty = np.zeros((800, 800, 3), dtype=np.uint8)
        for row in range(8):
            for col in range(8):
                color = (150, 180, 205) if (row + col) % 2 else (70, 105, 125)
                cv2.rectangle(empty, (col * 100, row * 100),
                              ((col + 1) * 100 - 1, (row + 1) * 100 - 1), color, -1)
        starting = empty.copy()
        for square, piece in chess.Board().piece_map().items():
            col, row = chess.square_file(square), 7 - chess.square_rank(square)
            color = (25, 25, 25) if piece.color == chess.BLACK else (245, 245, 245)
            cv2.circle(starting, (col * 100 + 50, row * 100 + 50), 32, color, -1)

        empty_photo = np.zeros((1000, 1200, 3), dtype=np.uint8)
        empty_photo[100:900, 100:900] = empty
        starting_photo = np.zeros((1000, 1200, 3), dtype=np.uint8)
        starting_photo[150:950, 250:1050] = starting

        flow = CalibrationFlow()
        flow.accept_empty(empty_photo, detected_corners=None)
        self.assertEqual(flow.stage, "empty_corners")
        for x, y in ((100, 100), (899, 100), (899, 899)):
            flow.add_corner(x, y)
        flow.undo_corner()
        self.assertEqual(len(flow.empty_corners), 2)
        flow.add_corner(899, 899)
        flow.add_corner(100, 899)
        self.assertEqual(flow.stage, "starting")
        flow.accept_starting(starting_photo, detected_corners=None)
        self.assertEqual(flow.stage, "starting_corners")
        for x, y in ((250, 150), (1049, 150), (1049, 949), (250, 949)):
            flow.add_corner(x, y)
        self.assertEqual(flow.stage, "ready")

        with self.assertRaises(VisionError):
            calibrate_photos(empty_photo, empty_photo,
                             flow.empty_corners, flow.starting_corners)
        with self.assertRaises(VisionError):
            calibrate_photos(empty_photo, starting_photo,
                             flow.empty_corners, flow.empty_corners)
        calibration, reference = calibrate_photos(
            empty_photo, starting_photo, flow.empty_corners, flow.starting_corners)
        with tempfile.TemporaryDirectory() as directory:
            save_calibration(Path(directory), calibration, reference)
            loaded, loaded_reference = load_calibration(Path(directory))
            self.assertEqual(loaded.corners, flow.starting_corners)
            self.assertEqual(loaded.scoring, "local_lighting_v1")
            self.assertEqual(loaded_reference.shape, (800, 800, 3))

    def test_marker_flow_skips_manual_corners(self):
        flow = CalibrationFlow()
        corners = [[0, 0], [9, 0], [9, 9], [0, 9]]
        flow.accept_empty(np.zeros((10, 10, 3), dtype=np.uint8), detected_corners=corners)
        self.assertEqual(flow.stage, "starting")
        flow.accept_starting(np.zeros((10, 10, 3), dtype=np.uint8), detected_corners=corners)
        self.assertEqual(flow.stage, "ready")
        self.assertEqual(flow.empty_corners, corners)
        self.assertEqual(flow.starting_corners, corners)
        flow.retake_starting()
        self.assertEqual(flow.stage, "starting")
        flow.accept_starting(np.zeros((10, 10, 3), dtype=np.uint8), detected_corners=None)
        self.assertEqual(flow.stage, "starting_corners")


if __name__ == "__main__":
    unittest.main()
