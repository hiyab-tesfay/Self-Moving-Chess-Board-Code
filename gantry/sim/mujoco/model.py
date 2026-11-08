"""The MuJoCo model of the DUEN board: built from gantry_config.json and the measured piece meshes.

Everything is in metres inside MuJoCo; the numbers in this file are millimetres (as in the
config) and converted at the end. The board top (the surface the pieces slide on) is z = 0; the
gantry lives below it. Pieces are free bodies whose frame sits at the centre of the base, on
the underside, so a body's position is the point the planner reasons about.

Assumptions that nobody has measured yet are the fields of `Params`, with their defaults; the
README repeats them with the word "assumed".
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
import math
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
PI_DIR = HERE.parent.parent / "pi"
MESH_DIR = HERE / "meshes"
if str(PI_DIR) not in sys.path:
    sys.path.insert(0, str(PI_DIR))

import gantry_driver  # noqa: E402  (the project's own BoardMap and config loader)

FILES = "abcdefgh"
NAMES = {"p": "pawn", "n": "knight", "b": "bishop", "r": "rook", "q": "queen", "k": "king"}
CREAM, PURPLE, GOLD = "0.93 0.88 0.72 1", "0.33 0.18 0.48 1", "0.80 0.62 0.22 1"


@dataclass
class Params:
    """Every physical assumption in one place. Millimetres, newtons, seconds."""
    board_mm: float = 3.0            # thickness of the board top the pieces slide on (assumed)
    gap_mm: float = 1.0              # magnet top to board underside with the servo extended (assumed)
    retract_mm: float = 15.0         # the same with the servo retracted (assumed)
    friction: float = 0.3            # piece on board, and piece on piece (assumed)
    pla_density_g_cm3: float = 1.25  # PLA (assumed; a real print with infill is lighter, see infill)
    infill: float = 1.0              # fraction of the mesh volume that is plastic (1.0 = solid, assumed)
    disc_diameter_mm: float = 10.0   # steel disc in the base pocket (assumed; the pocket is 10.1 mm)
    disc_thickness_mm: float = 2.0   # (assumed)
    disc_recess_mm: float = 0.0      # disc bottom above the base underside (assumed flush)
    steel_density_g_cm3: float = 7.85
    f0_n: float = 6.0                # magnet pull at contact (placeholder, to measure)
    d0_mm: float = 3.0               # pull falls as (d0 / (d + d0))^2 (placeholder)
    lateral_fraction: float = 0.4    # lateral pull saturates at this fraction of the normal pull (placeholder)
    magnet_radius_mm: float = 7.5    # offset where the lateral pull saturates (the magnet's radius)
    release_radius_mm: float = 12.0  # beyond this the piece is lost (placeholder)
    accel_mm_s2: float = 800.0       # MAX_ACCEL_MM_S2 in firmware config.h
    timestep_s: float = 0.001
    servo: bool = True               # replay the servo lift (M7/M8 + settle) or leave the magnet at the board
    servo_travel_s: float = 0.15     # how long the MG90 takes to move the arm (assumed, inside the 0.3 s settle)
    permanent_magnet: bool = False   # True: the pull is always there (the N52 option); only the arm height changes it
    box_in: float = 30.0             # the enclosure top is 30 x 30 inches (WOODEN BOX drawing)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PieceSpec:
    symbol: str                      # "P" white pawn, "n" black knight, ...
    home: str                        # the square it starts on, which names the body
    kind: str                        # "p", "n", ...

    @property
    def body(self) -> str:
        return f"{self.symbol}_{self.home}"

    @property
    def white(self) -> bool:
        return self.symbol.isupper()


def start_pieces() -> list[PieceSpec]:
    back = "rnbqkbnr"
    pieces = []
    for file, kind in zip(FILES, back):
        pieces.append(PieceSpec(kind.upper(), f"{file}1", kind))
        pieces.append(PieceSpec("P", f"{file}2", "p"))
        pieces.append(PieceSpec("p", f"{file}7", "p"))
        pieces.append(PieceSpec(kind, f"{file}8", kind))
    return pieces


def load_mesh_info(mesh_dir: Path = MESH_DIR) -> dict:
    path = mesh_dir / "pieces.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing: run prepare_meshes.py on the piece STLs first")
    return json.loads(path.read_text())


def composite_inertia(info: dict, params: Params) -> dict:
    """Mass, centre of mass and inertia (SI) of one piece: the PLA body plus the steel disc in its base."""
    rho_pla = params.pla_density_g_cm3 * 1e-3 * params.infill          # g/mm^3
    m_pla = info["volume_mm3"] * rho_pla                                # g
    c_pla = np.array(info["center_of_mass_mm"])
    i_pla = np.array(info["inertia_unit_density_mm5"]) * rho_pla       # g mm^2 about c_pla
    r, t = params.disc_diameter_mm / 2, params.disc_thickness_mm
    m_disc = math.pi * r * r * t * params.steel_density_g_cm3 * 1e-3    # g
    c_disc = np.array([0.0, 0.0, params.disc_recess_mm + t / 2])
    i_disc = np.diag([m_disc * (3 * r * r + t * t) / 12, m_disc * (3 * r * r + t * t) / 12, m_disc * r * r / 2])
    m = m_pla + m_disc
    c = (m_pla * c_pla + m_disc * c_disc) / m

    def shift(i, mass, offset):
        d = np.asarray(offset)
        return i + mass * (np.dot(d, d) * np.eye(3) - np.outer(d, d))

    i_total = shift(i_pla, m_pla, c_pla - c) + shift(i_disc, m_disc, c_disc - c)
    return {"mass_kg": m * 1e-3, "com_m": c * 1e-3, "inertia_kg_m2": i_total * 1e-9, "mass_pla_g": m_pla, "mass_disc_g": m_disc}


class Geometry:
    """Where things are, in mm, from the project's own BoardMap."""

    def __init__(self, config: dict):
        self.config = config
        self.board = gantry_driver.BoardMap(config)
        self.square = float(config["square_mm"])
        self.travel = tuple(float(v) for v in config["travel_mm"])
        self.slots_per_lane = len(config["park_rows_mm"])

    def place_mm(self, place: str) -> tuple[float, float]:
        """A square ("e4"), a slot ("w:3") or "x,y" in squares, the way the firmware reads them.

        Slots go through point_mm (one lane of twelve, as carry_plan and board_paths number them)
        rather than BoardMap.square_mm, which still counts eight per column.
        """
        if ":" in place:
            color, index = place[0], int(place[2:])
            lane, row = divmod(index, self.slots_per_lane)
            column = -1 - lane if color == "w" else 8 + lane
            return self.board.point_mm(float(column), float(row))
        if "," in place:
            x, y = (float(v) for v in place.split(","))
            return self.board.point_mm(x, y)
        return self.board.square_mm(place)

    def square_center(self, name: str) -> tuple[float, float]:
        return self.board.square_mm(name)

    def nearest_place(self, x: float, y: float) -> str:
        """The square or slot nearest a point, for reports."""
        best, best_d = "?", float("inf")
        for f in FILES:
            for r in range(1, 9):
                sx, sy = self.board.square_mm(f"{f}{r}")
                d = math.hypot(sx - x, sy - y)
                if d < best_d:
                    best, best_d = f"{f}{r}", d
        for color in "wb":
            for i in range(self.slots_per_lane):
                sx, sy = self.place_mm(f"{color}:{i}")
                d = math.hypot(sx - x, sy - y)
                if d < best_d:
                    best, best_d = f"{color}:{i}", d
        return best


