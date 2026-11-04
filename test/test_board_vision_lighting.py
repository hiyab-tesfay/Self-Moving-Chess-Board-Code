import unittest

import chess
import cv2
import numpy as np

from board_vision import (Calibration, calibrate, legacy_square_scores, observe,
                          square_scores)
from move_observation import occupied_squares


CORNERS = [[0, 0], [799, 0], [799, 799], [0, 799]]


def board_photo(board: chess.Board | None = None) -> np.ndarray:
    photo = np.empty((800, 800, 3), dtype=np.uint8)
    for row in range(8):
        for col in range(8):
            color = (145, 165, 180) if (row + col) % 2 else (75, 95, 110)
            cv2.rectangle(photo, (col * 100, row * 100),
                          ((col + 1) * 100 - 1, (row + 1) * 100 - 1), color, -1)
    xx = np.arange(800, dtype=np.float32)[None, :]
    yy = np.arange(800, dtype=np.float32)[:, None]
    grain = 3 * np.sin(xx / 7) + 2 * np.sin(yy / 11)
    photo = np.clip(photo.astype(np.float32) + grain[:, :, None], 0, 255).astype(np.uint8)
    if board is not None:
        for square, piece in board.piece_map().items():
            col, row = chess.square_file(square), 7 - chess.square_rank(square)
            color = (25, 30, 35) if piece.color == chess.BLACK else (220, 225, 230)
            cv2.circle(photo, (col * 100 + 50, row * 100 + 50), 32, color, -1)
    return photo


def under_light(photo: np.ndarray, *, offset: float, slope: float,
                gain: float = 1.0) -> np.ndarray:
    x = np.linspace(-1, 1, 800, dtype=np.float32)[None, :]
    y = np.linspace(-1, 1, 800, dtype=np.float32)[:, None]
    illumination = offset + slope * (x + y)
    return np.clip(photo.astype(np.float32) * gain + illumination[:, :, None],
                   0, 255).astype(np.uint8)


class LightingCorrectionTest(unittest.TestCase):
    def test_calibration_and_moves_survive_gradual_light_changes(self):
        empty = board_photo()
        starting_board = chess.Board()
        lit_starting = under_light(board_photo(starting_board), offset=30, slope=14, gain=1.08)
        calibration, reference, _ = calibrate(empty, lit_starting, CORNERS)
        self.assertEqual(calibration.scoring, "local_lighting_v1")

        starting_observation, _ = observe(reference, lit_starting, calibration, CORNERS)
        self.assertFalse(starting_observation.uncertain)
        self.assertEqual(starting_observation.occupied, occupied_squares(starting_board))

        moved_board = starting_board.copy(stack=False)
        moved_board.push_uci("e2e4")
        lit_move = under_light(board_photo(moved_board), offset=-15, slope=-12, gain=.92)
        move_observation, _ = observe(reference, lit_move, calibration, CORNERS)
        self.assertFalse(move_observation.uncertain)
        self.assertEqual(move_observation.occupied, occupied_squares(moved_board))

        lit_empty = under_light(empty, offset=38, slope=-10, gain=1.12)
        empty_observation, _ = observe(reference, lit_empty, calibration, CORNERS)
        self.assertEqual(empty_observation.occupied, frozenset())
        self.assertGreater(max(legacy_square_scores(reference, lit_empty).values()),
                           max(square_scores(reference, lit_empty).values()) + 20)

    def test_old_calibration_keeps_old_scoring(self):
        old_data = {"corners": CORNERS, "thresholds": [10, 10],
                    "margins": [1, 1], "rotation": "none"}
        old_calibration = Calibration.from_json(old_data)
        self.assertEqual(old_calibration.scoring, "legacy")
        empty = board_photo()
        brighter = under_light(empty, offset=40, slope=0)
        observation, _ = observe(empty, brighter, old_calibration, CORNERS)
        self.assertEqual(len(observation.occupied), 64)


if __name__ == "__main__":
    unittest.main()
