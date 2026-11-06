import base64
import unittest
import tempfile
import queue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import chess
import cv2
import numpy as np

from board_vision import (Calibration, Observation, TemporalObservation, VisionError, load_calibration,
                          observe, save_calibration)
from move_observation import occupied_squares
from pi_touch import (CalibrationFlow, MoveRejected, PhotoAdjustments, TouchApp, TouchSession,
                      adjust_photo, calibrate_photos, computer_move_feedback, rejected_move_feedback,
                      reusable_corners, should_retry_scan, temporal_computer_move_feedback,
                      photo_square_center, corners_from_rotated_preview,
                      corners_to_rotated_preview)


def after(board: chess.Board, uci: str) -> frozenset[str]:
    result = board.copy(stack=False)
    result.push_uci(uci)
    return occupied_squares(result)


def unclear(occupied: frozenset[str], *squares: str) -> Observation:
    return Observation(occupied - set(squares), tuple(squares), {})


class TouchSessionTest(unittest.TestCase):
    def test_multiple_piece_feedback_names_all_changed_sources(self):
        board = chess.Board()
        observed = Observation((occupied_squares(board) - {"d2", "e2"}) | {"d4", "e4"}, (), {})
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(board))
        with self.assertRaises(MoveRejected):
            session.scan_human(observed)
        feedback = rejected_move_feedback(board, observed, "Illegal or unrecognized move.")
        self.assertEqual(feedback.title, "MULTIPLE PIECES CHANGED")
        self.assertEqual(feedback.sources, ("d2", "e2"))
        self.assertEqual(feedback.destinations, ("d4", "e4"))
        self.assertIn("White pawn d2", feedback.detail)
        self.assertIn("White pawn e2", feedback.detail)
        self.assertFalse(should_retry_scan(feedback, "saved", 1))

    def test_illegal_move_feedback_names_piece_only_for_clear_move(self):
        board = chess.Board()
        observed = Observation((occupied_squares(board) - {"e2"}) | {"e5"}, (), {})
        feedback = rejected_move_feedback(board, observed, "Illegal or unrecognized move.")
        self.assertEqual(feedback.title, "ILLEGAL MOVE")
        self.assertIn("White pawn: e2 → e5", feedback.detail)
        self.assertEqual((feedback.source, feedback.destination), ("e2", "e5"))
        unclear = Observation(observed.occupied, ("e5",), {})
        self.assertNotEqual(rejected_move_feedback(board, unclear, "Photo unclear").title,
                            "ILLEGAL MOVE")

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

    def test_computer_move_feedback_identifies_wrong_destination_and_piece(self):
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(session.game.board))
        session.scan_human(after(session.game.board, "e2e4"))
        move = chess.Move.from_uci("e7e5")
        session.announce_computer(move)
        wrong_square = Observation(after(session.game.board, "e7e6"), (), {})
        with self.assertRaises(MoveRejected):
            session.verify_computer(wrong_square)
        feedback = computer_move_feedback(session.game.board, move, wrong_square)
        self.assertEqual(feedback.title, "COMPUTER MOVE MISMATCH")
        self.assertIn("Expected: Black pawn e7 → e5", feedback.detail)
        self.assertIn("Camera sees: Black pawn e7 → e6", feedback.detail)
        self.assertEqual(feedback.expected_destinations, ("e5",))
        self.assertEqual(feedback.destinations, ("e6",))
        self.assertTrue(should_retry_scan(feedback, "saved", 1))

        wrong_piece = Observation(after(session.game.board, "d7d5"), (), {})
        feedback = computer_move_feedback(session.game.board, move, wrong_piece)
        self.assertIn("Camera sees: Black pawn d7 → d5", feedback.detail)

    def test_computer_move_feedback_shows_castling_rook_and_uncertainty(self):
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1")
        move = chess.Move.from_uci("e8g8")
        observed = Observation((occupied_squares(board) - {"e8"}) | {"g8"}, (), {})
        feedback = computer_move_feedback(board, move, observed)
        self.assertEqual(feedback.expected_sources, ("e8", "h8"))
        self.assertEqual(feedback.expected_destinations, ("g8", "f8"))
        self.assertIn("rook h8 → f8", feedback.detail)
        unclear = Observation(observed.occupied, ("f8",), {})
        feedback = computer_move_feedback(board, move, unclear)
        self.assertEqual(feedback.title, "PHOTO UNCLEAR")
        self.assertIn("f8", feedback.detail)

    def test_unclear_setup_squares_require_one_manual_confirmation(self):
        session = TouchSession("beginner")
        session.verify_setup(unclear(occupied_squares(session.game.board), "f7", "g8"))
        self.assertEqual(session.phase, "setup_confirm")
        self.assertEqual(session.setup_uncertain, ("f7", "g8"))
        session.confirm_setup()
        self.assertEqual(session.phase, "human")

    def test_too_many_unclear_squares_still_reject_setup(self):
        session = TouchSession("beginner")
        observed = unclear(occupied_squares(session.game.board), "f7", "g8", "a7")
        with self.assertRaisesRegex(MoveRejected, "Too many unclear"):
            session.verify_setup(observed)
        self.assertEqual(session.phase, "setup")

    def test_unrelated_unclear_squares_do_not_block_human_move(self):
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(session.game.board))
        observed = unclear(after(session.game.board, "e2e4"), "f7", "g8")
        self.assertEqual(session.scan_human(observed), "e4")
        self.assertEqual(session.phase, "thinking")

    def test_move_using_unclear_square_is_rejected(self):
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(session.game.board))
        observed = unclear(after(session.game.board, "e2e4"), "e2")
        with self.assertRaisesRegex(MoveRejected, "e2"):
            session.scan_human(observed)
        self.assertEqual(session.phase, "human")

    def test_unrelated_unclear_squares_do_not_block_computer_move(self):
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(session.game.board))
        session.scan_human(after(session.game.board, "e2e4"))
        session.announce_computer(chess.Move.from_uci("e7e5"))
        observed = unclear(after(session.game.board, "e7e5"), "f7", "g8")
        self.assertEqual(session.verify_computer(observed), "e5")
        self.assertEqual(session.phase, "human")

    def test_computer_move_using_unclear_square_is_rejected(self):
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(session.game.board))
        session.scan_human(after(session.game.board, "e2e4"))
        session.announce_computer(chess.Move.from_uci("e7e5"))
        observed = unclear(after(session.game.board, "e7e5"), "e7")
        with self.assertRaisesRegex(MoveRejected, "e7"):
            session.verify_computer(observed)
        self.assertEqual(session.phase, "computer")

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
    def test_setup_only_flow_never_requires_empty_board(self):
        flow = CalibrationFlow(setup_only=True)
        self.assertEqual(flow.stage, "setup_only")
        self.assertEqual(flow.rotation, "180")
        photo = np.zeros((800, 800, 3), dtype=np.uint8)
        corners = [[0, 0], [799, 0], [799, 799], [0, 799]]
        flow.accept_starting(photo, corners)
        self.assertEqual(flow.stage, "ready")
        self.assertIsNone(flow.empty_photo)
        flow.suggested_rotation = "cw"
        self.assertEqual(flow.white_side_in_preview(), "left")
        flow.suggested_rotation = "180"
        self.assertEqual(flow.white_side_in_preview(), "bottom")
        a1 = photo_square_center("a1", corners, flow.rotation)
        h1 = photo_square_center("h1", corners, flow.rotation)
        self.assertGreater(a1[0], 700)
        self.assertLess(h1[0], 100)
        self.assertLess(a1[1], 100)
        self.assertLess(h1[1], 100)
        flow.cycle_rotation()
        self.assertEqual(flow.rotation, "ccw")
        self.assertEqual(flow.white_side_in_preview(), "left")
        a1 = photo_square_center("a1", corners, flow.rotation)
        h1 = photo_square_center("h1", corners, flow.rotation)
        self.assertLess(a1[0], 100)
        self.assertLess(h1[0], 100)
        self.assertLess(a1[1], 100)
        self.assertGreater(h1[1], 700)
        flow.flip_rotation()
        self.assertEqual(flow.rotation, "cw")
        flow.flip_rotation()
        self.assertEqual(flow.rotation, "ccw")
        flow.retake_starting()
        self.assertEqual(flow.stage, "setup_only")

    def test_rotated_corner_taps_map_back_to_raw_photo(self):
        width, height = 900, 600
        displayed = [[80, 60], [810, 50], [790, 540], [90, 550]]
        raw = corners_from_rotated_preview(displayed, width, height, "180")
        self.assertEqual(raw, [[109, 59], [809, 49], [819, 539], [89, 549]])
        self.assertEqual(corners_from_rotated_preview(
            [[0, 0], [599, 0], [599, 899], [0, 899]], width, height, "cw"),
            [[0, 0], [899, 0], [899, 599], [0, 599]])
        raw = [[89, 49], [819, 59], [809, 549], [79, 539]]
        for rotation in ("none", "cw", "180", "ccw"):
            shown = corners_to_rotated_preview(raw, width, height, rotation)
            self.assertEqual(corners_from_rotated_preview(
                shown, width, height, rotation), raw)

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
        edits = PhotoAdjustments(brightness=10, contrast=110, warmth=10)
        edited_calibration, edited_reference = calibrate_photos(
            empty_photo, starting_photo, flow.empty_corners, flow.starting_corners, edits)
        edited_observation, _ = observe(
            edited_reference, adjust_photo(starting_photo, edits), edited_calibration,
            flow.starting_corners)
        self.assertEqual(edited_observation.occupied, occupied_squares(chess.Board()))
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

    def test_reused_setup_corners_are_reviewable(self):
        photo = np.zeros((800, 800, 3), dtype=np.uint8)
        corners = [[10, 10], [790, 10], [790, 790], [10, 790]]
        self.assertEqual(reusable_corners(photo, corners), corners)
        self.assertIsNone(reusable_corners(photo, [[900, 10], *corners[1:]]))
        flow = CalibrationFlow()
        flow.accept_empty(photo, corners)
        flow.accept_starting(photo, corners, reused_corners=True)
        self.assertEqual(flow.stage, "ready")
        self.assertFalse(flow.starting_auto_corners)
        flow.reset_starting_corners()
        self.assertEqual(flow.stage, "starting_corners")


