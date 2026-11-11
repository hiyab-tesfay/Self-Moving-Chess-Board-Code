#!/usr/bin/env python3
"""Run the planner's carries through the MuJoCo board and report what the pieces did.

    python3 run_sim.py --scenario knight          one scenario (knight, capture, castle, enpassant,
                                                  promotion, reset, game)
    python3 run_sim.py --all                      every scenario, with and without the servo lift,
                                                  the speed sweep and the friction/pull sweep; writes
                                                  results.md and out/
    python3 run_sim.py --sweep                    only the knight speed x acceleration sweep
    python3 run_sim.py --scenario knight --friction 0.2 --f0 8 --carry-speed 80 --accel 1200

Every scenario builds its position with python-chess, asks carry_plan + gantry_driver.plan_carry
for the CARRY lines (so the paths come from board_paths with the real piece widths, and make-room
carries appear when the planner adds them), and replays those lines the way the firmware runs
them. Rendering (an MP4 per scenario, and a top-down trajectory PNG) goes to out/ when
MuJoCo's offscreen renderer works; --no-render skips the videos.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import platform
import random
import sys
import time

if platform.system() == "Darwin":
    os.environ.setdefault("MUJOCO_GL", "cgl")       # offscreen rendering on a Mac without a window

import mujoco
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import model as board_model  # noqa: E402
import simulate  # noqa: E402
from model import Params  # noqa: E402
import gantry_driver  # noqa: E402
import carry_plan  # noqa: E402
import chess  # noqa: E402

OUT = HERE / "out"
RESULTS = HERE / "results.md"
ARRIVE_MM = 2.0

SCENARIO_ORDER = ["knight", "capture", "castle", "enpassant", "promotion", "reset", "game"]
SCENARIO_TEXT = {
    "knight": "knight g1->f3 from the start position (through the lane between the f and g pawns)",
    "capture": "1.e4 d5 2.exd5: the person takes the d5 pawn off, then the taker goes",
    "castle": "1.e4 e5 2.Nf3 Nc6 3.Bc4 Bc5 4.O-O: the king, then the rook past it",
    "enpassant": "1.e4 Nf6 2.e5 d5 3.exd6 e.p.: the person takes the d5 pawn off, then the capturing pawn goes",
    "promotion": "e7e8=Q: the pawn is carried and stands in for the queen (no docked spares any more)",
    "reset": "a full reset from a mid-game position: strays go home, taken pieces are put back by hand",
    "game": "a random game, 40 plies (20 moves each side), seeded, every carry in sequence",
}


# ----------------------------------------------------------------------------- the positions

def position_after(ucis: list[str]) -> tuple[chess.Board, carry_plan.Layout]:
    """Board and physical layout after the computer has played these moves (captured pieces taken off by the person)."""
    board, layout = chess.Board(), carry_plan.Layout()
    for uci in ucis:
        move = chess.Move.from_uci(uci)
        layout.plan_computer(board, move)
        board.push(move)
    return board, layout


def random_game(plies: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    board = chess.Board()
    ucis = []
    for _ in range(plies):
        moves = list(board.legal_moves)
        if not moves:
            break
        move = rng.choice(moves)
        ucis.append(move.uci())
        board.push(move)
    return ucis


@dataclass
class Scenario:
    name: str
    text: str
    pieces: dict                 # place -> symbol as the scenario starts
    jobs: list                   # job dicts for carry_plan / gantry_driver
    moves: list = field(default_factory=list)


def build_scenario(name: str, config: dict, seed: int = 7) -> Scenario:
    if name == "knight":
        board, layout = position_after([])
        job = carry_plan.job_for_move(layout.copy(), board, chess.Move.from_uci("g1f3"))
        return Scenario(name, SCENARIO_TEXT[name], Layout_pieces(board), [job], ["g1f3"])
    if name == "capture":
        setup = ["e2e4", "d7d5"]
        board, layout = position_after(setup)
        pieces = dict(layout.pieces)
        job = carry_plan.job_for_move(layout, board, chess.Move.from_uci("e4d5"))
        return Scenario(name, SCENARIO_TEXT[name], pieces, [job], setup + ["e4d5"])
    if name == "castle":
        setup = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5"]
        board, layout = position_after(setup)
        pieces = dict(layout.pieces)
        job = carry_plan.job_for_move(layout, board, chess.Move.from_uci("e1g1"))
        return Scenario(name, SCENARIO_TEXT[name], pieces, [job], setup + ["e1g1"])
    if name == "enpassant":
        setup = ["e2e4", "g8f6", "e4e5", "d7d5"]
        board, layout = position_after(setup)
        pieces = dict(layout.pieces)
        job = carry_plan.job_for_move(layout, board, chess.Move.from_uci("e5d6"))
        return Scenario(name, SCENARIO_TEXT[name], pieces, [job], setup + ["e5d6"])
    if name == "promotion":
        board = chess.Board("7k/4P3/8/8/8/8/8/K7 w - - 0 1")
        layout = carry_plan.Layout(board)
        pieces = dict(layout.pieces)
        job = carry_plan.job_for_move(layout, board, chess.Move.from_uci("e7e8q"))
        return Scenario(name, SCENARIO_TEXT[name], pieces, [job], ["e7e8q"])
    if name == "reset":
        setup = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5c6", "d7c6", "d2d3", "f8d6",
                 "b1c3", "g8f6", "c1g5", "h7h6", "g5f6", "d8f6", "d1d2", "c8e6"]
        board, layout = position_after(setup)
        pieces = dict(layout.pieces)
        jobs = gantry_driver.reset_jobs(layout, config)
        return Scenario(name, SCENARIO_TEXT[name], pieces, jobs, setup)
    if name == "game":
        ucis = random_game(40, seed)
        jobs, _ = gantry_driver.game_jobs(ucis)
        return Scenario(name, SCENARIO_TEXT[name] + f" (seed {seed}: {' '.join(ucis)})", Layout_pieces(chess.Board()), jobs, ucis)
    raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIO_ORDER)}")


def Layout_pieces(board: chess.Board) -> dict:
    return dict(carry_plan.Layout(board).pieces)


# ----------------------------------------------------------------------------- recording

class Recorder:
    """Frames from MuJoCo's offscreen renderer into a small MP4, the camera following the carriage."""

    def __init__(self, sim: simulate.Sim, path: Path, duration_s: float, width=480, height=360, fps=20, max_frames=400):
        import cv2
        import mujoco
        self.cv2, self.mujoco = cv2, mujoco
        self.renderer = mujoco.Renderer(sim.model, height, width)
        self.cam = mujoco.MjvCamera()
        self.cam.type = mujoco.mjtCamera.mjCAMERA_FREE
        self.cam.distance, self.cam.azimuth, self.cam.elevation = 0.42, 90, -48
        self.cam.lookat[:] = [0.2, 0.2, 0.02]
        self.path = path
        self.writer = None
        for codec in ("avc1", "mp4v"):
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), fps, (width, height))
            if writer.isOpened():
                self.writer = writer
                break
            writer.release()
        if self.writer is None:
            raise RuntimeError("cv2.VideoWriter could not open an MP4 writer")
        self.frames = 0
        sim.frame_dt = max(1.0 / fps, duration_s / max_frames)
        sim.frame_hook = self.frame
        self.speedup = sim.frame_dt * fps

    def frame(self, sim: simulate.Sim, t: float, phase, timeline) -> None:
        x, y = sim.carriage
        target = np.array([x * 1e-3, y * 1e-3, 0.02])
        self.cam.lookat[:] = 0.85 * np.asarray(self.cam.lookat) + 0.15 * target
        self.renderer.update_scene(sim.data, self.cam)
        img = self.renderer.render()
        img = np.ascontiguousarray(img[:, :, ::-1])
        label = phase.label if phase is not None else ""
        text = f"t={t:6.2f}s  {label}" + (f"  (x{self.speedup:.0f})" if self.speedup > 1.05 else "")
        self.cv2.putText(img, text, (8, 20), self.cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, self.cv2.LINE_AA)
        self.writer.write(img)
        self.frames += 1
        self.last = img

    def close(self, png_path: Path | None = None) -> None:
        self.writer.release()
        if png_path is not None and getattr(self, "last", None) is not None:
            self.cv2.imwrite(str(png_path), self.last)
        self.renderer.close()