def build_xml(config: dict, params: Params, mesh_dir: Path = MESH_DIR) -> tuple[str, dict]:
    """The MJCF as a string, plus a dict describing the pieces (name -> spec, masses)."""
    geo = Geometry(config)
    meshes = load_mesh_info(mesh_dir)
    mm = 1e-3
    board_t = params.board_mm
    cx, cy = geo.travel[0] / 2, geo.travel[1] / 2            # centre of the magnet's reach, where the box top is centred
    half_box = params.box_in * 25.4 / 2
    z_extended = -(board_t + params.gap_mm)
    z_retracted = -(board_t + params.retract_mm)

    out = []
    w = out.append
    w('<mujoco model="duen_board">')
    w(f'  <compiler angle="degree" meshdir="{mesh_dir}" autolimits="true"/>')
    w(f'  <option timestep="{params.timestep_s}" gravity="0 0 -9.81" integrator="implicitfast" cone="elliptic" noslip_iterations="3" noslip_tolerance="1e-8"/>')
    w('  <visual><headlight ambient="0.4 0.4 0.4" diffuse="0.6 0.6 0.6"/><quality shadowsize="2048"/><map znear="0.005"/></visual>')
    w('  <default>')
    w(f'    <geom friction="{params.friction} 0.005 0.0001" solref="0.002 1" solimp="0.95 0.99 0.001" condim="3"/>')
    w('    <default class="visual"><geom contype="0" conaffinity="0" group="1"/></default>')
    w('    <default class="piece_base"><geom type="cylinder" contype="3" conaffinity="3" group="3"/></default>')
    w('    <default class="piece_hull"><geom type="mesh" contype="2" conaffinity="2" group="3"/></default>')
    w('  </default>')
    w('  <asset>')
    for kind, info in meshes["pieces"].items():
        w(f'    <mesh name="{NAMES[kind]}" file="{info["file"]}" scale="{mm} {mm} {mm}"/>')
    if "holder" in meshes:
        w(f'    <mesh name="em_holder" file="{meshes["holder"]["file"]}" scale="{mm} {mm} {mm}"/>')
    w('    <material name="board_top" rgba="0.55 0.38 0.22 1" specular="0.2"/>')
    w('  </asset>')
    w('  <worldbody>')
    w('    <light pos="0.2 0.2 1.0" dir="0 0 -1" diffuse="0.7 0.7 0.7" castshadow="true"/>')
    w('    <light pos="0.5 0.0 0.6" dir="-0.3 0.2 -1" diffuse="0.4 0.4 0.4" castshadow="false"/>')
    w(f'    <camera name="top" pos="{cx*mm} {cy*mm} 1.0" xyaxes="1 0 0 0 1 0" fovy="35"/>')
    w(f'    <camera name="oblique" pos="{(cx-150)*mm} {(cy-520)*mm} 0.45" xyaxes="1 0 0 0 0.65 0.76" fovy="40"/>')
    # The playing surface: an infinite plane for contact (hidden), the box top as what you see.
    w('    <geom name="board_plane" type="plane" size="1 1 0.01" pos="0 0 0" contype="1" conaffinity="1" group="3" rgba="0 0 0 0"/>')
    w(f'    <geom name="box_top" class="visual" type="box" size="{half_box*mm} {half_box*mm} {board_t/2*mm}" pos="{cx*mm} {cy*mm} {(-board_t/2 - 0.1)*mm}" material="board_top"/>')
    for i, f in enumerate(FILES):
        for r in range(1, 9):
            sx, sy = geo.square_center(f"{f}{r}")
            dark = (i + r) % 2 == 0
            w(f'    <geom class="visual" type="box" size="{geo.square/2*mm} {geo.square/2*mm} {0.05*mm}" pos="{sx*mm} {sy*mm} {-0.05*mm}" rgba="{PURPLE if dark else CREAM}"/>')
    for color in "wb":
        for i in range(geo.slots_per_lane):
            sx, sy = geo.place_mm(f"{color}:{i}")
            w(f'    <geom class="visual" type="cylinder" size="{14*mm} {0.04*mm}" pos="{sx*mm} {sy*mm} {-0.06*mm}" rgba="{GOLD}"/>')
    # The frame, as primitives: two 2020 rails along X, a 2040 beam along Y that rides on them (mocap), the carriage on the beam.
    rail_z = z_retracted - 52 - 30
    for y in (-30.0, geo.travel[1] + 30.0):
        w(f'    <geom class="visual" type="box" size="{(geo.travel[0]/2+60)*mm} {10*mm} {10*mm}" pos="{cx*mm} {y*mm} {rail_z*mm}" rgba="0.3 0.3 0.32 1"/>')
    w(f'    <body name="beam" mocap="true" pos="0 {cy*mm} {(rail_z+20)*mm}">')
    w(f'      <geom class="visual" type="box" size="{10*mm} {(geo.travel[1]/2+40)*mm} {20*mm}" rgba="0.3 0.3 0.32 1"/>')
    w('    </body>')
    w(f'    <body name="magnet" mocap="true" pos="0 0 {z_retracted*mm}">')
    if "holder" in meshes:
        w('      <geom class="visual" type="mesh" mesh="em_holder" rgba="0.2 0.2 0.22 1"/>')
    else:
        w(f'      <geom class="visual" type="box" size="{14.5*mm} {15.5*mm} {26*mm}" pos="0 0 {-26*mm}" rgba="0.2 0.2 0.22 1"/>')
    w(f'      <geom class="visual" type="cylinder" size="{13.5*mm} {10*mm}" pos="0 0 {-10*mm}" rgba="0.75 0.75 0.78 1"/>')
    w(f'      <geom class="visual" type="sphere" size="{2*mm}" rgba="1 0.1 0.1 1"/>')
    w(f'      <geom class="visual" type="box" size="{18*mm} {18*mm} {10*mm}" pos="0 0 {(-52-10)*mm}" rgba="0.5 0.5 0.52 1"/>')
    w('    </body>')
    specs = {}
    for spec in start_pieces():
        info = meshes["pieces"][spec.kind]
        inertia = composite_inertia(info, params)
        i = inertia["inertia_kg_m2"]
        c = inertia["com_m"]
        rgba = "0.96 0.95 0.90 1" if spec.white else "0.12 0.10 0.12 1"
        sx, sy = geo.square_center(spec.home)
        base_r = info["base_diameter_mm"] / 2
        base_h = max(info["base_straight_mm"], 2.0)
        yaw = 0 if spec.white else 180     # the knights face the other side
        w(f'    <body name="{spec.body}" pos="{sx*mm} {sy*mm} 0" euler="0 0 {yaw}">')
        w('      <freejoint/>')
        w(f'      <inertial pos="{c[0]} {c[1]} {c[2]}" mass="{inertia["mass_kg"]}" fullinertia="{i[0,0]} {i[1,1]} {i[2,2]} {i[0,1]} {i[0,2]} {i[1,2]}"/>')
        w(f'      <geom class="visual" type="mesh" mesh="{NAMES[spec.kind]}" rgba="{rgba}"/>')
        w(f'      <geom class="piece_base" size="{base_r*mm} {base_h/2*mm}" pos="0 0 {base_h/2*mm}"/>')
        w(f'      <geom class="piece_hull" mesh="{NAMES[spec.kind]}"/>')
        w('    </body>')
        specs[spec.body] = {"symbol": spec.symbol, "home": spec.home, "kind": spec.kind, "mass_g": inertia["mass_kg"] * 1e3,
                            "mass_pla_g": inertia["mass_pla_g"], "mass_disc_g": inertia["mass_disc_g"],
                            "base_mm": info["base_diameter_mm"], "height_mm": info["height_mm"], "com_z_mm": c[2] * 1e3}
    w('  </worldbody>')
    w('</mujoco>')
    details = {"pieces": specs, "z_extended_mm": z_extended, "z_retracted_mm": z_retracted, "params": params.as_dict(),
               "meshes": meshes}
    return "\n".join(out) + "\n", details


def load_config(path: Path | None = None) -> dict:
    return gantry_driver.load_config(path or gantry_driver.DEFAULT_CONFIG)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Write the MJCF for the board to a file (for the MuJoCo viewer).")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=HERE / "out" / "duen_board.xml")
    args = parser.parse_args()
    xml, details = build_xml(load_config(args.config), Params())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(xml)
    for name, spec in details["pieces"].items():
        if spec["home"][1] in "12" and spec["home"][0] in "abcde":
            print(f"{name:6s} {spec['mass_g']:5.1f} g (PLA {spec['mass_pla_g']:.1f} + disc {spec['mass_disc_g']:.2f})  base {spec['base_mm']} mm  CoM z {spec['com_z_mm']:.1f} mm")
    print(f"wrote {args.out}")