class TouchWorkflowTest(unittest.TestCase):
    def test_check_photo_uses_saved_orientation(self):
        photo = np.zeros((800, 800, 3), dtype=np.uint8)
        photo[:100, :100] = (0, 0, 255)
        photo[-100:, -100:] = (0, 255, 0)
        app = TouchApp.__new__(TouchApp)
        app.calibration_flow = None
        app.calibration = Calibration([[0, 0], [799, 0], [799, 799], [0, 799]],
                                      (0.0, 0.0), (0.0, 0.0), rotation="180")
        app.scan_photo = photo
        app.scan_corners = [[0, 0], [799, 0], [799, 799], [0, 799]]
        app.scan_feedback = None
        app.photo_adjustments = PhotoAdjustments()
        app.board_canvas = Mock()
        app.board_canvas.winfo_width.return_value = 800
        app.board_canvas.winfo_height.return_value = 800
        with patch("pi_touch.tk.PhotoImage") as image_widget:
            app.draw_photo_preview()
        encoded = base64.b64decode(image_widget.call_args.kwargs["data"])
        preview = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(preview[20, 20, 1]), 200)
        self.assertLess(int(preview[20, 20, 2]), 50)
        numbered = [call for call in app.board_canvas.create_text.call_args_list
                    if call.kwargs.get("text") == "1"]
        self.assertEqual(len(numbered), 1)
        self.assertLess(numbered[0].args[0], 50)
        self.assertLess(numbered[0].args[1], 50)

    def test_check_photo_corner_taps_map_to_raw_camera(self):
        app = TouchApp.__new__(TouchApp)
        app.calibration = Calibration(None, (0.0, 0.0), (0.0, 0.0), rotation="180")
        app.scan_photo = np.zeros((600, 900, 3), dtype=np.uint8)
        app.scan_corners = []
        app.preview_geometry = (0, 0, 1)
        app.busy = False
        app.say = Mock()
        app.render = Mock()
        app.process_scan_photo = Mock()
        displayed = [[80, 60], [810, 50], [790, 540], [90, 550]]
        for x, y in displayed:
            app.tap_scan_corner(SimpleNamespace(x=x, y=y))
        self.assertEqual(app.scan_corners,
                         [[109, 59], [809, 49], [819, 539], [89, 549]])
        app.process_scan_photo.assert_called_once()

    def test_corner_selection_shows_upright_rotated_setup_photo(self):
        photo = np.zeros((800, 800, 3), dtype=np.uint8)
        photo[:100, :100] = (0, 0, 255)
        photo[-100:, -100:] = (0, 255, 0)
        flow = CalibrationFlow(setup_only=True)
        flow.accept_starting(photo, None)
        app = TouchApp.__new__(TouchApp)
        app.calibration_flow = flow
        app.photo_adjustments = PhotoAdjustments()
        app.board_canvas = Mock()
        app.board_canvas.winfo_width.return_value = 800
        app.board_canvas.winfo_height.return_value = 800
        with patch("pi_touch.tk.PhotoImage") as image_widget:
            app.draw_photo_preview()
        encoded = base64.b64decode(image_widget.call_args.kwargs["data"])
        preview = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(preview[20, 20, 1]), 200)
        self.assertLess(int(preview[20, 20, 2]), 50)
        self.assertIsNotNone(app.preview_geometry)

    def test_corner_taps_on_rotated_view_are_saved_in_raw_coordinates(self):
        flow = CalibrationFlow(setup_only=True)
        flow.accept_starting(np.zeros((600, 900, 3), dtype=np.uint8), None)
        app = TouchApp.__new__(TouchApp)
        app.calibration_flow = flow
        app.preview_geometry = (0, 0, 1)
        app.busy = False
        app.say = Mock()
        app.render = Mock()
        app.suggest_starting_orientation = Mock()
        displayed = [[80, 60], [810, 50], [790, 540], [90, 550]]
        for x, y in displayed:
            app.tap_calibration_corner(SimpleNamespace(x=x, y=y))
        self.assertEqual(flow.stage, "ready")
        self.assertEqual(flow.starting_corners,
                         [[109, 59], [809, 49], [819, 539], [89, 549]])
        app.suggest_starting_orientation.assert_called_once()

    def test_setup_preview_places_a1_and_h1_along_bottom(self):
        flow = CalibrationFlow(setup_only=True)
        corners = [[0, 0], [799, 0], [799, 799], [0, 799]]
        flow.accept_starting(np.zeros((800, 800, 3), dtype=np.uint8), corners)
        app = TouchApp.__new__(TouchApp)
        app.calibration_flow = flow
        app.photo_adjustments = PhotoAdjustments()
        app.board_canvas = Mock()
        app.board_canvas.winfo_width.return_value = 800
        app.board_canvas.winfo_height.return_value = 800
        with patch("pi_touch.tk.PhotoImage"):
            app.draw_photo_preview()
        labels = {call.kwargs.get("text"): call.args[:2]
                  for call in app.board_canvas.create_text.call_args_list}
        self.assertLess(labels["a1"][0], labels["h1"][0])
        self.assertAlmostEqual(labels["a1"][1], labels["h1"][1])
        self.assertGreater(labels["a1"][1], 700)
        self.assertIsNone(app.preview_geometry)

    def test_temporal_capture_feedback_does_not_call_destination_empty(self):
        board = chess.Board("4k3/8/8/4p3/3P4/8/8/4K3 b - - 0 1")
        expected = chess.Move.from_uci("e5d4")
        scores = {square: 0.0 for square in chess.SQUARE_NAMES}
        observed = TemporalObservation(("e5", "d4"), scores)
        feedback = temporal_computer_move_feedback(board, expected, observed)
        self.assertIn("Expected: Black pawn e5 → d4", feedback.detail)
        self.assertIn("Camera sees changes on", feedback.detail)
        self.assertNotIn("emptied squares", feedback.detail)

    def test_setup_only_game_starts_with_whites_turn(self):
        calibration = Calibration([[0, 0], [799, 0], [799, 799], [0, 799]],
                                  (0.0, 0.0), (0.0, 0.0), scoring="temporal_v1")
        app = TouchApp.__new__(TouchApp)
        app.args = SimpleNamespace(calibration=Path("/unused"))
        app.level = "beginner"
        app.say = Mock()
        app.handle_result("start", (calibration, np.zeros((800, 800, 3), dtype=np.uint8), Mock()))
        self.assertEqual(app.session.phase, "human")

    def test_verified_temporal_move_updates_reference(self):
        board = chess.Board()
        session = TouchSession("beginner")
        session.phase = "human"
        scores = {square: 0.0 for square in chess.SQUARE_NAMES}
        scores["e2"] = scores["e4"] = 60.0
        observed = TemporalObservation(("e2", "e4"), scores)
        new_reference = np.ones((800, 800, 3), dtype=np.uint8)
        app = TouchApp.__new__(TouchApp)
        app.session = session
        app.scan_corners = [[0, 0], [799, 0], [799, 799], [0, 799]]
        app.last_scan_corners = None
        app.reference = np.zeros_like(new_reference)
        app.scan_corner_source = "saved"
        app.scan_attempts = 1
        app.clear_scan_photo = Mock()
        app.say = Mock()
        app.after_human_move = Mock()
        app.handle_result("scan", (observed, new_reference))
        self.assertIs(app.reference, new_reference)
        self.assertEqual(session.phase, "thinking")

    def test_verified_temporal_computer_move_updates_reference(self):
        session = TouchSession("beginner")
        session.phase = "human"
        human_scores = {square: 0.0 for square in chess.SQUARE_NAMES}
        human_scores["e2"] = human_scores["e4"] = 60.0
        session.scan_human_temporal(TemporalObservation(("e2", "e4"), human_scores))
        session.announce_computer(chess.Move.from_uci("e7e5"))
        scores = {square: 0.0 for square in chess.SQUARE_NAMES}
        scores["e7"] = scores["e5"] = 60.0
        observed = TemporalObservation(("e5", "e7"), scores)
        new_reference = np.ones((800, 800, 3), dtype=np.uint8)
        app = TouchApp.__new__(TouchApp)
        app.session = session
        app.scan_corners = [[0, 0], [799, 0], [799, 799], [0, 799]]
        app.last_scan_corners = None
        app.reference = np.zeros_like(new_reference)
        app.clear_scan_photo = Mock()
        app.say = Mock()
        app.handle_result("scan", (observed, new_reference))
        self.assertIs(app.reference, new_reference)
        self.assertEqual(session.phase, "human")

    def test_computer_mismatch_reaches_large_feedback_after_retries(self):
        session = TouchSession("beginner")
        session.verify_setup(occupied_squares(session.game.board))
        session.scan_human(after(session.game.board, "e2e4"))
        session.announce_computer(chess.Move.from_uci("e7e5"))
        app = TouchApp.__new__(TouchApp)
        app.closed = False
        app.generation = 0
        app.results = queue.SimpleQueue()
        app.root = SimpleNamespace(after=Mock())
        app.session = session
        app.scan_photo = np.zeros((8, 8, 3), dtype=np.uint8)
        app.scan_corners = [[0, 0], [7, 0], [7, 7], [0, 7]]
        app.scan_corner_source = "saved"
        app.scan_attempts = 3
        app.scan_feedback = None
        app.say = Mock()
        app.render = Mock()
        wrong = Observation(after(session.game.board, "e7e6"), (), {})
        app.results.put((0, "scan", wrong, None))
        app.poll_results()
        self.assertEqual(app.scan_feedback.title, "COMPUTER MOVE MISMATCH")
        self.assertIn("Expected: Black pawn e7 → e5", app.scan_feedback.detail)
        self.assertIn("Camera sees: Black pawn e7 → e6", app.scan_feedback.detail)
        self.assertEqual(session.phase, "computer")

    def test_setup_retries_unclear_photo_before_manual_confirmation(self):
        app = TouchApp.__new__(TouchApp)
        app.session = TouchSession("beginner")
        app.scan_corner_source = "saved"
        app.scan_attempts = 1
        app._capture_scan_attempt = Mock()
        app.scan_corners = [[0, 0], [1, 0], [1, 1], [0, 1]]
        observed = unclear(occupied_squares(app.session.game.board), "f7")
        app.handle_result("scan", observed)
        app._capture_scan_attempt.assert_called_once()
        self.assertEqual(app.session.phase, "setup")

    def test_missing_corners_retry_before_manual_prompt(self):
        app = TouchApp.__new__(TouchApp)
        app.scan_attempts = 1
        app._capture_scan_attempt = Mock()
        app.say = Mock()
        photo = np.zeros((8, 8, 3), dtype=np.uint8)
        app.handle_result("scan_photo", (photo, None, "manual"))
        app._capture_scan_attempt.assert_called_once()
        app.say.assert_not_called()
        app.scan_attempts = 3
        app.handle_result("scan_photo", (photo, None, "manual"))
        app.say.assert_called_once()

    def test_unclear_scan_retries_twice_then_reports_result(self):
        app = TouchApp.__new__(TouchApp)
        app.closed = False
        app.generation = 0
        app.results = queue.SimpleQueue()
        app.root = SimpleNamespace(after=Mock())
        app.session = TouchSession("beginner")
        app.session.phase = "human"
        app.scan_photo = np.zeros((8, 8, 3), dtype=np.uint8)
        app.scan_corners = [[0, 0], [7, 0], [7, 7], [0, 7]]
        app.scan_corner_source = "saved"
        app.scan_attempts = 1
        app.scan_feedback = None
        app.say = Mock()
        app.render = Mock()
        app._capture_scan_attempt = Mock(side_effect=lambda: setattr(
            app, "scan_attempts", app.scan_attempts + 1))
        observed = Observation(occupied_squares(app.session.game.board),
                               ("a7", "f7", "g8"), {})
        for expected_attempt in (2, 3):
            app.results.put((0, "scan", observed, None))
            app.poll_results()
            self.assertEqual(app.scan_attempts, expected_attempt)
            self.assertIsNone(app.scan_feedback)
        app.results.put((0, "scan", observed, None))
        app.poll_results()
        self.assertEqual(app._capture_scan_attempt.call_count, 2)
        self.assertEqual(app.scan_feedback.title, "PHOTO UNCLEAR")
        self.assertIn("Tried 3 photos", app.scan_feedback.detail)

    def test_editing_captured_setup_photo_keeps_both_raw_photos(self):
        app = TouchApp.__new__(TouchApp)
        flow = CalibrationFlow()
        flow.stage = "ready"
        flow.empty_photo = np.zeros((8, 8, 3), dtype=np.uint8)
        flow.starting_photo = np.ones((8, 8, 3), dtype=np.uint8)
        app.calibration_flow = flow
        app.photo_adjustments = PhotoAdjustments()
        app.draw_board = Mock()
        app.say = Mock()
        empty, starting = flow.empty_photo, flow.starting_photo
        app.set_photo_adjustment("brightness", 20)
        self.assertIs(flow.empty_photo, empty)
        self.assertIs(flow.starting_photo, starting)
        self.assertEqual(flow.stage, "ready")
        self.assertEqual(app.photo_adjustments.brightness, 20)
        app.draw_board.assert_called_once()

    def test_scan_automatically_uses_last_verified_corners(self):
        photo = np.zeros((1000, 1000, 3), dtype=np.uint8)
        corners = [[100, 100], [900, 100], [900, 900], [100, 900]]
        with tempfile.TemporaryDirectory() as directory:
            app = TouchApp.__new__(TouchApp)
            app.session = object()
            app.busy = False
            app.scan_feedback = None
            app.last_scan_corners = corners
            app.calibration = None
            app.args = SimpleNamespace(calibration=Path(directory), corners=None)
            app.camera = Mock()
            app.say = Mock()
            app.render = Mock()
            app.submit = Mock()
            with patch("pi_touch.read_image", return_value=photo), \
                    patch("pi_touch.board_corners", side_effect=VisionError("No markers")):
                app.scan()
                tag, work = app.submit.call_args.args
                self.assertEqual(tag, "scan_photo")
                scanned, detected, source = work()
            self.assertIs(scanned, photo)
            self.assertEqual(detected, corners)
            self.assertEqual(source, "saved")


if __name__ == "__main__":
    unittest.main()
