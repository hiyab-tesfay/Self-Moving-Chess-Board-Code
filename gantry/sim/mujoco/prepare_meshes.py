#!/usr/bin/env python3
"""Turn the printed-piece STLs into the meshes the simulation loads, and measure them.

The source STLs (PawnTopFinal, KnightNoSpikes, BishopNoSpikes, RookNoSpikes, QueenNoSpikes,
KingTest, and the gantry's `em holder`) are in millimetres with +Y up and the piece floating
somewhere in space. This script rotates each so +Z is up, puts the centre of the base at the
origin, saves it as a binary STL in meshes/, and writes meshes/pieces.json with what the mesh
says: base diameter, height, volume, centre of mass, inertia tensor (unit density), and the
pocket in the base. The simulation reads the JSON, so trimesh is only needed here.

    python3 prepare_meshes.py SOURCE_DIR [--holder "PATH/em holder.stl"]

Nothing here is estimated: every number comes from the triangles.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import trimesh

HERE = Path(__file__).resolve().parent
MESH_DIR = HERE / "meshes"

SOURCES = {
    "p": "PawnTopFinal.stl",
    "n": "KnightNoSpikes.stl",
    "b": "BishopNoSpikes.stl",
    "r": "RookNoSpikes.stl",
    "q": "QueenNoSpikes.stl",
    "k": "KingTest.stl",
}
NAMES = {"p": "pawn", "n": "knight", "b": "bishop", "r": "rook", "q": "queen", "k": "king"}

# +Y up in the STL -> +Z up in the simulation: (x, y, z) -> (x, -z, y).
Y_UP_TO_Z_UP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=float)


def ring_radii(mesh: trimesh.Trimesh, z: float, centre) -> np.ndarray:
    """Radii (from the base centre) of the mesh's outline at height z above the base."""
    segments = trimesh.intersections.mesh_plane(mesh, np.array([0, 0, 1.0]), np.array([0, 0, z]))
    if len(segments) == 0:
        return np.zeros(0)
    pts = segments.reshape(-1, 3)[:, :2]
    return np.hypot(pts[:, 0] - centre[0], pts[:, 1] - centre[1])


def measure(mesh: trimesh.Trimesh) -> dict:
    """Base diameter, pocket, height, volume, centre of mass and inertia of a Z-up mesh whose base centre is the origin."""
    radii = ring_radii(mesh, 0.3, (0.0, 0.0))
    base_radius = float(radii.max())
    # A pocket in the base shows as an inner ring at the bottom: find its radius and how high it goes.
    inner = radii[radii < base_radius * 0.6]
    pocket_radius = float(inner.max()) if len(inner) else 0.0
    pocket_depth = 0.0
    if pocket_radius:
        for z in np.arange(0.5, 15.0, 0.25):
            r = ring_radii(mesh, z, (0.0, 0.0))
            if len(r) == 0 or not (r < base_radius * 0.6).any():
                break
            pocket_depth = float(z)
    # How far up the base keeps its full width (the part that touches a neighbour).
    straight = 0.0
    for z in np.arange(0.5, 12.0, 0.5):
        r = ring_radii(mesh, z, (0.0, 0.0))
        if len(r) == 0 or r.max() < base_radius - 0.1:
            break
        straight = float(z)
    props = mesh.mass_properties
    return {
        "base_diameter_mm": round(2 * base_radius, 2),
        "base_straight_mm": straight,
        "pocket_diameter_mm": round(2 * pocket_radius, 2),
        "pocket_depth_mm": pocket_depth,
        "height_mm": round(float(mesh.bounds[1][2] - mesh.bounds[0][2]), 2),
        "max_diameter_mm": round(2 * float(np.hypot(mesh.vertices[:, 0], mesh.vertices[:, 1]).max()), 2),
        "volume_mm3": round(float(mesh.volume), 1),
        "center_of_mass_mm": [round(float(v), 3) for v in props["center_mass"]],
        "inertia_unit_density_mm5": [[round(float(v), 1) for v in row] for row in props["inertia"]],
        "watertight": bool(mesh.is_watertight),
        "faces": int(len(mesh.faces)),
    }


def prepare_piece(path: Path) -> tuple[trimesh.Trimesh, dict]:
    mesh = trimesh.load(path, force="mesh")
    mesh.apply_transform(Y_UP_TO_Z_UP)
    # The base: the vertices within half a millimetre of the lowest point.
    v = mesh.vertices
    low = v[:, 2].min()
    base = v[(v[:, 2] - low) < 0.5]
    centre = (base[:, :2].max(0) + base[:, :2].min(0)) / 2
    mesh.apply_translation([-centre[0], -centre[1], -low])
    return mesh, measure(mesh)


def prepare_holder(path: Path) -> tuple[trimesh.Trimesh, dict]:
    mesh = trimesh.load(path, force="mesh")      # already Z up, 29 x 31 x 52 mm
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -hi[2]])   # top face at z = 0, centred
    return mesh, {"extents_mm": [round(float(x), 2) for x in (hi - lo)], "faces": int(len(mesh.faces))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="folder with the six piece STLs")
    parser.add_argument("--holder", type=Path, help="the gantry's 'em holder.stl' (optional)")
    parser.add_argument("--out", type=Path, default=MESH_DIR)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"units": "mm", "up": "+Z", "origin": "centre of the base, on the underside", "pieces": {}, "sources": {}}
    for symbol, filename in SOURCES.items():
        mesh, info = prepare_piece(args.source / filename)
        out = args.out / f"{NAMES[symbol]}.stl"
        mesh.export(out)
        report["pieces"][symbol] = {"name": NAMES[symbol], "file": out.name, **info}
        report["sources"][symbol] = filename
        print(f"{NAMES[symbol]:7s} base {info['base_diameter_mm']:5.2f} mm  height {info['height_mm']:5.2f} mm  "
              f"volume {info['volume_mm3']:8.1f} mm^3  pocket {info['pocket_diameter_mm']} x {info['pocket_depth_mm']} mm  "
              f"CoM z {info['center_of_mass_mm'][2]:.2f} mm")
    if args.holder:
        mesh, info = prepare_holder(args.holder)
        mesh.export(args.out / "em_holder.stl")
        report["holder"] = {"file": "em_holder.stl", "source": args.holder.name, **info}
        print(f"holder  {info['extents_mm']} mm")
    (args.out / "pieces.json").write_text(json.dumps(report, indent=1) + "\n")
    print(f"wrote {args.out / 'pieces.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
