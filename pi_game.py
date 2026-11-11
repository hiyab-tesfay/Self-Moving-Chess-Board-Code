"""Play a physical chess game on a Raspberry Pi using camera verification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4

import chess

from board_vision import (VisionError, calibrate, load_calibration, observe,
                          read_image, save_calibration)
from chess_game import ChessGame, DIFFICULTIES, StockfishPlayer
from move_observation import match_legal_moves, occupied_squares
from pi_camera import Camera, CameraError


class GameError(RuntimeError):
    """A physical observation could not be safely accepted."""


def observed_moves(board: chess.Board, observed: frozenset[str]) -> tuple[chess.Move, ...]:
    """Only return legal moves whose resulting occupancy exactly matches the photo."""
    match = match_legal_moves(board, observed)
    return match.candidates if match.status in ("matched", "ambiguous") else ()


def verifies_move(board: chess.Board, move: chess.Move, observed: frozenset[str]) -> bool:
    if move not in board.legal_moves:
        raise ValueError(f"Illegal move: {move.uci()}")
    after = board.copy(stack=False)
    after.push(move)
    return occupied_squares(after) == observed


def capture_observation(camera: Camera, directory: Path, calibration, reference,
                        corners: list[list[float]] | None = None):
    path = directory / f"capture_{uuid4().hex}.jpg"
    camera.capture(path)
    observation, _ = observe(reference, read_image(path), calibration, corners)
    print(f"Photo: {path}")
    if observation.uncertain:
        raise GameError("Uncertain squares: " + ", ".join(observation.uncertain) +
                        ". Retake the photo or recalibrate.")
    return observation.occupied


def _choice(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def _corners(values: list[float] | None) -> list[list[float]] | None:
    return [values[index:index + 2] for index in range(0, 8, 2)] if values else None


def _position(fen: str | None, moves: list[str]) -> chess.Board:
    try:
        board = chess.Board(fen) if fen else chess.Board()
        if not board.is_valid():
            raise ValueError("The FEN does not describe a valid chess position.")
        for move in moves:
            board.push_uci(move)
        return board
    except ValueError as exc:
        raise GameError(f"Invalid FEN or move history: {exc}") from exc


def _print_grid(occupied: frozenset[str], uncertain: tuple[str, ...] = ()) -> None:
    print("    a b c d e f g h")
    for rank in range(8, 0, -1):
        print(f" {rank}  " + " ".join("?" if f"{file}{rank}" in uncertain else
                                     "X" if f"{file}{rank}" in occupied else "."
                                     for file in "abcdefgh"))
    print("X = occupied, . = empty, ? = uncertain")


def _setup(args: argparse.Namespace, camera: Camera) -> int:
    args.calibration.mkdir(parents=True, exist_ok=True)
    if _choice("Remove every piece, then press Enter to photograph the empty board (q to quit): ") == "q":
        return 0
    empty_path = args.calibration / "empty.jpg"
    camera.capture(empty_path)
    print(f"Saved {empty_path}")
    if _choice("Set up all 32 pieces in the standard starting position, then press Enter (q to quit): ") == "q":
        return 0
    starting_path = args.calibration / "starting.jpg"
    camera.capture(starting_path)
    print(f"Saved {starting_path}")
    empty_corners = _corners(args.empty_corners or args.corners)
    starting_corners = _corners(args.starting_corners) if args.starting_corners else None
    calibration, reference, _ = calibrate(read_image(empty_path), read_image(starting_path),
                                          empty_corners, args.rotate,
                                          starting_corners=starting_corners)
    save_calibration(args.calibration, calibration, reference)
    print(f"Calibration saved in {args.calibration}. Keep the camera and lighting fixed.")
    return 0


def _check(args: argparse.Namespace, camera: Camera) -> int:
    calibration, reference = load_calibration(args.calibration)
    path = args.calibration / "captures" / f"capture_{uuid4().hex}.jpg"
    camera.capture(path)
    observation, _ = observe(reference, read_image(path), calibration, _corners(args.corners))
    print(f"Photo: {path}")
    _print_grid(observation.occupied, observation.uncertain)
    if observation.uncertain:
        print("Uncertain squares: " + ", ".join(observation.uncertain) +
              ". Retake the photo or recalibrate.")
        return 2
    board = _position(args.fen, args.moves)
    if args.command == "check":
        expected = occupied_squares(board)
        missing = expected - observation.occupied
        extra = observation.occupied - expected
        if not missing and not extra:
            print("Verified: board occupancy matches the expected position.")
            return 0
        print("Expected occupied but observed empty: " + (", ".join(sorted(missing)) or "none"))
        print("Expected empty but observed occupied: " + (", ".join(sorted(extra)) or "none"))
        return 2
    match = match_legal_moves(board, observation.occupied)
    if match.status in ("matched", "ambiguous"):
        print("Matching legal moves:")
        for move in match.candidates:
            print(f"  {move.uci()} ({board.san(move)})")
        return 0 if match.status == "matched" else 2
    print("No move detected." if match.status == "unchanged" else
          "No legal move matches the observed board occupancy.")
    return 2


def _play(args: argparse.Namespace, camera: Camera) -> int:
    calibration, reference = load_calibration(args.calibration)
    game = ChessGame()
    game.start(args.level)
    captures = args.calibration / "captures"
    player = StockfishPlayer()
    try:
        print("Place pieces in the standard starting position. You play White.")
        while True:
            if _choice("Press Enter to verify the starting board (q to quit): ") == "q":
                return 0
            try:
                occupied = capture_observation(camera, captures, calibration, reference,
                                               _corners(args.corners))
                if occupied != occupied_squares(game.board):
                    raise GameError("Board occupancy does not match the starting position.")
                break
            except GameError as exc:
                print(exc)

        while not game.result_text():
            print(f"White to move. {game.board.fullmove_number}. Move one white piece on the board.")
            while True:
                if _choice("After your move, press Enter to photograph the board (q to quit): ") == "q":
                    return 0
                try:
                    occupied = capture_observation(camera, captures, calibration, reference,
                                                   _corners(args.corners))
                    candidates = observed_moves(game.board, occupied)
                    if not candidates:
                        raise GameError("No legal move matches the photo. Check the board and retake it.")
                    if len(candidates) == 1:
                        move = candidates[0]
                    else:
                        print("The camera cannot distinguish these moves:")
                        for candidate in candidates:
                            print(f"  {candidate.uci()} ({game.board.san(candidate)})")
                        chosen = _choice("Enter the UCI move you made, or Enter to retake: ")
                        move = next((candidate for candidate in candidates if candidate.uci() == chosen), None)
                        if move is None:
                            raise GameError("No move selected. Retake the photo.")
                    print(f"You played {game.board.san(move)} ({move.uci()}).")
                    game.play_human(chess.square_name(move.from_square),
                                    chess.square_name(move.to_square),
                                    chess.piece_symbol(move.promotion) if move.promotion else None)
                    break
                except GameError as exc:
                    print(exc)
            if game.result_text():
                break

            move = player.choose_move(game.board.copy(stack=True), game.level)
            print(f"Stockfish plays {game.board.san(move)} ({move.uci()}). Move that piece on the board.")
            if game.board.is_capture(move):
                print("Remove the captured piece. For en passant, remove the pawn from its original square.")
            if game.board.is_castling(move):
                print("Also move the rook to its castled square.")
            if move.promotion:
                print(f"Replace the pawn with a {chess.piece_name(move.promotion)}.")
            while True:
                if _choice("After Stockfish's move, press Enter to verify the board (q to quit): ") == "q":
                    return 0
                try:
                    occupied = capture_observation(camera, captures, calibration, reference,
                                                   _corners(args.corners))
                    if not verifies_move(game.board, move, occupied):
                        raise GameError("Board occupancy does not match Stockfish's move. Correct it and retake.")
                    game.play_computer(move)
                    break
                except GameError as exc:
                    print(exc)
            print(f"Verified. FEN: {game.board.fen()}")
        print(game.result_text())
        return 0
    finally:
        player.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "check", "scan", "play"))
    parser.add_argument("--calibration", type=Path, default=Path("calibration"))
    parser.add_argument("--camera", choices=("pi", "usb"), default="pi")
    parser.add_argument("--device", type=int, default=0, help="Camera index (default: 0)")
    parser.add_argument("--level", choices=tuple(DIFFICULTIES), default="beginner")
    parser.add_argument("--fen", help="Check/scan: known position, default standard setup")
    parser.add_argument("--moves", nargs="*", default=[], help="Check/scan: UCI move history after --fen")
    parser.add_argument("--corners", nargs=8, type=float, metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"),
                        help="Board corners for setup or each new photo; omit when using markers or saved corners")
    parser.add_argument("--empty-corners", nargs=8, type=float, metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"),
                        help="Setup: empty-photo corners; use with --starting-corners")
    parser.add_argument("--starting-corners", nargs=8, type=float, metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"),
                        help="Setup: starting-photo corners; use with --empty-corners")
    parser.add_argument("--rotate", choices=("none", "cw", "ccw", "180"), default="none",
                        help="Setup: rotate aligned image so a8 is at top left")
    args = parser.parse_args(argv)
    if args.command == "setup" and (bool(args.empty_corners) != bool(args.starting_corners) or
                                     (args.corners and args.empty_corners)):
        parser.error("Use --corners alone, or both --empty-corners and --starting-corners.")
    camera = Camera(args.camera, args.device)
    try:
        if args.command == "setup":
            return _setup(args, camera)
        if args.command in ("check", "scan"):
            return _check(args, camera)
        return _play(args, camera)
    except (CameraError, VisionError, FileNotFoundError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
