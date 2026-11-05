"""Finding a way for a carried piece that touches nothing, and making room only when there is none.

Every carry is checked in millimetres. Pieces are circles: each type has its own base width
(`piece_sizes_mm`; a piece of unknown type counts as the widest, `piece_mm`). The path runs
over a grid of tile centres, the midpoints between tiles and the crossings between four
tiles (the "lanes"), plus the parking slots beside the board, and A* picks the shortest
route, so a knight goes round through the lanes the way it would on a plotter rather than
over other pieces. Along a lane between two occupied tiles the carried piece keeps
`lane_clearance_mm` of air from each neighbour (1 mm by default: with 50 mm squares a 25 mm
knight between two 23 mm pawns has exactly that); everywhere else it keeps `clearance_mm`
(3 mm). Only when no lane is wide enough, for instance between a king and a queen, does
the search move one blocking piece to a free tile, make the move, and put the blocker back:
three carries instead of one, which the firmware and the camera check already handle.

Names: board squares "a1".."h8"; parking slots "w:0".. and "b:0".. down the lanes beside
the board. Path points come out in the job's square units: a1 is (0, 0), x.5 are lanes,
and a parking slot is (column, row) with column -1 or 8 and the row an index down the lane.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import heapq
import math

FILES = "abcdefgh"
# Base widths of the printed pieces, from the models: pawn 23 mm, knight/bishop/rook 25 mm, queen/king 27 mm.
DEFAULT_PIECE_SIZES_MM = {"p": 23.0, "n": 25.0, "b": 25.0, "r": 25.0, "q": 27.0, "k": 27.0}


@dataclass
class Geometry:
    square_mm: float
    a1_mm: tuple[float, float]
    park_columns_mm: dict[int, float]            # column index (-1, -2, 8, 9) -> x in mm
    park_rows_mm: list[float]                     # y of each slot down a parking lane
    piece_mm: float = 27.0                        # widest piece base; used when a piece's type is unknown
    clearance_mm: float = 3.0                     # air between two pieces, out in the open
    lane_clearance_mm: float = 1.0                # air when sliding along a lane between two tiles
    piece_sizes_mm: dict = field(default_factory=lambda: dict(DEFAULT_PIECE_SIZES_MM))

    def board_mm(self, x: float, y: float) -> tuple[float, float]:
        return self.a1_mm[0] + x * self.square_mm, self.a1_mm[1] + y * self.square_mm

    def node_mm(self, node) -> tuple[float, float]:
        if isinstance(node, Slot):
            return self.park_columns_mm[node.column], self.park_rows_mm[node.row]
        return self.board_mm(*node)

    def size(self, symbol: str | None) -> float:
        """Base width of a piece by its symbol (either case); the widest when unknown."""
        return float(self.piece_sizes_mm.get((symbol or "").lower(), self.piece_mm))

    @property
    def keep_apart_mm(self) -> float:
        return self.piece_mm + self.clearance_mm


@dataclass(frozen=True)
class Slot:
    column: int   # -1 is the lane beside the a file, 8 beside the h file; -2 and 9 are second lanes
    row: int

    @property
    def name(self) -> str:
        color = "w" if self.column < 0 else "b"
        return f"{color}:{self.row}"

    def point(self) -> list[float]:
        return [float(self.column), float(self.row)]


@dataclass
class Carry:
    piece_from: str
    piece_to: str
    path: list[list[float]]
    reason: str = "move"   # "move", "make room", "put back"


class NoPath(Exception):
    pass


def square_node(name: str) -> tuple[float, float]:
    return float(FILES.index(name[0])), float(int(name[1]) - 1)


def node_name(node) -> str:
    if isinstance(node, Slot):
        return node.name
    return f"{FILES[int(node[0])]}{int(node[1]) + 1}"


def _dist_point_segment(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


class Board:
    """The pieces on the board and in the lanes, and the ways between them.

    `pieces` maps a square or slot to the piece on it ("P", "n", ...) so each piece's real
    width is used; a square in `occupied` but not in `pieces` is treated as the widest piece.
    """

    def __init__(self, geometry: Geometry, occupied, slots_per_lane: int | None = None, pieces=None):
        self.g = geometry
        self.occupied: set[str] = set(occupied)
        self.pieces: dict[str, str] = {k: v for k, v in (pieces or {}).items() if k in self.occupied}
        self.slots_per_lane = slots_per_lane or len(geometry.park_rows_mm)
        self.nodes = self._build_nodes()
        self.edges = self._build_edges()

    def size_of(self, name: str) -> float:
        return self.g.size(self.pieces.get(name))

    # ------------------------------------------------------------------ names and nodes

    def node(self, name: str):
        if ":" in name:
            color, index = name[0], int(name[2:])
            lane = index // self.slots_per_lane
            column = -1 - lane if color == "w" else 8 + lane
            if column not in self.g.park_columns_mm:
                raise NoPath(f"no parking lane for {name}")
            return Slot(column, index % self.slots_per_lane)
        return square_node(name)

    def free_board_tiles(self) -> list[str]:
        return [f"{f}{r}" for f in FILES for r in range(1, 9) if f"{f}{r}" not in self.occupied]

    # ------------------------------------------------------------------ the grid

    def _build_nodes(self):
        nodes = []
        steps = [i / 2 for i in range(-1, 16)]           # -0.5 .. 7.5
        for x in steps:
            for y in steps:
                nodes.append((x, y))
        for column in self.g.park_columns_mm:
            for row in range(min(self.slots_per_lane, len(self.g.park_rows_mm))):
                nodes.append(Slot(column, row))
        return nodes

    def _lane_beside(self, column: int) -> float:
        return -0.5 if column < 0 else 7.5

    def _build_edges(self):
        edges: dict = {node: [] for node in self.nodes}
        board = {n for n in self.nodes if not isinstance(n, Slot)}
        for (x, y) in board:
            for dx, dy in ((0.5, 0), (-0.5, 0), (0, 0.5), (0, -0.5)):
                other = (x + dx, y + dy)
                if other in board:
                    edges[(x, y)].append(other)
            # Corners: a tile centre to the four crossings round it, and back.
            centre = x == int(x) and y == int(y)
            crossing = x != int(x) and y != int(y)
            if centre or crossing:
                for dx, dy in ((0.5, 0.5), (0.5, -0.5), (-0.5, 0.5), (-0.5, -0.5)):
                    other = (x + dx, y + dy)
                    if other in board:
                        edges[(x, y)].append(other)
        slots = [n for n in self.nodes if isinstance(n, Slot)]
        for slot in slots:
            # Up and down the lane.
            for other in (Slot(slot.column, slot.row - 1), Slot(slot.column, slot.row + 1)):
                if other in edges:
                    edges[slot].append(other)
                    edges[other].append(slot)
            # In and out of the lane, at the nearest half-step beside it.
            lane_x = self._lane_beside(slot.column)
            if slot.column in (-2, 9):
                # A second lane connects through the first one at the same row.
                inner = Slot(slot.column + (1 if slot.column < 0 else -1), slot.row)
                if inner in edges:
                    edges[slot].append(inner)
                    edges[inner].append(slot)
                continue
            y_mm = self.g.park_rows_mm[slot.row]
            y = round((y_mm - self.g.a1_mm[1]) / self.g.square_mm * 2) / 2
            y = max(-0.5, min(7.5, y))
            lane = (lane_x, y)
            edges[slot].append(lane)
            edges[lane].append(slot)
        return edges

    # ------------------------------------------------------------------ clearance

    @staticmethod
    def _lane_run(a, b) -> bool:
        """A straight slide along a lane line between tiles (x.5 or y.5), where the tight margin applies."""
        if isinstance(a, Slot) or isinstance(b, Slot):
            return False
        return (a[0] == b[0] and a[0] != int(a[0])) or (a[1] == b[1] and a[1] != int(a[1]))

    def clear(self, a, b, ignore: set[str], carried_mm: float | None = None) -> bool:
        """Can the carried piece slide from node a to node b without touching anything but `ignore`?"""
        pa, pb = self.g.node_mm(a), self.g.node_mm(b)
        carried = self.g.piece_mm if carried_mm is None else carried_mm
        air = self.g.lane_clearance_mm if self._lane_run(a, b) else self.g.clearance_mm
        for name in self.occupied:
            if name in ignore:
                continue
            try:
                p = self.g.node_mm(self.node(name))
            except NoPath:
                continue
            needed = (carried + self.size_of(name)) / 2 + air
            if _dist_point_segment(p, pa, pb) < needed - 1e-9:
                return False
        return True

    # ------------------------------------------------------------------ search

    def find_path(self, src: str, dst: str):
        """Nodes from src to dst, or None. The piece on src and the empty dst are not obstacles."""
        start, goal = self.node(src), self.node(dst)
        ignore = {src, dst}
        carried = self.size_of(src)
        goal_mm = self.g.node_mm(goal)
        best = {start: 0.0}
        came: dict = {}
        heap = [(0.0, 0, start, None)]
        tick = 0
        while heap:
            _, _, node, prev = heapq.heappop(heap)
            if node == goal:
                path = [node]
                while node in came:
                    node = came[node]
                    path.append(node)
                return list(reversed(path))
            cost = best[node]
            here = self.g.node_mm(node)
            for other in self.edges.get(node, []):
                if not self.clear(node, other, ignore, carried):
                    continue
                there = self.g.node_mm(other)
                step = math.hypot(there[0] - here[0], there[1] - here[1])
                # A small price for every change of direction keeps paths simple.
                turn = 0.0
                if prev is not None:
                    before = self.g.node_mm(prev)
                    d1 = (here[0] - before[0], here[1] - before[1])
                    d2 = (there[0] - here[0], there[1] - here[1])
                    if abs(d1[0] * d2[1] - d1[1] * d2[0]) > 1e-6:
                        turn = 2.0
                new = cost + step + turn
                if new < best.get(other, float("inf")) - 1e-9:
                    best[other] = new
                    came[other] = node
                    tick += 1
                    h = math.hypot(goal_mm[0] - there[0], goal_mm[1] - there[1])
                    heapq.heappush(heap, (new + h, tick, other, node))
        return None

    @staticmethod
    def simplify(nodes):
        """Drop points that lie on a straight run between their neighbours."""
        out = [nodes[0]]
        for i in range(1, len(nodes) - 1):
            a, b, c = out[-1], nodes[i], nodes[i + 1]
            if isinstance(a, Slot) or isinstance(b, Slot) or isinstance(c, Slot):
                out.append(b)
                continue
            if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
                out.append(b)
        out.append(nodes[-1])
        return out

    def path_points(self, nodes) -> list[list[float]]:
        return [n.point() if isinstance(n, Slot) else [float(n[0]), float(n[1])] for n in self.simplify(nodes)]

    def path_mm(self, nodes) -> float:
        return sum(math.dist(self.g.node_mm(a), self.g.node_mm(b)) for a, b in zip(nodes, nodes[1:]))

    # ------------------------------------------------------------------ carries, with room made when needed

    def carry(self, src: str, dst: str, reason: str = "move") -> Carry:
        nodes = self.find_path(src, dst)
        if nodes is None:
            raise NoPath(f"no way from {src} to {dst} that touches nothing")
        return Carry(src, dst, self.path_points(nodes), reason)

    def apply(self, carry: Carry) -> None:
        self.occupied.discard(carry.piece_from)
        self.occupied.add(carry.piece_to)
        symbol = self.pieces.pop(carry.piece_from, None)
        if symbol is not None:
            self.pieces[carry.piece_to] = symbol

    def plan(self, src: str, dst: str) -> list[Carry]:
        """Carries that get the piece on src to dst: one, or three when a blocker has to make room."""
        if src not in self.occupied:
            raise NoPath(f"nothing on {src}")
        if dst in self.occupied:
            raise NoPath(f"{dst} is not empty")
        try:
            return [self.carry(src, dst)]
        except NoPath:
            pass
        best: tuple[float, list[Carry]] | None = None
        blockers = [name for name in self.occupied if name not in (src, dst) and ":" not in name]
        for blocker in blockers:
            trial = Board(self.g, self.occupied - {blocker}, self.slots_per_lane, self.pieces)
            if trial.find_path(src, dst) is None:
                continue
            for tile in self.free_board_tiles():
                if tile == dst:
                    continue
                try:
                    out = self.carry(blocker, tile, "make room")
                    during = Board(self.g, (self.occupied - {blocker}) | {tile}, self.slots_per_lane,
                                   {**self.pieces, tile: self.pieces.get(blocker, "")})
                    move = during.carry(src, dst)
                    after = Board(self.g, (during.occupied - {src}) | {dst}, self.slots_per_lane,
                                  {**during.pieces, dst: self.pieces.get(src, "")})
                    back = after.carry(tile, blocker, "put back")
                except NoPath:
                    continue
                total = sum(self.path_mm([self._node_of(p) for p in c.path]) for c in (out, move, back))
                if best is None or total < best[0]:
                    best = (total, [out, move, back])
        if best is None:
            raise NoPath(f"no way from {src} to {dst}, even after moving one piece aside")
        return best[1]

    def _node_of(self, point):
        x, y = point
        if x <= -1 or x >= 8:
            return Slot(int(x), int(y))
        return (x, y)


def occupied_at_start() -> set[str]:
    return {f"{f}{r}" for f in FILES for r in (1, 2, 7, 8)}


def pieces_at_start() -> dict[str, str]:
    back = "RNBQKBNR"
    pieces = {}
    for file, symbol in zip(FILES, back):
        pieces[f"{file}1"], pieces[f"{file}2"] = symbol, "P"
        pieces[f"{file}7"], pieces[f"{file}8"] = "p", symbol.lower()
    return pieces
