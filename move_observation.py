"""Match a 64-square occupancy observation to legal chess moves."""

from __future__ import annotations

from dataclasses import dataclass

import chess


def occupied_squares(board: chess.Board) -> frozenset[str]:
    return frozenset(chess.square_name(square) for square in board.piece_map())


@dataclass(frozen=True)
class MoveMatch:
    candidates: tuple[chess.Move, ...]
    expected: frozenset[str]
    observed: frozenset[str]

    @property
    def status(self) -> str:
        if self.observed == self.expected:
            return "unchanged"
        if not self.candidates:
            return "no_match"
        if len(self.candidates) > 1:
            return "ambiguous"
        return "matched"


def match_legal_moves(board: chess.Board, observed: set[str] | frozenset[str]) -> MoveMatch:
    """Compare the photo with every resulting legal position without changing board."""
    invalid = set(observed) - set(chess.SQUARE_NAMES)
    if invalid:
        raise ValueError(f"Invalid square names: {', '.join(sorted(invalid))}")
    observed = frozenset(observed)
    expected = occupied_squares(board)
    if observed == expected:
        return MoveMatch((), expected, observed)

    matches = []
    for move in board.legal_moves:
        after = board.copy(stack=False)
        after.push(move)
        if occupied_squares(after) == observed:
            matches.append(move)
    return MoveMatch(tuple(matches), expected, observed)


@dataclass(frozen=True)
class ScoredMoveMatch:
    status: str
    candidates: tuple[chess.Move, ...]
    best_cost: float
    unchanged_cost: float
    second_cost: float | None
    conflicts: tuple[tuple[str, float], ...]


def match_scored_moves(
    board: chess.Board,
    scores: dict[str, float],
    thresholds: tuple[float, float],
    *,
    max_cost: float = 25.0,
    max_single_cost: float = 15.0,
    max_conflicts: int = 2,
    min_advantage: float = 10.0,
) -> ScoredMoveMatch:
    """Find a legal occupancy pattern while tolerating a few weak image errors.

    The limits are conservative defaults for the sample phone photos. A high
    unexplained score or a close second candidate never commits a move.
    """
    if set(scores) != set(chess.SQUARE_NAMES):
        raise ValueError("Expected one score for every chess square.")

    def cost(occupied: frozenset[str]) -> tuple[float, tuple[tuple[str, float], ...]]:
        conflicts = []
        for square, value in scores.items():
            group = (ord(square[0]) - ord("a") + int(square[1]) - 1) % 2
            threshold = thresholds[group]
            penalty = max(0.0, threshold - value) if square in occupied else max(0.0, value - threshold)
            if penalty:
                conflicts.append((square, penalty))
        conflicts.sort(key=lambda item: -item[1])
        return sum(value for _, value in conflicts), tuple(conflicts)

    unchanged_cost, _ = cost(occupied_squares(board))
    ranked = []
    for move in board.legal_moves:
        after = board.copy(stack=False)
        after.push(move)
        move_cost, conflicts = cost(occupied_squares(after))
        ranked.append((move_cost, move.uci(), move, conflicts))
    ranked.sort()
    if not ranked:
        status = "unchanged" if unchanged_cost <= max_cost else "no_match"
        return ScoredMoveMatch(status, (), unchanged_cost, unchanged_cost, None, ())

    best_cost, _, best_move, conflicts = ranked[0]
    second_cost = ranked[1][0] if len(ranked) > 1 else None
    clean = (best_cost <= max_cost and len(conflicts) <= max_conflicts and
             all(penalty <= max_single_cost for _, penalty in conflicts))
    if unchanged_cost <= max_cost and unchanged_cost - best_cost < min_advantage:
        status, candidates = "unchanged", ()
    elif not clean or unchanged_cost - best_cost < min_advantage:
        status, candidates = "no_match", ()
    elif second_cost is not None and second_cost - best_cost < min_advantage:
        status = "ambiguous"
        candidates = tuple(item[2] for item in ranked if item[0] - best_cost < min_advantage)
    else:
        status, candidates = "matched", (best_move,)
    return ScoredMoveMatch(status, candidates, best_cost, unchanged_cost, second_cost, conflicts)
