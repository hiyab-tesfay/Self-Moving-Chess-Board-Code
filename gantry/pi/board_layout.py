"""Where the board is, and what makes the gantry count real millimetres.

gantry_config.json holds the board as Sketch 5 of the Fusion "Chess Board Design" draws it. Sketch 5
is the 470 x 420 mm rectangle the magnet's CENTRE can travel ("Sketch 5 traces the center of the
magnet around the perimeter of where the magnet can move", from the mechanical team), so its
bottom-left corner is where the magnet sits at home (both carriages pushed into the bottom-left
corner, `$HM`), and the sketch's millimetres are what the firmware counts. Inside it: 8 x 8 squares
of 52.5 mm filling the full 420 mm height, centred across (25 mm each side), so a1's centre is at
(51.25, 26.25); a docking column of circles on each side edge, x 0 and x 470.

The magnet sits 60 mm right of the Y gantry's left edge. Sketch 5 already traces the magnet, so that
offset is in the sketch, not added to it. `magnet_home_mm` (0, 0 here) exists for a frame whose
home is somewhere else: machine() subtracts it, and the console's "a1 is here" sets it when the
board check shows every square off by the same amount.

Counting real millimetres: one motor turn is `full_steps_per_turn` x `microsteps` steps, and on
the bench it moves the carriage `mm_per_turn` (41 mm on X and on Y, measured 2026-11-06 with the
drivers on full steps). The firmware has one steps/mm for both axes ($100), taken from X; a Y that
differs a little is absorbed by measuring the board itself: the square size is measured per axis
(a1 to h1 along X, h1 to h8 along Y), in counted millimetres.

A docking lane must be inside the magnet's reach and `min_gap` (piece + clearance, 30 mm) from the
edge file's centres; check() names any that is not, and the planner uses the ones that are
(usable_lanes).
"""
from __future__ import annotations

import math

FULL_STEPS_PER_TURN = 200
DOCK_GAP_MM = 51.25         # sketch 5: docking column to the edge file's centre (0 -> 51.25, 418.75 -> 470)
SKEW_WARN_MM = 1.5          # a1 -> h1 rising (or h1 -> h8 leaning) more than this: the board is turned


def mm_per_turn(config: dict) -> tuple[float, float]:
    value = config.get("mm_per_turn", 41.0)
    if isinstance(value, (int, float)):
        return float(value), float(value)
    return float(value[0]), float(value[1])


def steps_per_turn(config: dict) -> float:
    return float(config.get("full_steps_per_turn", FULL_STEPS_PER_TURN)) * float(config.get("microsteps", 1))


def steps_per_mm(config: dict) -> float:
    """$100 for the firmware: from X's mm per turn (the firmware has one value for both axes)."""
    return steps_per_turn(config) / mm_per_turn(config)[0]


def magnet_home(config: dict) -> tuple[float, float]:
    """Where the magnet's centre sits on the sketch when the gantry is home."""
    value = config.get("magnet_home_mm", [0.0, 0.0])
    return float(value[0]), float(value[1])


def machine(config: dict) -> dict:
    """The config in the machine's millimetres: every sketch position less magnet_home_mm, and the travel from
    home to the sketch's far corner (travel_mm is that corner on the sketch). Idempotent: a config already in
    machine millimetres (no magnet_home_mm, or 0,0) comes back as it is."""
    hx, hy = magnet_home(config)
    if hx == 0 and hy == 0:
        return config
    out = dict(config)
    out["magnet_home_mm"] = [0.0, 0.0]
    out["a1_center_mm"] = [float(config["a1_center_mm"][0]) - hx, float(config["a1_center_mm"][1]) - hy]
    park = config.get("park_columns_mm") or {}
    out["park_columns_mm"] = {side: [float(x) - hx for x in xs] for side, xs in park.items()}
    if config.get("park_rows_mm"):
        out["park_rows_mm"] = [float(y) - hy for y in config["park_rows_mm"]]
    out["travel_mm"] = [float(config["travel_mm"][0]) - hx, float(config["travel_mm"][1]) - hy]
    if config.get("reach_mm"):
        x0, y0, x1, y1 = (float(v) for v in config["reach_mm"])
        out["reach_mm"] = [x0 - hx, y0 - hy, x1 - hx, y1 - hy]
    return out


