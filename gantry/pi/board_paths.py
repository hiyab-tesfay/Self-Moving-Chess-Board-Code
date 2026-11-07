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

Options (all off by default, so old configs plan exactly as before; gantry_config.json turns
them on, and gantry/sim/path_sim.py measures what each one does over many games):

  * `slot_columns`: which docking lanes may hold pieces (board_layout.usable_lanes: the ones
    asked for and inside the magnet's reach);
  * `reach_mm`: the box the magnet can reach (x_min, y_min, x_max, y_max). Nodes outside it are
    never used: on the bench the magnet sits 60 mm right of the Y gantry's left edge, so a lane
    drawn too far left is out of reach (the driver passes the whole travel by default);
  * `smooth`: after A*, replace runs of grid points by straight shortcuts that keep the open
    clearance, so a carry has fewer corners (every corner is a stop on the real gantry);
  * `corner_lag_mm`: a carried piece trails the magnet by a few millimetres (the MuJoCo model says
    5-6), so at a corner it cuts inside. With this set, a corner is only used if the chord the
    piece cuts also touches nothing. (On this grid the simulation found it changes nothing: when
    both legs of a corner are clear, so is the short chord. The lag matters at the end of a carry,
    which the console's overshoot handles.);
  * `turn_penalty_mm`: what one corner costs in the search, in millimetres of path.

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
    slot_columns: tuple | None = None             # docking lanes in use (None: every one in park_columns_mm)
    reach_mm: tuple | None = None                 # (x_min, y_min, x_max, y_max) of the magnet; None: no limit
    smooth: bool = False                          # straight shortcuts over the grid path
    corner_lag_mm: float = 0.0                    # how far the piece trails the magnet, checked at corners
    turn_penalty_mm: float = 2.0                  # the search's price for a change of direction
    square_y_mm: float | None = None              # the square size along Y when measured apart from X (square_mm)

    @property
    def sy(self) -> float:
        return self.square_y_mm or self.square_mm

    def reachable(self, x_mm: float, y_mm: float) -> bool:
        if self.reach_mm is None:
            return True
        x0, y0, x1, y1 = self.reach_mm
        return x0 - 1e-6 <= x_mm <= x1 + 1e-6 and y0 - 1e-6 <= y_mm <= y1 + 1e-6

    def slot_column_in_use(self, column: int) -> bool:
        return column in self.park_columns_mm and (self.slot_columns is None or column in self.slot_columns)

    def board_mm(self, x: float, y: float) -> tuple[float, float]:
        """A board point in mm. The lanes beside the board (x between -1 and 0, or 7 and 8) run between
        the edge file and the docking lane, the same rule the firmware (place_mm) and the driver use."""
        return self.x_mm(x), self.a1_mm[1] + y * self.sy

    def x_mm(self, x: float) -> float:
        a1 = self.a1_mm[0]
        if x < 0 and -1 in self.park_columns_mm:
            return self.park_columns_mm[-1] + (x + 1) * (a1 - self.park_columns_mm[-1])
        if x > 7 and 8 in self.park_columns_mm:
            h = a1 + 7 * self.square_mm
            return h + (x - 7) * (self.park_columns_mm[8] - h)
        return a1 + x * self.square_mm

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
        # The grid depends only on the geometry, never on the pieces: build it once and share it (the
        # search for room to move a blocker builds hundreds of Boards).
        cache = geometry.__dict__.setdefault("_grids", {})
        key = (self.slots_per_lane, geometry.reach_mm, geometry.slot_columns, geometry.square_mm, geometry.square_y_mm, tuple(geometry.a1_mm),
               tuple(sorted(geometry.park_columns_mm.items())), tuple(geometry.park_rows_mm))
        if key not in cache:
            self.nodes = self._build_nodes()
            self.edges = self._build_edges()
            cache[key] = (self.nodes, self.edges, {n: geometry.node_mm(n) for n in self.nodes})
        self.nodes, self.edges, self.mm = cache[key]

    def size_of(self, name: str) -> float:
        return self.g.size(self.pieces.get(name))

    # ------------------------------------------------------------------ names and nodes

    def node(self, name: str):
        if ":" in name:
            color, index = name[0], int(name[2:])
            lane = index // self.slots_per_lane
            column = -1 - lane if color == "w" else 8 + lane
            if not self.g.slot_column_in_use(column):
                raise NoPath(f"no parking lane in use for {name}")
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
                if self.g.reachable(*self.g.board_mm(x, y)):
                    nodes.append((x, y))
        for column in self.g.park_columns_mm:
            if not self.g.slot_column_in_use(column):
                continue
            for row in range(min(self.slots_per_lane, len(self.g.park_rows_mm))):
                slot = Slot(column, row)
                if self.g.reachable(*self.g.node_mm(slot)):
                    nodes.append(slot)
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
            y = round((y_mm - self.g.a1_mm[1]) / self.g.sy * 2) / 2
            y = max(-0.5, min(7.5, y))
            lane = (lane_x, y)
            if lane in edges:
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

    def obstacles(self, ignore: set[str]) -> list[tuple[float, float, float]]:
        """(x mm, y mm, base width) of every piece but those in `ignore`."""
        out = []
        for name in self.occupied:
            if name in ignore:
                continue
            try:
                x, y = self.g.node_mm(self.node(name))
            except NoPath:
                continue
            out.append((x, y, self.size_of(name)))
        return out

    def clear(self, a, b, ignore: set[str], carried_mm: float | None = None, obstacles=None) -> bool:
        """Can the carried piece slide from node a to node b without touching anything but `ignore`?"""
        air = self.g.lane_clearance_mm if self._lane_run(a, b) else self.g.clearance_mm
        return self.clear_mm(self.mm.get(a) or self.g.node_mm(a), self.mm.get(b) or self.g.node_mm(b), ignore,
                             carried_mm, air, obstacles)

    def clear_mm(self, pa, pb, ignore: set[str], carried_mm: float | None, air: float, obstacles=None) -> bool:
        """The same for any two points in mm, with `air` between the carried piece and every other one.
        `obstacles` is self.obstacles(ignore), passed in by a search that checks many segments."""
        carried = self.g.piece_mm if carried_mm is None else carried_mm
        if obstacles is None:
            obstacles = self.obstacles(ignore)
        ax, ay = pa
        bx, by = pb
        lo_x, hi_x, lo_y, hi_y = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        for px, py, size in obstacles:
            needed = (carried + size) / 2 + air - 1e-9
            if px < lo_x - needed or px > hi_x + needed or py < lo_y - needed or py > hi_y + needed:
                continue
            if _dist_point_segment((px, py), pa, pb) < needed:
                return False
        return True

    # ------------------------------------------------------------------ search

    def corner_ok(self, before, here, there, ignore: set[str], carried_mm: float, obstacles=None) -> bool:
        """With corner_lag_mm set: the chord a trailing piece cuts at the corner `here` touches nothing."""
        lag = self.g.corner_lag_mm
        if lag <= 0:
            return True
        pb, ph, pt = self.g.node_mm(before), self.g.node_mm(here), self.g.node_mm(there)
        d1, d2 = math.dist(pb, ph), math.dist(ph, pt)
        if d1 < 1e-9 or d2 < 1e-9:
            return True
        cross = (ph[0] - pb[0]) * (pt[1] - ph[1]) - (ph[1] - pb[1]) * (pt[0] - ph[0])
        if abs(cross) < 1e-6:
            return True
        k1, k2 = min(lag, d1) / d1, min(lag, d2) / d2
        a = (ph[0] - (ph[0] - pb[0]) * k1, ph[1] - (ph[1] - pb[1]) * k1)
        b = (ph[0] + (pt[0] - ph[0]) * k2, ph[1] + (pt[1] - ph[1]) * k2)
        return self.clear_mm(a, b, ignore, carried_mm, self.g.lane_clearance_mm, obstacles)

    def find_path(self, src: str, dst: str):
        """Nodes from src to dst, or None. The piece on src and the empty dst are not obstacles."""
        start, goal = self.node(src), self.node(dst)
        if start not in self.edges or goal not in self.edges:
            raise NoPath(f"{src if start not in self.edges else dst} is out of the magnet's reach")
        ignore = {src, dst}
        carried = self.size_of(src)
        obstacles = self.obstacles(ignore)
        mm = self.mm
        goal_mm = mm[goal]
        best = {start: 0.0}
        came: dict = {}
        heap = [(0.0, 0, start, None, 0.0)]
        tick = 0
        while heap:
            _, _, node, prev, cost = heapq.heappop(heap)
            if cost > best[node] + 1e-9:
                continue                                   # a better way here was found after this one was queued
            if node == goal:
                path = [node]
                while node in came:
                    node = came[node]
                    path.append(node)
                path.reverse()
                return self.shortcut(path, ignore, carried, obstacles) if self.g.smooth else path
            here = mm[node]
            for other in self.edges.get(node, []):
                if not self.clear(node, other, ignore, carried, obstacles):
                    continue
                there = mm[other]
                step = math.hypot(there[0] - here[0], there[1] - here[1])
                # A price for every change of direction keeps paths simple (each corner is a stop).
                turn = 0.0
                if prev is not None:
                    before = mm[prev]
                    d1 = (here[0] - before[0], here[1] - before[1])
                    d2 = (there[0] - here[0], there[1] - here[1])
                    if abs(d1[0] * d2[1] - d1[1] * d2[0]) > 1e-6:
                        if not self.corner_ok(prev, node, other, ignore, carried, obstacles):
                            continue
                        turn = self.g.turn_penalty_mm
                new = cost + step + turn
                if new < best.get(other, float("inf")) - 1e-9:
                    best[other] = new
                    came[other] = node
                    tick += 1
                    h = math.hypot(goal_mm[0] - there[0], goal_mm[1] - there[1])
                    heapq.heappush(heap, (new + h, tick, other, node, new))
        return None

    def shortcut(self, nodes, ignore: set[str], carried: float, obstacles=None):
        """String-pulling: from each point, jump to the farthest later point that a straight slide reaches with
        the open clearance (never the tight lane margin), corners checked as in the search. The points stay grid
        nodes, so the firmware can be told them exactly."""
        if obstacles is None:
            obstacles = self.obstacles(ignore)
        mm = self.mm
        out = [nodes[0]]
        i = 0
        while i < len(nodes) - 1:
            for j in range(len(nodes) - 1, i, -1):
                if j == i + 1:
                    break
                if not self.clear_mm(mm[nodes[i]], mm[nodes[j]], ignore, carried, self.g.clearance_mm, obstacles):
                    continue
                if len(out) >= 2 and not self.corner_ok(out[-2], nodes[i], nodes[j], ignore, carried, obstacles):
                    continue
                if j + 1 < len(nodes) and not self.corner_ok(nodes[i], nodes[j], nodes[j + 1], ignore, carried, obstacles):
                    continue
                break
            out.append(nodes[j])
            i = j
        return out

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
        for name in (src, dst):
            if self.node(name) not in self.edges:
                raise NoPath(f"{name} is out of the magnet's reach")
        try:
            return [self.carry(src, dst)]
        except NoPath:
            pass
        best: tuple[float, list[Carry]] | None = None
        blockers = sorted(name for name in self.occupied if name not in (src, dst) and ":" not in name)
        direct = math.dist(self.g.node_mm(self.node(src)), self.g.node_mm(self.node(dst)))
        for blocker in blockers:
            trial = Board(self.g, self.occupied - {blocker}, self.slots_per_lane, self.pieces)
            if trial.find_path(src, dst) is None:
                continue
            at = self.g.node_mm(self.node(blocker))
            tiles = sorted((math.dist(at, self.g.node_mm(self.node(t))), t) for t in self.free_board_tiles() if t != dst)
            for away, tile in tiles:
                if best is not None and 2 * away + direct >= best[0]:
                    break                                  # every later tile is farther: none can do better
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

    def best_slot(self, src: str, slots) -> str | None:
        """Of the free slots `slots`, the one the piece on `src` reaches by the shortest clear way (no room made),
        or None when none is reachable directly."""
        best = None
        for slot in slots:
            try:
                nodes = self.find_path(src, slot)
            except NoPath:
                continue
            if nodes is not None:
                cost = self.path_mm(nodes)
                if best is None or cost < best[0]:
                    best = (cost, slot)
        return best[1] if best else None

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
