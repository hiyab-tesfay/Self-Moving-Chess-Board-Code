"""Test board photos and legal-move inference without a camera or Raspberry Pi."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import chess
import cv2

from board_vision import (
    Observation, VisionError, annotated_board, annotated_changes, calibrate,
    calibrate_from_position, detect_square_changes, load_calibration,
    objectness_scores, observe, read_image, rotate_board, save_calibration,
    warp_board,
)
from move_observation import match_legal_moves, match_scored_moves, occupied_squares


def _corners(values: list[float] | None) -> list[list[float]] | None:
    return [values[index:index + 2] for index in range(0, 8, 2)] if values else None


def _board(args: argparse.Namespace) -> chess.Board:
    try:
        board = chess.Board(args.fen) if args.fen else chess.Board()
        for move in args.moves:
            board.push_uci(move)
        return board
    except ValueError as exc:
        raise VisionError(f"Invalid FEN or move history: {exc}") from exc


def _print_grid(occupied: frozenset[str], uncertain: tuple[str, ...], marker_label: str = "uncertain") -> None:
    print("    a b c d e f g h")
    for rank in range(8, 0, -1):
        print(f" {rank}  " + " ".join("?" if f"{file}{rank}" in uncertain else
                                     "X" if f"{file}{rank}" in occupied else "."
                                     for file in "abcdefgh"))
    print(f"X = occupied, . = empty, ? = {marker_label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    markers = commands.add_parser("markers", help="Create four printable ArUco marker PNGs")
    markers.add_argument("--out", required=True, type=Path)
    setup = commands.add_parser("calibrate", help="Save an empty-board reference and thresholds")
    setup.add_argument("--empty", required=True, type=Path)
    setup.add_argument("--starting", required=True, type=Path)
    setup.add_argument("--out", required=True, type=Path)
    setup.add_argument("--corners", nargs=8, type=float, metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"),
                       help="Board corners in image pixels; omit to use ArUco markers 0,1,2,3")

    for name, help_text in (("inspect", "Show square changes without judging move legality"),
                            ("compare", "Find a legal move from before/after photos")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--before", required=True, type=Path)
        command.add_argument("--after", required=True, type=Path)
        command.add_argument("--before-corners", nargs=8, type=float, metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"))
        command.add_argument("--after-corners", nargs=8, type=float, metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"))
        command.add_argument("--rotate", choices=("none", "cw", "ccw", "180"), default="none")
        command.add_argument("--fen", help="Known position in the before photo (default: starting position)")
        command.add_argument("--moves", nargs="*", default=[], help="Legal UCI moves after --fen")
        command.add_argument("--debug-before", type=Path)
        command.add_argument("--debug-after", type=Path)
        if name == "inspect":
            command.add_argument("--min-change", type=float, default=30.0,
                                 help="Minimum square score change to report as likely (default: 30)")

    for name in ("scan", "verify"):
        command = commands.add_parser(name, help="Find a move" if name == "scan" else "Check board against known position")
        command.add_argument("--calibration", required=True, type=Path)
        command.add_argument("--image", required=True, type=Path)
        command.add_argument("--fen", help="Known position before this photo (default: starting position)")
        command.add_argument("--moves", nargs="*", default=[], help="Legal UCI moves after --fen, e.g. e2e4 e7e5")
        command.add_argument("--debug", type=Path, help="Write an annotated, straightened image")

    args = parser.parse_args(argv)
    try:
        if args.command == "markers":
            args.out.mkdir(parents=True, exist_ok=True)
            dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
            names = ("top_left", "top_right", "bottom_right", "bottom_left")
            for marker_id, name in enumerate(names):
                image = cv2.aruco.generateImageMarker(dictionary, marker_id, 400)
                image = cv2.copyMakeBorder(image, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
                path = args.out / f"{marker_id}_{name}.png"
                if not cv2.imwrite(str(path), image):
                    raise VisionError(f"Could not write marker: {path}")
            print(f"Saved markers 0-3 in {args.out}. Keep them upright and outside the board.")
            print("Align each sticker's outer corner nearest the board with the board's outer corner.")
            return 0
        if args.command == "calibrate":
            calibration, reference, scores = calibrate(read_image(args.empty), read_image(args.starting), _corners(args.corners))
            save_calibration(args.out, calibration, reference)
            print(f"Saved calibration to {args.out}")
            print(f"Thresholds by square color: {calibration.thresholds[0]:.1f}, {calibration.thresholds[1]:.1f}")
            print(f"Starting-photo score range: {min(scores.values()):.1f} to {max(scores.values()):.1f}")
            return 0

        if args.command in ("inspect", "compare"):
            board = _board(args)
            before = rotate_board(warp_board(read_image(args.before), _corners(args.before_corners)), args.rotate)
            after = rotate_board(warp_board(read_image(args.after), _corners(args.after_corners)), args.rotate)
            before_scores = objectness_scores(before)
            thresholds = calibrate_from_position(before_scores, board)
            scores = objectness_scores(after)
            if args.command == "inspect":
                changes = detect_square_changes(before_scores, scores, occupied_squares(board),
                                                thresholds, min_change=args.min_change)
                print("Likely vacated: " + (", ".join(changes.vacated) or "none"))
                print("Likely filled: " + (", ".join(changes.filled) or "none"))
                print("Other threshold crossings: " + (", ".join(changes.uncertain) or "none"))
                for square in changes.vacated + changes.filled:
                    print(f"  {square}: {before_scores[square]:.1f} -> {scores[square]:.1f}")
                if args.debug_before:
                    args.debug_before.parent.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(args.debug_before), before):
                        raise VisionError(f"Could not write debug image: {args.debug_before}")
                if args.debug_after:
                    args.debug_after.parent.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(args.debug_after), annotated_changes(after, changes)):
                        raise VisionError(f"Could not write debug image: {args.debug_after}")
                    print(f"Annotated changes: {args.debug_after}")
                return 0
            classified = frozenset(square for square, score in scores.items()
                                   if score > thresholds[(ord(square[0]) - ord("a") + int(square[1]) - 1) % 2])
            match = match_scored_moves(board, scores, thresholds)
            conflicts = tuple(square for square, _ in match.conflicts)
            _print_grid(classified, conflicts, "conflicts with best legal candidate")
            print(f"Thresholds: {thresholds[0]:.1f}, {thresholds[1]:.1f}; "
                  f"best cost: {match.best_cost:.1f}; unchanged cost: {match.unchanged_cost:.1f}")
            for path, aligned, occupied in ((args.debug_before, before, occupied_squares(board)),
                                            (args.debug_after, after, classified)):
                if path:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(path), annotated_board(aligned, Observation(occupied, conflicts if path == args.debug_after else (), scores))):
                        raise VisionError(f"Could not write debug image: {path}")
                    print(f"Annotated image: {path}")
            if match.status == "matched":
                move = match.candidates[0]
                print(f"Matched move: {move.uci()} ({board.san(move)})")
                board.push(move)
                print("Resulting FEN: " + board.fen())
                return 0
            if match.status == "ambiguous":
                print("Ambiguous occupancy. Choose one manually:")
                for move in match.candidates:
                    print(f"  {move.uci()} ({board.san(move)})")
            elif match.status == "unchanged":
                print("No convincing board change detected.")
            else:
                print("No reliable legal move matches these photos. Game state was not changed.")
                if conflicts:
                    print("Largest conflicting squares: " + ", ".join(f"{square} ({penalty:.1f})" for square, penalty in match.conflicts[:5]))
            return 2

        calibration, reference = load_calibration(args.calibration)
        board = _board(args)
        observation, aligned = observe(reference, read_image(args.image), calibration)
        _print_grid(observation.occupied, observation.uncertain)
        if args.debug:
            args.debug.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(args.debug), annotated_board(aligned, observation)):
                raise VisionError(f"Could not write debug image: {args.debug}")
            print(f"Annotated image: {args.debug}")
        if observation.uncertain:
            print("Uncertain squares: " + ", ".join(observation.uncertain))
            print("No move accepted. Retake the photo or recalibrate.")
            return 2

        if args.command == "verify":
            missing = occupied_squares(board) - observation.occupied
            extra = observation.occupied - occupied_squares(board)
            if not missing and not extra:
                print("Verified: photo occupancy matches the known position.")
                return 0
            print(f"Mismatch. Expected occupied but empty: {', '.join(sorted(missing)) or 'none'}")
            print(f"Expected empty but occupied: {', '.join(sorted(extra)) or 'none'}")
            return 2

        match = match_legal_moves(board, observation.occupied)
        if match.status == "unchanged":
            print("No board change detected.")
            return 2
        if match.status == "no_match":
            print("No legal move matches this occupancy. Game state was not changed.")
            print("Expected occupied but empty: " + (", ".join(sorted(match.expected - match.observed)) or "none"))
            print("Expected empty but occupied: " + (", ".join(sorted(match.observed - match.expected)) or "none"))
            return 2
        if match.status == "ambiguous":
            print("More than one legal move matches. Choose one manually:")
            for move in match.candidates:
                print(f"  {move.uci()} ({board.san(move)})")
            return 2
        move = match.candidates[0]
        print(f"Matched move: {move.uci()} ({board.san(move)})")
        board.push(move)
        print("Resulting FEN: " + board.fen())
        return 0
    except VisionError as exc:
        print(f"Vision error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