def to_sketch(config: dict, x: float, y: float) -> tuple[float, float]:
    hx, hy = magnet_home(config)
    return x + hx, y + hy


def square_xy(config: dict) -> tuple[float, float]:
    value = config["square_mm"]
    if isinstance(value, (int, float)):
        return float(value), float(value)
    return float(value[0]), float(value[1])


def travel(config: dict) -> tuple[float, float]:
    """How far the magnet travels from home, machine mm (the firmware's $130 / $131)."""
    t = machine(config)["travel_mm"]
    return float(t[0]), float(t[1])


def reach(config: dict) -> tuple[float, float, float, float]:
    """The box the magnet's centre can reach, machine mm: the configured reach_mm, else the whole travel."""
    m = machine(config)
    r = m.get("reach_mm")
    if r:
        return tuple(float(v) for v in r)
    tx, ty = travel(config)
    return 0.0, 0.0, tx, ty


def min_gap(config: dict) -> float:
    """The closest a docking lane may sit to an edge file: two of the widest pieces side by side with air between."""
    widest = max([float(config.get("piece_mm", 27.0))] + [float(v) for v in (config.get("piece_sizes_mm") or {}).values()])
    return widest + float(config.get("clearance_mm", 3.0))


def motion_lines(config: dict) -> list[str]:
    """What the board must be told before it moves: steps/mm ($100) and the travel ($130, $131). $100 to the value
    the board already has changes nothing; a new one forgets the position (home again)."""
    tx, ty = travel(config)
    return [f"$100={steps_per_mm(config):.5f}", f"$130={tx:g}", f"$131={ty:g}"]


def servo_line(config: dict) -> str | None:
    """$S=up,down,settle from config "servo_deg" ({"up": deg, "down": deg}) and "servo_settle_ms" (default 3000),
    sent on every connect so the arm's angles survive an ESP32 restart (the firmware forgets $S and goes back to
    config.h's). None when the config has no servo_deg."""
    deg = config.get("servo_deg")
    if not deg:
        return None
    up, down = int(round(float(deg["up"]))), int(round(float(deg["down"])))
    settle = int(round(float(config.get("servo_settle_ms", 3000))))
    for v in (up, down):
        if not -20 <= v <= 200:   # firmware 0.8.6+: real degrees, 0-180 = 500-2500 us, 90 = centre: same scale past both ends (0 = 1100 us, 180 = 1900 us)
            raise ValueError(f"servo_deg angles are -20..200, not {v}")
    return f"$S={up},{down},{max(0, min(settle, 3000))}"


def lanes(config: dict) -> dict[str, float]:
    """The x of each configured docking lane's first column, machine mm: {"w": 0.0, "b": 470.0}."""
    park = machine(config).get("park_columns_mm") or {}
    return {side: float(park[side][0]) for side in ("w", "b") if park.get(side)}


def slot_count(config: dict) -> int:
    """Docking slots per lane: one per row in park_rows_mm (9 on Sketch 5: every other circle)."""
    return len(config.get("park_rows_mm") or []) or 12


def usable_lanes(config: dict) -> list[str]:
    """The docking lanes that may hold pieces: asked for in docking_lanes, configured, and inside the magnet's reach."""
    wanted = config.get("docking_lanes")
    x0, _, x1, _ = reach(config)
    return [side for side, x in lanes(config).items()
            if (wanted is None or side in wanted) and x0 - 1e-6 <= x <= x1 + 1e-6]


