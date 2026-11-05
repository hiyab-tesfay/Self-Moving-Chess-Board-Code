"""What the gantry has to carry for a chess move, and where every piece is.

The Layout follows each physical piece: on a square ("e4") or in a docking slot beside the
board ("w:0".."w:11" down White's lane, "b:0".. down Black's; a thirteenth captured piece of
one colour goes to a second lane). For a move the gantry plays:

  * a capture docks the taken piece first, then moves the taker;
  * en passant docks the pawn that was passed, then moves the capturing pawn;
  * castling carries the king, then the rook (the path finder makes room if the rook
    cannot pass the king on this board);
  * a promotion docks the pawn and brings a docked piece of the promoted type to the
    square, or, when there is none, moves the pawn and says so in a note;
  * a new game carries everything that is not on its home square back.

For a move the person plays by hand, the layout just follows: a taken piece leaves the
board (the person keeps it), a promoted piece appears from their hand.

Every job carries "before" and "pieces" so the gantry driver can find paths with the real
piece widths (board_paths.py). Steps also carry a plain straight "path" for a driver that
does not re-plan.
"""
from __future__ import annotations

import chess

FILES = "abcdefgh"
SLOTS_PER_LANE = 12
SLOTS_PER_COLOR = 16


def _xy(name: str) -> list[float]:
    if ":" in name:
        index = int(name[2:])
        lane = index // SLOTS_PER_LANE
        x = -1 - lane if name[0] == "w" else 8 + lane
        return [float(x), float(index % SLOTS_PER_LANE)]
    return [float(FILES.index(name[0])), float(int(name[1]) - 1)]


class Layout:
    """Where every physical piece is, keyed by square or docking slot."""

    def __init__(self, board: chess.Board | None = None) -> None:
        board = board or chess.Board()
        self.pieces: dict[str, str] = {chess.square_name(sq): piece.symbol() for sq, piece in board.piece_map().items()}

    def copy(self) -> "Layout":
        other = Layout.__new__(Layout)
        other.pieces = dict(self.pieces)
        return other

    def on_board(self) -> list[str]:
        return sorted(name for name in self.pieces if ":" not in name)

    def free_slot(self, symbol: str) -> str:
        lane = "w" if symbol.isupper() else "b"
        for index in range(SLOTS_PER_COLOR):
            name = f"{lane}:{index}"
            if name not in self.pieces:
                return name
        raise ValueError(f"no free docking slot for {symbol}")

    def docked(self, symbol: str) -> str | None:
        """A spare piece of this exact type waiting in a lane, lowest slot first."""
        slots = [name for name, piece in self.pieces.items() if ":" in name and piece == symbol]
        return min(slots, key=lambda n: (n[0], int(n[2:]))) if slots else None

    # ------------------------------------------------------------------ carries

    def carry(self, source: str, target: str) -> dict:
        piece = self.pieces.pop(source)
        self.pieces[target] = piece
        return {"op": "carry", "piece": piece, "from": source, "to": target, "path": [_xy(source), _xy(target)]}

    def dock(self, square: str) -> dict:
        return self.carry(square, self.free_slot(self.pieces[square]))

    def plan_computer(self, board: chess.Board, move: chess.Move) -> list[dict]:
        """The carries for the gantry to play `move` on `board`. Updates the layout."""
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {move.uci()}")
        source, target = chess.square_name(move.from_square), chess.square_name(move.to_square)
        steps: list[dict] = []
        if board.is_en_passant(move):
            steps.append(self.dock(target[0] + source[1]))
        elif board.is_capture(move):
            steps.append(self.dock(target))
        if board.is_castling(move):
            rank = source[1]
            rook_from, rook_to = ("h", "f") if board.is_kingside_castling(move) else ("a", "d")
            steps.append(self.carry(source, target))
            steps.append(self.carry(rook_from + rank, rook_to + rank))
        elif move.promotion:
            wanted = chess.Piece(move.promotion, board.turn).symbol()
            spare = self.docked(wanted)
            if spare:
                steps.append(self.dock(source))
                steps.append(self.carry(spare, target))
            else:
                steps.append(self.carry(source, target))
                steps.append({"op": "note", "message": f"The pawn on {target} now counts as a {chess.piece_name(move.promotion)}."})
        else:
            steps.append(self.carry(source, target))
        return steps

    def apply_human(self, board: chess.Board, move: chess.Move) -> None:
        """The person played `move` by hand: pieces they took leave the board, a promotion comes from their hand."""
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {move.uci()}")
        source, target = chess.square_name(move.from_square), chess.square_name(move.to_square)
        if board.is_en_passant(move):
            self.pieces.pop(target[0] + source[1], None)
        elif board.is_capture(move):
            self.pieces.pop(target, None)
        piece = self.pieces.pop(source)
        if move.promotion:
            piece = chess.Piece(move.promotion, board.turn).symbol()
        self.pieces[target] = piece
        if board.is_castling(move):
            rank = source[1]
            rook_from, rook_to = ("h", "f") if board.is_kingside_castling(move) else ("a", "d")
            self.pieces[rook_to + rank] = self.pieces.pop(rook_from + rank)

    def plan_reset(self) -> list[dict]:
        """Carries that put every piece back on its home square, docking what is in the way first."""
        home = Layout().pieces
        steps = [self.dock(name) for name in sorted(self.pieces)
                 if ":" not in name and home.get(name) != self.pieces[name]]
        for square, symbol in sorted(home.items()):
            if square in self.pieces:
                continue
            spare = self.docked(symbol)
            if spare is None:
                steps.append({"op": "note", "message": f"Put a {('white ' if symbol.isupper() else 'black ') + chess.piece_name(chess.PIECE_SYMBOLS.index(symbol.lower()))} on {square}."})
                continue
            steps.append(self.carry(spare, square))
        return steps


def job_for_steps(layout_before: Layout, layout_after: Layout, label: str, steps: list[dict], job_id: int = 0) -> dict:
    """A job in the shape the gantry driver and the website's game server use."""
    return {"id": job_id, "label": label, "before": sorted(layout_before.pieces), "pieces": dict(layout_before.pieces),
            "steps": steps, "expect": layout_after.on_board()}


def job_for_move(layout: Layout, board: chess.Board, move: chess.Move, job_id: int = 0) -> dict:
    """Plan `move` for the gantry; `layout` is updated to the position after it."""
    before = layout.copy()
    steps = layout.plan_computer(board, move)
    return job_for_steps(before, layout, move.uci(), steps, job_id)


def job_for_reset(layout: Layout, job_id: int = 0) -> dict:
    before = layout.copy()
    steps = layout.plan_reset()
    return job_for_steps(before, layout, "reset", steps, job_id)


def describe(job: dict) -> list[str]:
    """What a person would have to do instead, one line per step."""
    lines = []
    for step in job["steps"]:
        if step["op"] != "carry":
            lines.append(step["message"])
            continue
        symbol = step["piece"]
        piece = f"{'white' if symbol.isupper() else 'black'} {chess.piece_name(chess.PIECE_SYMBOLS.index(symbol.lower()))}"
        if ":" in step["to"]:
            lines.append(f"Take the {piece} off {step['from']} (slot {step['to']}).")
        elif ":" in step["from"]:
            lines.append(f"Put the {piece} from slot {step['from']} on {step['to']}.")
        else:
            lines.append(f"Move the {piece} from {step['from']} to {step['to']}.")
    return lines
