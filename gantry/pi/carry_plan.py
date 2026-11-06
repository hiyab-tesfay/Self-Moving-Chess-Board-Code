"""What the gantry has to carry for a chess move, and where every piece is.

The Layout follows each physical piece on the board, by square ("e4"). The gantry never takes
a captured piece off the board: a person does, and the board waits until the camera has seen
the square empty before the gantry plays. So for a move the gantry plays:

  * a capture is a `remove` step for the taken piece, then the carry of the taker;
  * en passant is a `remove` step for the pawn that was passed, then the capturing pawn's carry;
  * castling carries the king, then the rook (the path finder makes room if the rook
    cannot pass the king on this board);
  * a promotion carries the pawn and says in a note that it now counts as the promoted piece,
    to be swapped by hand if there is a spare;
  * a new game carries every piece that is not on its home square back to one, and leaves a
    note for every home square whose piece the person took (the gantry cannot bring it back).

A job's steps are therefore of three kinds:

    {"op": "remove", "piece": "p", "square": "d5"}          a person takes the piece off; wait for the camera
    {"op": "carry", "piece": "P", "from": "e4", "to": "d5", "path": [[4, 3], [3, 4]]}
    {"op": "note", "message": "..."}                        something the person must know or do

A `remove` always comes before the carry of the capturing piece. Whoever runs the job (the
touch-screen app, the website relay) waits for the person and checks the square with the camera
before the carries run; the gantry driver itself treats a `remove` as already done. A removed
piece leaves the layout entirely: the person keeps it.

Dock mode (`Layout(captures="dock")`, used by the gantry console on the bench) lets the gantry
take captured pieces off itself, into the docking lane on the RIGHT of the board ("b:0".."b:11",
beside the h file): the left lane is too tight on this frame. Then:

  * a capture (or the pawn passed en passant) is a carry into the free slot the caller names, or
    the first free one, before the taker's carry; when the lane is full it falls back to `remove`;
  * a promotion whose new piece waits in the lane brings that piece onto the promotion square and
    parks the pawn in the slot it left; without one it is the plain carry and the note.

For a move the person plays by hand, the layout just follows: a taken piece leaves the
board (the person keeps it), a promoted piece appears from their hand.

Every job carries "before" and "pieces" so the gantry driver can find paths with the real
piece widths (board_paths.py). Steps also carry a plain straight "path" for a driver that
does not re-plan. Only dock mode sends a piece to a docking slot; a reset carries docked pieces home.
"""
from __future__ import annotations

import chess

FILES = "abcdefgh"
SLOTS_PER_LANE = 12   # slots down one docking lane; "b:12" would be a second lane
DOCK_LANE = "b"       # captured pieces go to the right of the board, beside the h file


def _xy(name: str) -> list[float]:
    if ":" in name:
        index = int(name[2:])
        lane = index // SLOTS_PER_LANE
        x = -1 - lane if name[0] == "w" else 8 + lane
        return [float(x), float(index % SLOTS_PER_LANE)]
    return [float(FILES.index(name[0])), float(int(name[1]) - 1)]


def piece_words(symbol: str) -> str:
    """"black pawn" for "p"."""
    return f"{'white' if symbol.isupper() else 'black'} {chess.piece_name(chess.PIECE_SYMBOLS.index(symbol.lower()))}"


def removal(board: chess.Board, move: chess.Move) -> tuple[str, str] | None:
    """The piece a person must take off the board before the gantry can play `move`: (square, symbol), or None.

    A capture names the square the taker lands on; en passant names the passed pawn's square.
    """
    if board.is_en_passant(move):
        square = chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square))
    elif board.is_capture(move):
        square = move.to_square
    else:
        return None
    piece = board.piece_at(square)
    return (chess.square_name(square), piece.symbol()) if piece else None