def check(config: dict) -> list[str]:
    """Plain-words problems with the layout, or [] when the whole board and both lanes are inside the reach.
    Positions in the messages are the sketch's millimetres."""
    problems = []
    sx, sy = square_xy(config)
    hx0, hy0 = magnet_home(config)
    m = machine(config)
    a1x, a1y = (float(v) for v in m["a1_center_mm"])
    hx, top = a1x + 7 * sx, a1y + 7 * sy
    x0, y0, x1, y1 = reach(config)
    if a1x < x0 - 0.5 or a1y < y0 - 0.5 or hx > x1 + 0.5 or top > y1 + 0.5:
        problems.append(f"The board (a1 at {a1x + hx0:.1f},{a1y + hy0:.1f}, h8 at {hx + hx0:.1f},{top + hy0:.1f} on the sketch) is not "
                        f"inside the magnet's reach ({x0 + hx0:.0f}..{x1 + hx0:.0f} x {y0 + hy0:.0f}..{y1 + hy0:.0f}).")
    gap = min_gap(config)
    wanted = config.get("docking_lanes")
    configured = lanes(config)
    for side, name in (("w", "left"), ("b", "right")):
        if wanted is not None and side not in wanted:
            continue
        if side not in configured:
            problems.append(f"No {name} docking lane is configured (park_columns_mm.{side}).")
            continue
        x = configured[side]
        edge = a1x if side == "w" else hx
        if x < x0 - 1e-6 or x > x1 + 1e-6:
            if side == "w":
                problems.append(f"The left docking lane (x {x + hx0:g} on the sketch) is {x0 - x:.1f} mm left of where the magnet can "
                                f"go: the magnet is {hx0:g} mm right of the Y gantry's left edge, so x {x0 + hx0:g} is as far left as "
                                f"it reaches. Captured pieces all go to the right lane.")
            else:
                problems.append(f"The right docking lane (x {x + hx0:g} on the sketch) is {x - x1:.1f} mm past the end of the travel. "
                                f"Captured pieces all go to the left lane.")
        elif abs(x - edge) < gap - 1e-6:
            problems.append(f"The {name} docking lane is only {abs(x - edge):.1f} mm from the {'a' if side == 'w' else 'h'}-file "
                            f"centres; docked pieces would touch the board's pieces (needs {gap:.0f} mm).")
    rows = [float(v) for v in m.get("park_rows_mm") or []]
    if rows and (min(rows) < y0 - 1e-6 or max(rows) > y1 + 1e-6):
        problems.append(f"Docking slots run from y {min(rows) + hy0:.1f} to {max(rows) + hy0:.1f}, outside the reach.")
    return problems


def shift_to(config: dict, place_mm, measured) -> tuple[dict, list[str]]:
    """The whole sketch moved so that a place it draws at `place_mm` (machine mm, from BoardMap) is where the magnet
    really sits over it (`measured`, machine mm): magnet_home_mm changes, nothing else."""
    dx, dy = float(measured[0]) - float(place_mm[0]), float(measured[1]) - float(place_mm[1])
    hx, hy = magnet_home(config)
    new = dict(config)
    new["magnet_home_mm"] = [round(hx - dx, 2), round(hy - dy, 2)]
    report = [f"Every square moved {dx:+.1f} mm in X and {dy:+.1f} mm in Y: the magnet's home is now at "
              f"({hx - dx:.1f}, {hy - dy:.1f}) on the sketch."]
    return new, report + (check(new) or ["The whole board and both docking lanes are inside the magnet's reach."])