def plot_trajectories(sim: simulate.Sim, jobs: list[simulate.JobResult], path: Path, title: str) -> None:
    """Top-down PNG: squares and slots, pieces where the job began, planned paths (dashed) and the carried pieces' real paths."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle

    geo = sim.geo
    fig, ax = plt.subplots(figsize=(8.5, 7.6), dpi=90)
    sq = geo.square
    for i, f in enumerate(board_model.FILES):
        for r in range(1, 9):
            sx, sy = geo.square_center(f"{f}{r}")
            ax.add_patch(Rectangle((sx - sq / 2, sy - sq / 2), sq, sq, facecolor="#54307a" if (i + r) % 2 == 0 else "#eee0b8", edgecolor="none"))
    for color in "wb":
        for k in range(geo.slots_per_lane):
            sx, sy = geo.place_mm(f"{color}:{k}")
            ax.add_patch(Circle((sx, sy), 14, facecolor="none", edgecolor="#cc9e38", lw=1.2))
    first = jobs[0]
    for body, (x, y) in first.start_positions.items():
        if y < -100:
            continue
        spec = sim.details["pieces"][body]
        ax.add_patch(Circle((x, y), spec["base_mm"] / 2, facecolor="#f4f1e6" if spec["symbol"].isupper() else "#2a2228", edgecolor="#777", lw=0.6, alpha=0.9))
    colors = plt.cm.tab10.colors
    k = 0
    for job in jobs:
        trace = np.array(job.trace) if job.trace else np.zeros((0, 6))
        for res, planned in zip(job.carries, job.planned):
            c = colors[k % 10]
            px = [p[0] for p in planned]
            py = [p[1] for p in planned]
            ax.plot(px, py, "--", color=c, lw=1.0, alpha=0.8)
            ax.plot(px[1:-1], py[1:-1], ".", color=c, ms=4)
            if len(trace):
                mine = trace[(trace[:, 3] == res.index) & ~np.isnan(trace[:, 4])]
                if len(mine):
                    ax.plot(mine[:, 4], mine[:, 5], "-", color=c, lw=1.8)
            if res.piece and not math.isnan(res.final_error_mm):
                ax.plot(*res.final_mm, "o", mfc="none", mec=c, ms=7, mew=1.5)
            if res.lost and res.lost_mm:
                ax.plot(*res.lost_mm, "x", color="red", ms=10, mew=2)
            k += 1
    ax.plot(*(np.array(first.trace)[:, 1:3].T if first.trace else ([], [])), "-", color="0.5", lw=0.5, alpha=0.5)
    ax.set_xlim(-10, geo.travel[0] + 10)
    ax.set_ylim(-10, geo.travel[1] + 10)
    ax.set_aspect("equal")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ----------------------------------------------------------------------------- running

@dataclass
class ScenarioResult:
    scenario: Scenario
    servo: bool
    jobs: list                  # JobResult per job
    lines: list                 # (text, note) per job, flattened
    video: Path | None = None
    plot: Path | None = None

    @property
    def carries(self) -> list:
        return [c for j in self.jobs for c in j.carries]

    variant: str = ""            # "", "no servo", "permanent magnet, arm down 15 mm", ...

    @property
    def make_room(self) -> int:
        return sum(1 for text, note in self.lines if "(make room)" in note or "(put back)" in note)

    @property
    def title(self) -> str:
        return self.scenario.name + (f" ({self.variant})" if self.variant else "")

    def summary(self) -> dict:
        carries = self.carries
        ok = [c for c in carries if c.piece and not c.lost]
        worst = max(ok, key=lambda c: c.final_error_mm) if ok else None
        others = max(((c.others_max_mm, c.others_name) for c in carries), default=(0.0, ""))
        tilt = max(((c.max_tilt_deg, c.tilt_name) for c in carries), default=(0.0, ""))
        fallen = sorted({n for c in carries for n in c.fallen})
        lost = [c for c in carries if c.lost or not c.piece]
        drift = max(((j.others_max_mm, j.others_name) for j in self.jobs), default=(0.0, ""))
        return {
            "scenario": self.title, "servo": self.servo, "carries": len(carries),
            "arrived": sum(1 for c in ok if c.final_error_mm <= ARRIVE_MM),
            "mean_error": float(np.mean([c.final_error_mm for c in ok])) if ok else float("nan"),
            "worst_error": worst.final_error_mm if worst else float("nan"),
            "worst_carry": f"{worst.source}->{worst.target}" if worst else "",
            "others_mm": others[0], "others_name": others[1],
            "drift_mm": drift[0], "drift_name": drift[1],
            "tilt_deg": tilt[0], "tilt_name": tilt[1], "fallen": fallen,
            "lost": [f"{c.source}->{c.target}" + (f" at {c.lost_near}" if c.lost_near else " (nothing under the magnet)") for c in lost],
            "make_room": self.make_room,
            "sim_s": sum(j.sim_seconds for j in self.jobs), "wall_s": sum(j.wall_seconds for j in self.jobs),
        }


def variant_name(params: Params) -> str:
    parts = []
    if not params.servo:
        parts.append("no servo")
    if params.permanent_magnet:
        parts.append(f"permanent magnet, arm down {params.retract_mm:g} mm")
    if params.infill != 1.0:
        parts.append(f"infill {params.infill:g}")
    return ", ".join(parts)


def run_scenario(name: str, config: dict, params: Params, render: bool, out_dir: Path, seed: int = 7, quiet=False) -> ScenarioResult:
    scenario = build_scenario(name, config, seed)
    board = gantry_driver.BoardMap(config)
    sim = simulate.Sim(config, params)
    sim.place(scenario.pieces)
    all_lines = []
    job_lines = []
    for job in scenario.jobs:
        lines = gantry_driver.plan_carry(job, board, config)
        job_lines.append((job, lines))
        all_lines += lines
    variant = variant_name(params)
    tag = name + ("_" + "".join(ch if ch.isalnum() else "_" for ch in variant) if variant else "")
    recorder = None
    if render:
        out_dir.mkdir(parents=True, exist_ok=True)
        total = 0.0
        for job, lines in job_lines:
            tl = simulate.Timeline(sim.geo, config, params)
            for text, _ in lines:
                if text.startswith("CARRY"):
                    tl.add_carry(text)
            total += tl.duration + 0.5
        try:
            recorder = Recorder(sim, out_dir / f"{tag}.mp4", total)
        except Exception as exc:    # no GL, no codec: the plots still get written
            print(f"  (no video: {exc})")
            recorder = None
    results = []
    for job, lines in job_lines:
        if not quiet:
            for text, note in lines:
                print(f"    {text:<44s} ; {note}")
        # A "remove" step is the person's: the piece leaves the board before the gantry carries anything.
        for step in job.get("steps", []):
            if step.get("op") == "remove" and sim.take_off(step["square"]) and not quiet:
                print(f"    (person takes {step['piece']} off {step['square']})")
        results.append(sim.run_lines([t for t, _ in lines], job.get("label", name)))
    result = ScenarioResult(scenario, params.servo, results, all_lines, variant=variant)
    if recorder is not None:
        recorder.close(out_dir / f"{tag}_last.png")
        result.video = out_dir / f"{tag}.mp4"
    out_dir.mkdir(parents=True, exist_ok=True)
    result.plot = out_dir / f"{tag}_paths.png"
    plot_trajectories(sim, results, result.plot, f"{result.title}: {scenario.text}"[:110])
    return result


def knight_once(config: dict, params: Params) -> simulate.CarryResult:
    """The knight g1->f3 carry alone, for the sweeps."""
    scenario = build_scenario("knight", config)
    sim = simulate.Sim(config, params)
    sim.place(scenario.pieces)
    lines = gantry_driver.plan_carry(scenario.jobs[0], gantry_driver.BoardMap(config), config)
    return sim.run_lines([t for t, _ in lines], "knight").carries[0]


def speed_sweep(config: dict, params: Params, speeds=(40, 60, 80, 100), accels=(400, 800, 1200)) -> list[dict]:
    rows = []
    for v in speeds:
        for a in accels:
            cfg = dict(config, carry_speed_mm_s=float(v))
            p = Params(**{**params.as_dict(), "accel_mm_s2": float(a)})
            c = knight_once(cfg, p)
            rows.append({"speed": v, "accel": a, "error": c.final_error_mm, "lag": c.max_lag_mm, "lost": c.lost, "lost_near": c.lost_near,
                         "others": c.others_max_mm, "others_name": c.others_name, "tilt": c.max_tilt_deg, "duration": c.duration_s})
            print(f"  carry {v:3d} mm/s  accel {a:4d} mm/s^2: error {c.final_error_mm:5.2f} mm, lag {c.max_lag_mm:5.2f} mm, "
                  f"{'LOST near ' + c.lost_near if c.lost else 'held'}, others moved {c.others_max_mm:.2f} mm ({c.others_name}), {c.duration_s:.1f} s")
    return rows


def pull_sweep(config: dict, params: Params, frictions=(0.15, 0.2, 0.3, 0.4), pulls=(3.0, 6.0, 12.0)) -> list[dict]:
    """How the lag and the final error depend on the two numbers nobody has measured: friction and the pull."""
    rows = []
    for mu in frictions:
        for f0 in pulls:
            p = Params(**{**params.as_dict(), "friction": mu, "f0_n": f0})
            c = knight_once(config, p)
            rows.append({"friction": mu, "f0": f0, "error": c.final_error_mm, "lag": c.max_lag_mm, "lost": c.lost,
                         "others": c.others_max_mm, "others_name": c.others_name, "tilt": c.max_tilt_deg})
            print(f"  mu {mu:.2f}  F0 {f0:4.1f} N: error {c.final_error_mm:5.2f} mm, lag {c.max_lag_mm:5.2f} mm, "
                  f"{'LOST' if c.lost else 'held'}, others {c.others_max_mm:.2f} mm, tilt {c.max_tilt_deg:.1f} deg")
    return rows


# ----------------------------------------------------------------------------- reporting

def fmt(x: float, nd=1) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def scenario_table(results: list[ScenarioResult]) -> str:
    head = ("| Scenario | Servo | Carries | Arrived (<=2 mm) | Mean error (mm) | Worst error (mm) | Largest move of another piece (mm) "
            "| Max tilt | Fallen | Lost | Make-room carries | Sim time (s) |")
    rows = [head, "|" + "---|" * 12]
    for r in results:
        s = r.summary()
        rows.append(f"| {s['scenario']} | {'yes' if s['servo'] else 'no'} | {s['carries']} | {s['arrived']}/{s['carries']} | {fmt(s['mean_error'])} | "
                    f"{fmt(s['worst_error'])} ({s['worst_carry']}) | {fmt(s['others_mm'], 2)} ({s['others_name'] or '-'}) | "
                    f"{fmt(s['tilt_deg'])} deg ({s['tilt_name'] or '-'}) | {', '.join(s['fallen']) or 'none'} | {', '.join(s['lost']) or 'none'} | "
                    f"{s['make_room']} | {s['sim_s']:.0f} |")
    return "\n".join(rows)


def sweep_table(rows: list[dict]) -> str:
    out = ["| Carry speed (mm/s) | Acceleration (mm/s^2) | Final error (mm) | Max lag (mm) | Lost | Largest move of another piece (mm) | Carry time (s) |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['speed']} | {r['accel']} | {fmt(r['error'], 2)} | {fmt(r['lag'], 2)} | {'yes, near ' + r['lost_near'] if r['lost'] else 'no'} | "
                   f"{fmt(r['others'], 2)} ({r['others_name'] or '-'}) | {r['duration']:.1f} |")
    return "\n".join(out)


def pull_table(rows: list[dict]) -> str:
    out = ["| Friction mu | Pull at contact F0 (N) | Final error (mm) | Max lag (mm) | Lost | Largest move of another piece (mm) | Max tilt (deg) |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['friction']:.2f} | {r['f0']:.0f} | {fmt(r['error'], 2)} | {fmt(r['lag'], 2)} | {'yes' if r['lost'] else 'no'} | "
                   f"{fmt(r['others'], 2)} ({r['others_name'] or '-'}) | {fmt(r['tilt'])} |")
    return "\n".join(out)


def pieces_table(details: dict, config: dict) -> str:
    cfg = {k: float(v) for k, v in config["piece_sizes_mm"].items()}
    out = ["| Piece | Base from mesh (mm) | Config estimate (mm) | Height (mm) | Pocket in base | Mesh volume (mm^3) | Mass in the model (g) | CoM height (mm) |", "|---|---|---|---|---|---|---|---|"]
    seen = set()
    for body, spec in details["pieces"].items():
        if spec["kind"] in seen:
            continue
        seen.add(spec["kind"])
        info = details["meshes"]["pieces"][spec["kind"]]
        out.append(f"| {board_model.NAMES[spec['kind']]} | {info['base_diameter_mm']:.2f} | {cfg[spec['kind']]:.0f} | {info['height_mm']:.1f} | "
                   f"{info['pocket_diameter_mm']:.1f} x {info['pocket_depth_mm']:.1f} mm | {info['volume_mm3']:.0f} | {spec['mass_g']:.1f} | {spec['com_z_mm']:.1f} |")
    return "\n".join(out)


def carry_details(results: list[ScenarioResult]) -> str:
    out = []
    for r in results:
        out.append(f"\n### {r.title}\n")
        out.append(r.scenario.text + "\n")
        out.append("```")
        for text, note in r.lines:
            out.append(f"{text:<44s} ; {note}")
        out.append("```\n")
        out.append("| # | Carry | Piece | Final error (mm) | Max lag (mm) | Lost | Others moved (mm) | Max tilt (deg) | Time (s) |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for c in r.carries:
            out.append(f"| {c.index + 1} | {c.source}->{c.target} | {c.piece or '(none)'} | {fmt(c.final_error_mm, 2)} | {fmt(c.max_lag_mm, 2)} | "
                       f"{'yes, near ' + c.lost_near if c.lost else 'no'} | {fmt(c.others_max_mm, 2)} ({c.others_name or '-'}) | {fmt(c.max_tilt_deg)} ({c.tilt_name or '-'}) | {c.duration_s:.1f} |")
        if r.video:
            out.append(f"\nVideo: `out/{r.video.name}`, paths: `out/{r.plot.name}`\n")
        elif r.plot:
            out.append(f"\nPaths: `out/{r.plot.name}`\n")
    return "\n".join(out)


def write_results(path: Path, results: list[ScenarioResult], sweep: list[dict], pulls: list[dict], params: Params, config: dict, details: dict, rendered: bool) -> None:
    p = params
    lines = ["# MuJoCo simulation results", "",
             f"Generated by `run_sim.py --all` on {time.strftime('%Y-%m-%d %H:%M')} with MuJoCo {mujoco.__version__}. "
             f"Carry speed {config['carry_speed_mm_s']:.0f} mm/s, travel {config['travel_speed_mm_s']:.0f} mm/s, acceleration {p.accel_mm_s2:.0f} mm/s^2, "
             f"grab {config['grab_settle_s']} s, release {config['release_settle_s']} s, servo settle {config.get('servo_settle_s', 0.3)} s.", "",
             f"Assumed physics (placeholders until measured, see README.md): board top {p.board_mm} mm, magnet gap {p.gap_mm} mm extended / {p.retract_mm} mm retracted, "
             f"friction {p.friction}, pull F0 = {p.f0_n} N at contact with d0 = {p.d0_mm} mm (so {simulate.Magnet(p).normal_pull(p.board_mm + p.gap_mm):.2f} N through the board), "
             f"lateral pull up to {p.lateral_fraction:.0%} of the normal pull at {p.magnet_radius_mm} mm, released beyond {p.release_radius_mm} mm; "
             f"steel disc {p.disc_diameter_mm} x {p.disc_thickness_mm} mm in the base pocket, PLA {p.pla_density_g_cm3} g/cm^3 solid.", "",
             "## The pieces, measured from the STL meshes", "", pieces_table(details, config), "",
             "The config's `piece_sizes_mm` (23 / 25 / 25 / 25 / 27 / 27) are exactly what the meshes say. Every base has a 10.1 mm pocket about 5 mm deep for a magnet or disc.", "",
             "## Scenarios", "", "A carry \"arrives\" when the piece ends within 2 mm of the target centre. \"Lost\" means the piece left the magnet's release radius while carried, or nothing was under the magnet when it switched on.", "",
             scenario_table(results), "",
             "## Knight g1->f3: carry speed x acceleration", "", sweep_table(sweep) if sweep else "(not run)", "",
             "## Knight g1->f3: friction x pull (the two unmeasured numbers)", "", pull_table(pulls) if pulls else "(not run)", "",
             "## Every carry", carry_details(results), "",
             f"Rendering: {'MP4 videos and PNG frames in out/ (MuJoCo offscreen renderer, MUJOCO_GL=' + os.environ.get('MUJOCO_GL', 'default') + ')' if rendered else 'videos were not written (see run output); trajectory PNGs are in out/'}.", ""]
    path.write_text("\n".join(lines))


def print_summary(results: list[ScenarioResult]) -> None:
    print("\n" + scenario_table(results).replace("|---", "|---") + "\n")


# ----------------------------------------------------------------------------- main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", action="append", choices=SCENARIO_ORDER, help="run this scenario (repeatable)")
    parser.add_argument("--all", action="store_true", help="all scenarios with and without the servo, both sweeps, results.md")
    parser.add_argument("--sweep", action="store_true", help="the knight speed x acceleration sweep")
    parser.add_argument("--pull-sweep", action="store_true", help="the knight friction x pull sweep")
    parser.add_argument("--no-servo", action="store_true", help="leave the magnet at the board: no arm up/down steps")
    parser.add_argument("--both", action="store_true", help="run the chosen scenarios with and without the servo")
    parser.add_argument("--permanent-magnet", action="store_true", help="the pull never switches off (N52 option); the arm height is all that changes it")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--results", type=Path, default=None, help="write results.md here (default: with --all, next to this script)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--carry-speed", type=float, help="override carry_speed_mm_s")
    parser.add_argument("--travel-speed", type=float, help="override travel_speed_mm_s")
    for name, f in (("board-mm", "board_mm"), ("gap-mm", "gap_mm"), ("retract-mm", "retract_mm"), ("friction", "friction"), ("f0", "f0_n"),
                     ("d0", "d0_mm"), ("lateral", "lateral_fraction"), ("release", "release_radius_mm"), ("accel", "accel_mm_s2"),
                     ("infill", "infill"), ("disc-recess", "disc_recess_mm"), ("timestep", "timestep_s")):
        parser.add_argument(f"--{name}", type=float, dest=f, help=f"Params.{f}")
    args = parser.parse_args(argv)

    config = board_model.load_config(args.config)
    if args.carry_speed:
        config["carry_speed_mm_s"] = args.carry_speed
    if args.travel_speed:
        config["travel_speed_mm_s"] = args.travel_speed
    overrides = {k: getattr(args, k) for k in Params().as_dict() if getattr(args, k, None) is not None}
    params = Params(**overrides)
    params.servo = not args.no_servo
    params.permanent_magnet = args.permanent_magnet
    render = not args.no_render

    scenarios = args.scenario or (SCENARIO_ORDER if args.all else [])
    results: list[ScenarioResult] = []
    rendered = False
    runs: list[tuple[str, Params, bool]] = []          # scenario, params, render this one
    servo_modes = [True, False] if (args.all or args.both) else [params.servo]
    for servo in servo_modes:
        for name in scenarios:
            runs.append((name, Params(**{**params.as_dict(), "servo": servo}), render and servo))
    if args.all:
        # The design questions the README asks: a lighter print (20 % infill) for the king, and the
        # N52 permanent magnet with the arm down at the assumed 15 mm and at a shallow 4 mm.
        runs.append(("castle", Params(**{**params.as_dict(), "servo": True, "infill": 0.2}), False))
        runs.append(("knight", Params(**{**params.as_dict(), "servo": True, "permanent_magnet": True}), False))
        runs.append(("knight", Params(**{**params.as_dict(), "servo": True, "permanent_magnet": True, "retract_mm": 4.0}), False))
    for name, p, render_this in runs:
        variant = variant_name(p)
        print(f"\n== {name}{' (' + variant + ')' if variant else ''}: {SCENARIO_TEXT[name]}")
        t = time.time()
        r = run_scenario(name, config, p, render_this, args.out, args.seed)
        rendered = rendered or r.video is not None
        s = r.summary()
        print(f"  {s['carries']} carries, {s['arrived']} arrived within {ARRIVE_MM:.0f} mm, mean error {fmt(s['mean_error'], 2)} mm, worst {fmt(s['worst_error'], 2)} mm ({s['worst_carry']}); "
              f"other pieces moved up to {s['others_mm']:.2f} mm ({s['others_name'] or '-'}); max tilt {s['tilt_deg']:.1f} deg; fallen {s['fallen'] or 'none'}; "
              f"lost {s['lost'] or 'none'}; make-room carries {s['make_room']}; {s['sim_s']:.0f} s simulated in {time.time() - t:.0f} s")
        results.append(r)
    sweep = pulls = []
    if args.sweep or args.all:
        print("\n== knight g1->f3: carry speed x acceleration")
        sweep = speed_sweep(config, Params(**{**params.as_dict(), "servo": True}))
    if args.pull_sweep or args.all:
        print("\n== knight g1->f3: friction x pull")
        pulls = pull_sweep(config, Params(**{**params.as_dict(), "servo": True}))
    if results:
        print_summary(results)
    results_path = args.results or (RESULTS if args.all else None)
    if results_path and results:
        _, details = board_model.build_xml(config, params)
        write_results(results_path, results, sweep, pulls, params, config, details, rendered)
        print(f"wrote {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