class Layout:
    """Where every physical piece is, keyed by square (and by slot, "b:3", for docked pieces).

    `captures` is "remove" (a person takes captured pieces off: the touch screen and the website)
    or "dock" (the gantry carries them into the right-hand docking lane: the bench console).
    """

    def __init__(self, board: chess.Board | None = None, captures: str = "remove", dock_slots: int = SLOTS_PER_LANE) -> None:
        if captures not in ("remove", "dock"):
            raise ValueError(f"captures must be 'remove' or 'dock', not {captures!r}")
        board = board or chess.Board()
        self.captures, self.dock_slots = captures, dock_slots
        self.pieces: dict[str, str] = {chess.square_name(sq): piece.symbol() for sq, piece in board.piece_map().items()}

    def copy(self) -> "Layout":
        other = Layout.__new__(Layout)
        other.captures, other.dock_slots = self.captures, self.dock_slots
        other.pieces = dict(self.pieces)
        return other

    def free_slots(self) -> list[str]:
        """Empty slots of the docking lane, nearest the bottom first."""
        return [name for name in (f"{DOCK_LANE}:{i}" for i in range(self.dock_slots)) if name not in self.pieces]

    def docked(self, symbol: str) -> str | None:
        """The slot holding a docked piece of this kind ("Q"), if any."""
        for i in range(self.dock_slots):
            name = f"{DOCK_LANE}:{i}"
            if self.pieces.get(name) == symbol:
                return name
        return None

    def on_board(self) -> list[str]:
        return sorted(name for name in self.pieces if ":" not in name)

    # ------------------------------------------------------------------ steps

    def carry(self, source: str, target: str, reason: str | None = None) -> dict:
        if target in self.pieces:
            raise ValueError(f"{target} is not empty")
        piece = self.pieces.pop(source)
        self.pieces[target] = piece
        step = {"op": "carry", "piece": piece, "from": source, "to": target, "path": [_xy(source), _xy(target)]}
        if reason:
            step["reason"] = reason
        return step

    def dock(self, square: str, slot: str | None = None) -> dict:
        """The gantry carries the piece on `square` into a docking slot (`slot`, or the first free one)."""
        free = self.free_slots()
        if not free:
            raise ValueError("the docking lane is full")
        if slot is None:
            slot = free[0]
        elif slot not in free:
            raise ValueError(f"slot {slot} is not a free docking slot")
        return self.carry(square, slot, "capture")

    def remove(self, square: str) -> dict:
        """A person takes the piece on `square` off the board; it leaves the layout (the person keeps it)."""
        return {"op": "remove", "piece": self.pieces.pop(square), "square": square}

    def plan_computer(self, board: chess.Board, move: chess.Move, dock_slot: str | None = None,
                      by_hand: bool = False, spare: bool = True) -> list[dict]:
        """The steps for the gantry to play `move` on `board`. Updates the layout.

        A capture comes first: a `remove` step, or in dock mode a carry into `dock_slot` (or the first
        free slot; a `remove` again when the lane is full, or with `by_hand`, which the caller sets when
        the path finder found no way into the lane). Then the carries of the move itself; a promotion
        in dock mode brings a docked piece of the new kind out, unless `spare` is False (no way for it).
        FALLBACKS lists what a caller tries, in order, when the path finder refuses a plan.
        """
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {move.uci()}")
        source, target = chess.square_name(move.from_square), chess.square_name(move.to_square)
        steps: list[dict] = []
        taken = removal(board, move)
        if taken:
            if self.captures == "dock" and self.free_slots() and not by_hand:
                steps.append(self.dock(taken[0], dock_slot))
            else:
                steps.append(self.remove(taken[0]))
        use_spare, spare = spare, None
        if move.promotion and self.captures == "dock" and use_spare:
            spare = self.docked(chess.Piece(move.promotion, board.turn).symbol())
        if board.is_castling(move):
            rank = source[1]
            rook_from, rook_to = ("h", "f") if board.is_kingside_castling(move) else ("a", "d")
            steps.append(self.carry(source, target))
            steps.append(self.carry(rook_from + rank, rook_to + rank))
        elif spare:
            # The new piece comes out of the lane onto the promotion square; the pawn takes its slot.
            steps.append(self.carry(spare, target, "promotion"))
            steps.append(self.carry(source, spare, "promotion"))
        elif move.promotion:
            steps.append(self.carry(source, target))
            steps.append({"op": "note", "message": f"The pawn on {target} now counts as a {chess.piece_name(move.promotion)}; "
                                                   "swap it by hand if you have a spare piece."})
        else:
            steps.append(self.carry(source, target))
        return steps

    def apply_human(self, board: chess.Board, move: chess.Move) -> None:
        """The person played `move` by hand: pieces they took leave the board, a promotion comes from their hand."""
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {move.uci()}")
        source, target = chess.square_name(move.from_square), chess.square_name(move.to_square)
        taken = removal(board, move)
        if taken:
            self.pieces.pop(taken[0], None)
        piece = self.pieces.pop(source)
        if move.promotion:
            piece = chess.Piece(move.promotion, board.turn).symbol()
        self.pieces[target] = piece
        if board.is_castling(move):
            rank = source[1]
            rook_from, rook_to = ("h", "f") if board.is_kingside_castling(move) else ("a", "d")
            self.pieces[rook_to + rank] = self.pieces.pop(rook_from + rank)

    def plan_reset(self) -> list[dict]:
        """Carries that put every piece back on a home square, and notes for what the gantry cannot do.

        A piece that is not on its home square goes to a free home square of its kind; one that
        sits on another piece's home square moves first, and two pieces on each other's home
        squares go round through an empty square. A home square whose piece is gone from the board
        gets a note ("Put a black pawn on e7."): the person took it and must put it back, which is
        why a reset job still expects the full starting position. An extra piece of a kind whose
        home squares are all taken (a promotion from the person's hand) gets a note to take it off.
        """
        home = Layout().pieces
        at_home = {name for name, symbol in self.pieces.items() if home.get(name) == symbol}
        free: dict[str, list[str]] = {}
        for square, symbol in sorted(home.items()):
            if square not in at_home:
                free.setdefault(symbol, []).append(square)
        goals: dict[str, str] = {}
        extras: list[str] = []
        for name in sorted(n for n in self.pieces if n not in at_home):
            symbol = self.pieces[name]
            if free.get(symbol):
                goals[name] = free[symbol].pop(0)
            else:
                extras.append(name)
        steps: list[dict] = []
        parked: list[str] = []
        for name in extras:          # out of the way first, if it sits where another piece must go
            if name in goals.values():
                spare = self._spare_square(set(goals.values()))
                steps.append(self.carry(name, spare))
                name = spare
            parked.append(name)
        while goals:
            ready = sorted((name for name in goals if goals[name] not in self.pieces), key=lambda n: _reset_order(n, goals[n]))
            if ready:
                steps.append(self.carry(ready[0], goals.pop(ready[0])))
                continue
            # Every goal is under another stray: step one of those aside, onto a square nobody needs.
            name = sorted(n for n in goals if n in goals.values())[0]
            spare = self._spare_square(set(goals.values()))
            steps.append(self.carry(name, spare))
            goals[spare] = goals.pop(name)
        for name in parked:
            steps.append({"op": "note", "message": f"Take the {piece_words(self.pieces.pop(name))} off {name}."})
        for square, symbol in sorted(home.items()):
            if square not in self.pieces:
                steps.append({"op": "note", "message": f"Put a {piece_words(symbol)} on {square}."})
        return steps

    def _spare_square(self, reserved: set[str]) -> str:
        """An empty square that is not a reset goal, the middle of the board first."""
        middle = [f"{f}{r}" for r in (4, 5, 3, 6) for f in FILES]
        for square in middle + [f"{f}{r}" for r in (1, 2, 7, 8) for f in FILES]:
            if square not in self.pieces and square not in reserved:
                return square
        raise ValueError("no empty square to step aside to")