def from_points(config: dict, a1, h1, h8, far=None, left=None, right=None) -> tuple[dict, list[str]]:
    """A new config from points measured by steering the magnet (machine mm): the centres of a1, h1 and h8, and
    optionally the far corner of the travel and the first slot (the one nearest rank 1) of the left and right
    docking lanes. Lanes not measured are put DOCK_GAP_MM (dock_gap_mm) out from the edge files, at the existing
    slot rows. The result is in the sketch's millimetres (magnet_home_mm added back), like the config.
    Returns (config, report lines); the report says what was found and every problem check() sees."""
    home = magnet_home(config)
    config = machine(config)
    a1x, a1y = (float(v) for v in a1)
    h1x, h1y = (float(v) for v in h1)
    h8x, h8y = (float(v) for v in h8)
    sx, sy = (h1x - a1x) / 7, (h8y - h1y) / 7
    if sx < 10 or sy < 10:
        raise ValueError(f"h1 must be seven squares right of a1 and h8 seven squares above h1 "
                         f"(got {sx:.1f} mm and {sy:.1f} mm per square)")
    new = dict(config)
    new.pop("square_nudges_mm", None)      # the squares were measured again: old nudges were against the old layout
    new["a1_center_mm"] = [round(a1x, 2), round(a1y, 2)]
    new["square_mm"] = round(sx, 3) if abs(sx - sy) < 0.05 else [round(sx, 3), round(sy, 3)]
    if far is not None:
        new["travel_mm"] = [round(float(far[0]), 1), round(float(far[1]), 1)]
        new.pop("reach_mm", None)
    gap = float(config.get("dock_gap_mm", DOCK_GAP_MM))
    old_rows = [float(v) for v in config.get("park_rows_mm") or []]
    pitch = (old_rows[-1] - old_rows[0]) / (len(old_rows) - 1) if len(old_rows) > 1 else 35.0
    count = len(old_rows) or 12
    measured = left if left is not None else right
    ty = float(new["travel_mm"][1])
    if measured is None and old_rows:      # no slot measured: the rows stay as they are, less any past the travel
        rows = [round(y, 2) for y in old_rows if y <= ty + 1e-6]
    else:
        row0 = float(measured[1]) if measured is not None else a1y - sy / 2
        rows = [round(row0 + i * pitch, 2) for i in range(count) if row0 + i * pitch <= ty + 1e-6]
    new["park_rows_mm"] = rows
    hx = a1x + 7 * sx
    tx = float(new["travel_mm"][0])
    # Sketch 5 puts the docking columns on the edges of the travel: a lane worked out from the squares (not measured)
    # that lands a hair outside it is put on the edge instead.
    new["park_columns_mm"] = {"w": [round(float(left[0]) if left is not None else max(a1x - gap, 0.0), 2)],
                              "b": [round(float(right[0]) if right is not None else min(hx + gap, tx), 2)]}
    new["docking_lanes"] = list(config.get("docking_lanes") or ["w", "b"])
    if home != (0.0, 0.0):      # back into the sketch's millimetres, as the config keeps them
        hx0, hy0 = home
        new["a1_center_mm"] = [round(new["a1_center_mm"][0] + hx0, 2), round(new["a1_center_mm"][1] + hy0, 2)]
        new["park_columns_mm"] = {k: [round(x + hx0, 2) for x in v] for k, v in new["park_columns_mm"].items()}
        new["park_rows_mm"] = [round(y + hy0, 2) for y in new["park_rows_mm"]]
        new["travel_mm"] = [round(new["travel_mm"][0] + hx0, 1), round(new["travel_mm"][1] + hy0, 1)]
        new["magnet_home_mm"] = [hx0, hy0]
    ax, ay = new["a1_center_mm"]
    report = [f"a1 centre {ax:.1f}, {ay:.1f} mm on the sketch; squares {sx:.2f} mm along X and {sy:.2f} mm along Y"
              + ("" if abs(sx - sy) < 0.05 else " (the two differ: both are used)") + "."]
    rise, lean = h1y - a1y, h8x - h1x
    if abs(rise) > SKEW_WARN_MM or abs(lean) > SKEW_WARN_MM:
        report.append(f"The board is turned against the gantry: h1 is {rise:+.1f} mm off a1's line and h8 {lean:+.1f} mm off "
                      f"h1's. Turn the board until both are under {SKEW_WARN_MM:g} mm, then measure again: the gantry "
                      f"moves square to its rails, so a turned board puts the far squares off centre.")
    rows_out = new["park_rows_mm"]
    report.append(f"Docking lanes: left at x {new['park_columns_mm']['w'][0]:.1f}, right at x {new['park_columns_mm']['b'][0]:.1f}; "
                  f"{len(rows_out)} slots each from y {rows_out[0]:.1f} at a {pitch:.2f} mm pitch." if rows_out else "No docking slots fit.")
    problems = check(new)
    report += problems or ["Both docking lanes and the whole board are inside the magnet's reach."]
    return new, report


# ---------------------------------------------------------------------------------------------- tuning (nudges)
#
# The console's Tune panel: the person sends the magnet to a square, nudges it onto the square's real centre and
# says "right place". Each answer is kept in `square_nudges_mm` as how far that square's real centre is from where
# the layout above puts it (machine mm, so moving the whole sketch with magnet_home_mm keeps them). From those
# answers a correction field is fitted: an offset plus a tilt and stretch along X and Y (least squares, gently held
# to no tilt/stretch so one answer is a plain shift and two along a rank fix that rank's spacing). Every point the
# gantry goes to, squares, lanes between them and docking slots alike, moves by the field there; a square that was
# answered goes exactly where the person put it (its leftover from the field is added back on that square only).

