"""End-to-end synthetic photo check; real photos are still needed for field tuning."""

import unittest

import chess

try:
    import cv2
    import numpy as np
    from board_vision import (board_corners, calibrate, calibrate_from_position,
                              detect_square_changes, objectness_scores, observe)
    from move_observation import match_legal_moves, match_scored_moves, occupied_squares
except ImportError:
    cv2 = None


@unittest.skipIf(cv2 is None, "Install requirements.txt for image tests")
class BoardVisionTests(unittest.TestCase):
    def test_printed_marker_geometry_finds_board_corners(self) -> None:
        image = np.full((1200, 1200, 3), 255, dtype=np.uint8)
        cv2.rectangle(image, (200, 200), (999, 999), (150, 150, 150), -1)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        for marker_id, (x, y) in enumerate(((80, 80), (1000, 80), (1000, 1000), (80, 1000))):
            marker = cv2.aruco.generateImageMarker(dictionary, marker_id, 100)
            sticker = cv2.copyMakeBorder(marker, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
            image[y:y + 120, x:x + 120] = cv2.cvtColor(sticker, cv2.COLOR_GRAY2BGR)
        np.testing.assert_allclose(board_corners(image),
                                   [[200, 200], [1000, 200], [1000, 1000], [200, 1000]], atol=2)

    def image(self, board: chess.Board | None):
        image = np.zeros((800, 800, 3), dtype=np.uint8)
        for row in range(8):
            for col in range(8):
                color = (195, 205, 215) if (row + col) % 2 else (85, 105, 115)
                cv2.rectangle(image, (col * 100, row * 100), ((col + 1) * 100 - 1, (row + 1) * 100 - 1), color, -1)
                square = chess.square(col, 7 - row)
                piece = board.piece_at(square) if board else None
                if piece:
                    piece_color = (245, 245, 245) if piece.color else (20, 20, 20)
                    cv2.circle(image, (col * 100 + 50, row * 100 + 50), 24, piece_color, -1)
        return image

    def test_calibration_scan_and_move(self) -> None:
        board = chess.Board()
        corners = [[0, 0], [799, 0], [799, 799], [0, 799]]
        calibration, reference, _ = calibrate(self.image(None), self.image(board), corners)
        board.push_uci("e2e4")
        observation, _ = observe(reference, self.image(board), calibration)
        self.assertEqual(observation.uncertain, ())
        self.assertEqual(observation.occupied, occupied_squares(board))
        initial = chess.Board()
        result = match_legal_moves(initial, observation.occupied)
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.candidates[0].uci(), "e2e4")

    def test_known_position_pair_accepts_move_and_rejects_illegal_pattern(self) -> None:
        board = chess.Board()
        before_scores = objectness_scores(self.image(board))
        thresholds = calibrate_from_position(before_scores, board)
        moved = board.copy()
        moved.push_uci("e2e3")
        moved_scores = objectness_scores(self.image(moved))
        result = match_scored_moves(board, moved_scores, thresholds)
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.candidates[0].uci(), "e2e3")
        changes = detect_square_changes(before_scores, moved_scores, occupied_squares(board),
                                        thresholds, min_change=15)
        self.assertEqual(changes.vacated, ("e2",))
        self.assertEqual(changes.filled, ("e3",))

        illegal = board.copy()
        illegal.remove_piece_at(chess.F8)
        illegal.set_piece_at(chess.C5, chess.Piece(chess.BISHOP, chess.BLACK))
        illegal_scores = objectness_scores(self.image(illegal))
        result = match_scored_moves(board, illegal_scores, thresholds)
        self.assertEqual(result.status, "no_match")
        changes = detect_square_changes(before_scores, illegal_scores, occupied_squares(board),
                                        thresholds, min_change=15)
        self.assertEqual(changes.vacated, ("f8",))
        self.assertEqual(changes.filled, ("c5",))


if __name__ == "__main__":
    unittest.main()