# What to try when the path finder finds no way for a plan, in order: as planned; the promotion without the docked
# spare (the pawn goes to the square, a note asks for the swap); and the captured piece taken off by the person too.
FALLBACKS = ({}, {"spare": False}, {"spare": False, "by_hand": True})


def _reset_order(source: str, goal: str) -> tuple:
    """Which stray goes home first. The back ranks before the pawns: a queen or king (27 mm) cannot pass between two
    pawns already home (it needs 26 mm of lane; there are 25), so it must be home before they are. And a stray standing
    deep in the home ranks before one in the middle: the home ranks fill round it and wall it in (the path simulation
    found a queen left on c8 that could no longer reach d1)."""
    depth = {"1": 0, "8": 0, "2": 1, "7": 1}.get(source[1], 2) if ":" not in source else 3
    return (goal[1] in "27", depth, source)


def job_for_steps(layout_before: Layout, layout_after: Layout, label: str, steps: list[dict], job_id: int = 0) -> dict:
    """A job in the shape the gantry driver and the website's game server use."""
    return {"id": job_id, "label": label, "before": sorted(layout_before.pieces), "pieces": dict(layout_before.pieces),
            "steps": steps, "expect": layout_after.on_board()}


def job_for_move(layout: Layout, board: chess.Board, move: chess.Move, job_id: int = 0, dock_slot: str | None = None,
                 by_hand: bool = False, spare: bool = True) -> dict:
    """Plan `move` for the gantry; `layout` is updated to the position after it."""
    before = layout.copy()
    steps = layout.plan_computer(board, move, dock_slot, by_hand, spare)
    return job_for_steps(before, layout, move.uci(), steps, job_id)


def job_for_reset(layout: Layout, job_id: int = 0) -> dict:
    """Everything home. The job expects the full starting position: the pieces the person took are theirs to put back."""
    before = layout.copy()
    steps = layout.plan_reset()
    job = job_for_steps(before, layout, "reset", steps, job_id)
    job["any_order"] = True      # the driver may carry a blocked piece later in the reset instead of failing
    job["expect"] = Layout().on_board()
    return job


def describe(job: dict) -> list[str]:
    """What a person would have to do instead, one line per step."""
    lines = []
    for step in job["steps"]:
        if step["op"] == "remove":
            lines.append(f"Take the {piece_words(step['piece'])} off {step['square']}, then press Check.")
        elif step["op"] == "carry" and ":" in step["to"]:
            lines.append(f"Take the {piece_words(step['piece'])} off {step['from']} to docking slot {step['to']}.")
        elif step["op"] == "carry" and ":" in step["from"]:
            lines.append(f"Bring the {piece_words(step['piece'])} from docking slot {step['from']} to {step['to']}.")
        elif step["op"] == "carry":
            lines.append(f"Move the {piece_words(step['piece'])} from {step['from']} to {step['to']}.")
        else:
            lines.append(step["message"])
    return lines
