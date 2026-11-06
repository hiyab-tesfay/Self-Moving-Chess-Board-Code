import unittest

import chess
import cv2
import numpy as np

from board_vision import (Calibration, calibrate, legacy_square_scores, observe,
                          observe_temporal, square_scores, robust_square_scores, VisionError, warp_board)
from move_observation import occupied_squares
from pi_touch import (CalibrationFlow, PhotoAdjustments, TouchSession, calibrate_setup_photo,
                      calibrate_photos, suggest_setup_rotation, MoveRejected)


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
    def test_corrects_small_camera_shift_but_rejects_large_shift(self):
        calibration, reference, _ = calibrate(board_photo(), board_photo(chess.Board()), CORNERS)
        board = chess.Board()
        board.push_uci("e2e4")
        for dx, dy in ((3, -2), (7, 5)):
            shifted = cv2.warpAffine(board_photo(board), np.float32([[1, 0, dx], [0, 1, dy]]),
                                     (800, 800), borderMode=cv2.BORDER_REPLICATE)
            observed, _ = observe(reference, shifted, calibration, CORNERS)
            self.assertEqual(observed.occupied, occupied_squares(board))
            self.assertFalse(observed.uncertain)
        moved_camera = cv2.warpAffine(board_photo(board), np.float32([[1, 0, 25], [0, 1, 0]]),
                                      (800, 800), borderMode=cv2.BORDER_REPLICATE)
        with self.assertRaises(VisionError):
            observe(reference, moved_camera, calibration, CORNERS)

    def test_smooth_shadow_does_not_create_pieces(self):
        calibration, reference, _ = calibrate(board_photo(), board_photo(chess.Board()), CORNERS)
        board = chess.Board()
        board.push_uci("e2e4")
        yy, xx = np.mgrid[:800, :800]
        shadow = 1 - .35 * np.exp(-((xx - 350) ** 2 / 330 ** 2 + (yy - 400) ** 2 / 600 ** 2))
        photo = np.clip(board_photo(board).astype(float) * shadow[:, :, None] + 12, 0, 255).astype(np.uint8)
        observed, _ = observe(reference, photo, calibration, CORNERS)
        self.assertEqual(observed.occupied, occupied_squares(board))
        self.assertFalse(observed.uncertain)

    def test_small_low_contrast_piece_is_detected(self):
        empty = board_photo()
        def small_pieces(board):
            photo = empty.copy()
            for square, piece in board.piece_map().items():
                x = chess.square_file(square) * 100 + 50
                y = (7 - chess.square_rank(square)) * 100 + 50
                color = tuple(int(np.clip(int(value) + (22 if piece.color else -24), 0, 255))
                              for value in photo[y, x])
                cv2.circle(photo, (x, y), 14, color, -1)
            return photo
        board = chess.Board()
        calibration, reference, _ = calibrate(empty, small_pieces(board), CORNERS)
        board.push_uci("e2e4")
        observed, _ = observe(reference, small_pieces(board), calibration, CORNERS)
        self.assertEqual(observed.occupied, occupied_squares(board))

    def test_invalid_corner_order_is_rejected(self):
        for corners in ([[0, 0], [799, 799], [799, 0], [0, 799]],
                        [[0, 0], [0, 799], [799, 799], [799, 0]],
                        [[0, 0], [799, 0], [float("nan"), 799], [0, 799]]):
            with self.assertRaises(VisionError):
                warp_board(board_photo(), corners)

    def test_castling_and_en_passant_remain_verifiable(self):
        calibration, reference, _ = calibrate(board_photo(), board_photo(chess.Board()), CORNERS)
        for fen, uci in (("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
                         ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6")):
            session = TouchSession("beginner")
            session.game.board = chess.Board(fen)
            session.phase = "human"
            before = board_photo(session.game.board)
            moved = session.game.board.copy()
            moved.push_uci(uci)
            photo = board_photo(moved)
            observed, _ = observe(reference, photo, calibration, CORNERS)
            session.scan_human(observed, change_scores=robust_square_scores(before, photo))
            self.assertEqual(session.game.board.fen(), moved.fen())

    def test_optional_empty_board_calibration_supports_rotated_camera(self):
        empty = cv2.rotate(board_photo(), cv2.ROTATE_180)
        starting = cv2.rotate(board_photo(chess.Board()), cv2.ROTATE_180)
        calibration, reference = calibrate_photos(
            empty, starting, CORNERS, CORNERS, PhotoAdjustments(), "180")
        self.assertEqual(calibration.rotation, "180")
        observation, _ = observe(reference, starting, calibration, CORNERS)
        self.assertEqual(observation.occupied, occupied_squares(chess.Board()))

    def test_setup_photo_suggests_white_at_bottom(self):
        canonical = board_photo(chess.Board())
        settings = PhotoAdjustments()
        cases = ((canonical, "none"),
                 (cv2.rotate(canonical, cv2.ROTATE_90_CLOCKWISE), "ccw"),
                 (cv2.rotate(canonical, cv2.ROTATE_180), "180"),
                 (cv2.rotate(canonical, cv2.ROTATE_90_COUNTERCLOCKWISE), "cw"))
        for photo, expected in cases:
            with self.subTest(rotation=expected):
                self.assertEqual(suggest_setup_rotation(photo, CORNERS, settings), expected)
        blank = np.full((800, 800, 3), 120, dtype=np.uint8)
        self.assertIsNone(suggest_setup_rotation(blank, CORNERS, settings))

    def test_default_setup_corner_view_has_white_at_bottom(self):
        board = chess.Board()
        flow = CalibrationFlow(setup_only=True)
        setup = cv2.rotate(board_photo(board), cv2.ROTATE_180)
        calibration, reference = calibrate_setup_photo(
            setup, CORNERS, PhotoAdjustments(), rotation=flow.rotation)
        session = TouchSession("beginner")
        session.phase = "human"
        board.push_uci("e2e4")
        moved = cv2.rotate(board_photo(board), cv2.ROTATE_180)
        observation, _ = observe_temporal(reference, moved, calibration, CORNERS)
        self.assertEqual(calibration.rotation, "180")
        self.assertEqual(observation.changed, ("e2", "e4"))
        self.assertEqual(session.scan_human_temporal(observation), "e4")

    def test_white_home_row_on_photo_right_edge(self):
        board = chess.Board()
        setup = cv2.rotate(board_photo(board), cv2.ROTATE_90_COUNTERCLOCKWISE)
        calibration, reference = calibrate_setup_photo(
            setup, CORNERS, PhotoAdjustments(), rotation="cw")
        self.assertEqual(calibration.rotation, "cw")
        session = TouchSession("beginner")
        session.phase = "human"
        board.push_uci("e2e4")
        moved = cv2.rotate(board_photo(board), cv2.ROTATE_90_COUNTERCLOCKWISE)
        observation, _ = observe_temporal(reference, moved, calibration, CORNERS)
        self.assertEqual(observation.changed, ("e2", "e4"))
        self.assertEqual(session.scan_human_temporal(observation), "e4")

    def test_setup_photo_alone_tracks_both_sides_from_verified_positions(self):
        board = chess.Board()
        setup = under_light(board_photo(board), offset=10, slope=8, gain=1.03)
        calibration, reference = calibrate_setup_photo(setup, CORNERS, PhotoAdjustments())
        self.assertEqual(calibration.scoring, "temporal_v1")

        session = TouchSession("beginner")
        session.phase = "human"  # The user confirmed the photographed starting position.
        board.push_uci("e2e4")
        human_photo = under_light(board_photo(board), offset=-10, slope=-7, gain=.97)
        human_observation, current_reference = observe_temporal(
            reference, human_photo, calibration, CORNERS)
        self.assertEqual(human_observation.changed, ("e2", "e4"))
        self.assertEqual(session.scan_human_temporal(human_observation), "e4")

        move = chess.Move.from_uci("e7e5")
        session.announce_computer(move)
        board.push(move)
        computer_photo = under_light(board_photo(board), offset=14, slope=6, gain=1.05)
        computer_observation, _ = observe_temporal(
            current_reference, computer_photo, calibration, CORNERS)
        self.assertEqual(computer_observation.changed, ("e5", "e7"))
        self.assertEqual(session.verify_computer_temporal(computer_observation), "e5")

    def test_setup_only_rejects_multiple_moves(self):
        board = chess.Board()
        calibration, reference = calibrate_setup_photo(board_photo(board), CORNERS,
                                                       PhotoAdjustments())
        board.push_uci("e2e4")
        board.push_uci("e7e5")
        board.push_uci("d2d4")
        observed, _ = observe_temporal(reference, board_photo(board), calibration, CORNERS)
        session = TouchSession("beginner")
        session.phase = "human"
        with self.assertRaises(MoveRejected):
            session.scan_human_temporal(observed)
        self.assertEqual(session.game.board.fullmove_number, 1)

    def test_calibration_and_moves_survive_gradual_light_changes(self):
        empty = board_photo()
        starting_board = chess.Board()
        lit_starting = under_light(board_photo(starting_board), offset=30, slope=14, gain=1.08)
        calibration, reference, _ = calibrate(empty, lit_starting, CORNERS)
        self.assertEqual(calibration.scoring, "robust_occupancy_v2")

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

    def test_move_photo_can_use_its_own_corners(self):
        empty = board_photo()
        board = chess.Board()
        calibration, reference, _ = calibrate(empty, board_photo(board), CORNERS)
        board.push_uci("e2e4")
        shifted_photo = np.zeros((1000, 1200, 3), dtype=np.uint8)
        shifted_photo[130:930, 270:1070] = board_photo(board)
        corners = [[270, 130], [1069, 130], [1069, 929], [270, 929]]
        observation, _ = observe(reference, shifted_photo, calibration, corners)
        self.assertEqual(observation.occupied, occupied_squares(board))


if __name__ == "__main__":
    unittest.main()