NUDGE_CENTRE = (235.0, 210.0)   # the fit's origin and unit (mm): keeps the numbers well-sized
NUDGE_UNIT = 100.0
NUDGE_HOLD = 0.02               # how gently tilt/stretch is held to 0 (in mm² per (mm per 100 mm)²)
NUDGE_EXACT_MM = 0.5            # a point within this of an answered square's centre gets that square's exact answer


def nudges(config: dict) -> dict[str, tuple[float, float]]:
    """The answered squares: {"e4": (dx, dy)} in mm, real centre less the layout's."""
    out = {}
    for name, value in (config.get("square_nudges_mm") or {}).items():
        out[str(name).lower()] = (float(value[0]), float(value[1]))
    return out


def _solve3(a: list[list[float]], b: list[float]) -> list[float]:
    m = [row[:] + [v] for row, v in zip(a, b)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(m[r][i]))
        m[i], m[p] = m[p], m[i]
        for r in range(3):
            if r != i and m[i][i]:
                f = m[r][i] / m[i][i]
                m[r] = [x - f * y for x, y in zip(m[r], m[i])]
    return [m[i][3] / m[i][i] if m[i][i] else 0.0 for i in range(3)]


class NudgeField:
    """The correction (dx, dy) to add at any point (machine mm), fitted from the answered squares.
    `points` maps each answered square to (layout x, layout y, dx, dy)."""

    def __init__(self, points: dict[str, tuple[float, float, float, float]]):
        self.points = dict(points)
        self.cx = self.cy = [0.0, 0.0, 0.0]
        if points:
            rows = [(1.0, (x - NUDGE_CENTRE[0]) / NUDGE_UNIT, (y - NUDGE_CENTRE[1]) / NUDGE_UNIT) for x, y, _, _ in points.values()]
            ata = [[sum(r[i] * r[j] for r in rows) + (NUDGE_HOLD if i == j and i > 0 else 0.0) for j in range(3)] for i in range(3)]
            self.cx = _solve3(ata, [sum(r[i] * p[2] for r, p in zip(rows, points.values())) for i in range(3)])
            self.cy = _solve3(ata, [sum(r[i] * p[3] for r, p in zip(rows, points.values())) for i in range(3)])
        self.leftover = {name: (dx - self.smooth(x, y)[0], dy - self.smooth(x, y)[1]) for name, (x, y, dx, dy) in points.items()}

    def smooth(self, x: float, y: float) -> tuple[float, float]:
        u, v = (x - NUDGE_CENTRE[0]) / NUDGE_UNIT, (y - NUDGE_CENTRE[1]) / NUDGE_UNIT
        return self.cx[0] + self.cx[1] * u + self.cx[2] * v, self.cy[0] + self.cy[1] * u + self.cy[2] * v

    def at(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = self.smooth(x, y)
        for name, (px, py, _, _) in self.points.items():
            if abs(px - x) <= NUDGE_EXACT_MM and abs(py - y) <= NUDGE_EXACT_MM:
                lx, ly = self.leftover[name]
                return dx + lx, dy + ly
        return dx, dy

    def summary(self) -> dict:
        """For the page: the fit in plain numbers (mm, and mm per 100 mm)."""
        worst = max((math.hypot(*v) for v in self.leftover.values()), default=0.0)
        return {"shift": [round(self.cx[0], 2), round(self.cy[0], 2)],
                "stretch": [round(self.cx[1], 2), round(self.cy[2], 2)],      # X per 100 mm of X, Y per 100 mm of Y
                "tilt": [round(self.cx[2], 2), round(self.cy[1], 2)],         # X per 100 mm of Y, Y per 100 mm of X
                "leftover": {k: [round(a, 2), round(b, 2)] for k, (a, b) in self.leftover.items()},
                "worst_leftover": round(worst, 2), "count": len(self.points)}

    def __bool__(self) -> bool:
        return bool(self.points)
