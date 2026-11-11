#!/usr/bin/env python3
"""Plays many chess games through the gantry's planner and path finder, and measures every carry.

    python3 gantry/sim/path_sim.py                     # 100 random games per variant, a table
    python3 gantry/sim/path_sim.py --games 30 --engine # games from Stockfish (needs stockfish on PATH)
    python3 gantry/sim/path_sim.py --json results.json

Nothing moves: this runs the same code the bench console uses (carry_plan in dock mode, so
captures go into the right-hand lane; board_paths for every path; gantry_driver.route for
making room) on the board in gantry_config.json, then resets the board at the end of each
game. Each variant changes only path-finding settings, on the same games.

What it measures, for every carry:
  * whether a way was found at all (a failure means the person would have to move it);
  * extra carries to make room (a blocker moved aside and back);
  * path length, corners (each is a stop on the gantry), and the time at the console's speeds;
  * how long a piece is held (with an electromagnet the firmware cut it at 15 s and the console
    rested it at 12; this build's permanent magnet has no limit, so it is only a statistic);
  * the leftmost point reached (the magnet sits on the right of the Y carriage);
  * the trailing piece: a carried piece lags the magnet by a few millimetres (MuJoCo: 5-6) and
    cuts inside every corner. A leash model (the piece is pulled along on a string of length
    `--lag`) measures how close it really comes to the other pieces, and how far short of the
    square it stops, with and without a small overshoot at the end.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

import chess

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "pi"))
import board_paths as bp  # noqa: E402
import carry_plan as cp  # noqa: E402
import board_layout as bl  # noqa: E402
import gantry_driver as gd  # noqa: E402

# The console's defaults (console_server.DEFAULTS): real mm and seconds.
TRAVEL, CARRY, ACCEL = 40.0, 25.0, 400.0
STOP_S = 0.15                         # status-poll latency at every point of a carry
OVERHEAD_S = 2 * 0.35 + 0.3 + 0.25    # arm up and down, grab, release
MAGNET_BUDGET_S = 12.0

# Every variant names every path option, so the table does not depend on what gantry_config.json turns on.
OLD = {"docking_lanes": None, "reach_mm": None, "path_smoothing": False, "corner_lag_mm": 0.0, "turn_penalty_mm": 2.0}
RIGHT = dict(OLD, docking_lanes=["b"], reach_mm=[50, 0, 470, 420])
VARIANTS = {
    "both lanes (old)": OLD,
    "right lane only": RIGHT,
    "+ shortcuts": dict(RIGHT, path_smoothing=True),
    "+ corner check": dict(RIGHT, path_smoothing=True, corner_lag_mm=6.0),
    "+ corner cost 10": dict(RIGHT, path_smoothing=True, corner_lag_mm=6.0, turn_penalty_mm=10.0),
    "reach from a-file": dict(RIGHT, reach_mm=[60, 0, 470, 420], path_smoothing=True, corner_lag_mm=6.0),
    "as configured": {},          # gantry_config.json as it is (Sketch 5 of 2026-11-06: both lanes, 9 slots each)
}


def trapezoid_s(d: float, v: float, a: float) -> float:
    if d <= 1e-9:
        return 0.0
    return 2 * math.sqrt(d / a) if d < v * v / a else d / v + v / a


def random_game(rng: random.Random, plies: int, capture_bias: float) -> list[chess.Move]:
    board, moves = chess.Board(), []
    while len(moves) < plies and not board.is_game_over():
        legal = list(board.legal_moves)
        captures = [m for m in legal if board.is_capture(m)]
        move = rng.choice(captures) if captures and rng.random() < capture_bias else rng.choice(legal)
        moves.append(move)
        board.push(move)
    return moves


def engine_game(rng: random.Random, plies: int, engine) -> list[chess.Move]:
    import chess.engine
    board, moves = chess.Board(), []
    while len(moves) < plies and not board.is_game_over():
        # Seeded random moves at the start and now and then, the engine's best (a fixed node count, so the same
        # seed gives the same games) otherwise.
        if len(moves) < 4 or rng.random() < 0.15:
            move = rng.choice(list(board.legal_moves))
        else:
            move = engine.play(board, chess.engine.Limit(nodes=3000)).move
        moves.append(move)
        board.push(move)
    return moves


def leash(points: list[tuple[float, float]], lag: float, step: float = 0.5):
    """The carried piece's track when it trails the magnet on a string of length `lag`."""
    piece = points[0]
    track = [piece]
    for a, b in zip(points, points[1:]):
        d = math.dist(a, b)
        n = max(1, int(d / step))
        for k in range(1, n + 1):
            m = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
            gap = math.dist(piece, m)
            if gap > lag:
                piece = (m[0] + (piece[0] - m[0]) * lag / gap, m[1] + (piece[1] - m[1]) * lag / gap)
                track.append(piece)
    return track


def seg_dist(p, a, b) -> float:
    return bp._dist_point_segment(p, a, b)


class Variant:
    def __init__(self, name: str, overrides: dict, config: dict, lag: float):
        self.name = name
        self.config = dict(config, **overrides)
        self.map = gd.BoardMap(self.config)
        self.geometry = self.map.geometry(self.config)
        self.lag = lag
        self.carries: list[dict] = []
        self.moves = self.failures = self.make_room = self.by_hand = self.no_spare = 0
        self.move_seconds: list[float] = []
        self.plan_ms: list[float] = []
        self.resets: list[dict] = []
        self.examples: list[str] = []
        self.failed: list[str] = []      # every move that failed even with the capture taken off by hand

    # ------------------------------------------------------------------ one job

    def route(self, job: dict, kind: str):
        t0 = time.perf_counter()
        try:
            return gd.route(job, self.map, self.config)
        except gd.GantryError as exc:
            if len(self.examples) < 6:
                self.examples.append(f"{kind} {job['label']}: {exc}")
            return None
        finally:
            self.plan_ms.append((time.perf_counter() - t0) * 1000)

    def run_job(self, job: dict, kind: str, routed=None) -> dict:
        routed = routed if routed is not None else self.route(job, kind)
        if routed is None:
            return {"ok": False}
        world = bp.Board(self.geometry, job["before"], pieces=job.get("pieces"))
        seconds, here, carries = 0.0, None, 0
        for step in routed:
            if step["op"] == "remove":
                world.occupied.discard(step["square"])
                world.pieces.pop(step["square"], None)
                continue
            if step["op"] != "carry":
                continue
            carries += 1
            if step.get("reason") in ("make room", "put back"):
                self.make_room += 1
            pts = [self.map.point_mm(float(px), float(py)) for px, py in step["path"]]
            carried = self.geometry.size(step.get("piece"))
            others = []
            for name in world.occupied:
                if name == step["from"]:
                    continue
                try:
                    others.append((self.geometry.node_mm(world.node(name)), world.size_of(name)))
                except bp.NoPath:
                    continue
            planned = min((min(seg_dist(o, a, b) for a, b in zip(pts, pts[1:])) - (carried + size) / 2
                           for o, size in others), default=99.0)
            track = leash(pts, self.lag)
            trailing = min((min(math.dist(o, p) for p in track) - (carried + size) / 2 for o, size in others), default=99.0)
            short = math.dist(track[-1], pts[-1])
            # Overshoot by the lag along the last leg, then come back: the piece is dragged onto the square.
            (ax, ay), (bx, by) = pts[-2], pts[-1]
            d = math.dist(pts[-2], pts[-1]) or 1.0
            over = (bx + (bx - ax) / d * self.lag, by + (by - ay) / d * self.lag)
            track2 = leash(pts + [over, pts[-1]], self.lag)
            short2 = math.dist(track2[-1], pts[-1])
            length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
            held = sum(trapezoid_s(math.dist(a, b), CARRY, ACCEL) + STOP_S for a, b in zip(pts, pts[1:])) + 0.3
            go = trapezoid_s(math.dist(here, pts[0]), TRAVEL, ACCEL) if here else 0.0
            seconds += go + held + OVERHEAD_S - 0.3
            here = pts[-1]
            self.carries.append({"kind": kind, "reason": step.get("reason", "move"), "length": length,
                                 "corners": len(pts) - 2, "held": held, "planned": planned, "trailing": trailing,
                                 "short": short, "short_overshoot": short2, "min_x": min(x for x, _ in pts),
                                 "slot": ":" in step["to"] or ":" in step["from"]})
            world.apply(bp.Carry(step["from"], step["to"], step["path"], step.get("reason", "move")))
        return {"ok": True, "seconds": seconds, "carries": carries}

    # ------------------------------------------------------------------ one game

    def play(self, moves: list[chess.Move]) -> None:
        board = chess.Board()
        layout = cp.Layout(captures="dock", dock_lanes=tuple(bl.usable_lanes(self.config)), dock_slots=bl.slot_count(self.config))
        for move in moves:
            slot = None
            taken = cp.removal(board, move)
            if taken and layout.free_slots():
                world = bp.Board(self.geometry, set(layout.pieces), pieces=dict(layout.pieces))
                slot = world.best_slot(taken[0], layout.free_slots())
            # What the console does (Console.fallback_preview): the plan as made, then without the docked spare
            # for a promotion, then with the captured piece taken off by the person too.
            for options in cp.FALLBACKS:
                trial = layout.copy()
                job = cp.job_for_move(trial, board, move, dock_slot=None if options else slot, **options)
                routed = self.route(job, "move" + (f" {options}" if options else ""))
                if routed is not None:
                    if options.get("by_hand") and taken:
                        self.by_hand += 1
                    elif options and move.promotion:
                        self.no_spare += 1
                    break
            layout.pieces = trial.pieces
            if routed is None:
                self.failed.append(f"{board.fen()} {move.uci()}: {self.examples[-1] if self.examples else ''}")
            board.push(move)
            self.moves += 1
            result = self.run_job(job, "move", routed) if routed is not None else {"ok": False}
            if result["ok"]:
                self.move_seconds.append(result["seconds"])
            else:
                self.failures += 1
        reset = self.run_job(cp.job_for_reset(layout), "reset")
        if not reset["ok"]:
            self.failed.append(f"reset from {sorted(layout.pieces.items())}")
        self.resets.append(reset)

    # ------------------------------------------------------------------ summary

    def summary(self) -> dict:
        moves = [c for c in self.carries if c["kind"] == "move"]
        every = self.carries

        def pct(values, q):
            values = sorted(values)
            return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0
        resets_ok = [r for r in self.resets if r["ok"]]
        return {
            "variant": self.name,
            "moves": self.moves,
            "failed moves": self.failures,
            "captures taken off by hand": self.by_hand,
            "promotions without the docked spare": self.no_spare,
            "failed resets": len(self.resets) - len(resets_ok),
            "make-room carries per 100 moves": round(100 * sum(1 for c in moves if c["reason"] in ("make room", "put back")) / max(1, self.moves), 1),
            "mean path (mm)": round(statistics.mean(c["length"] for c in moves), 1) if moves else 0,
            "mean corners": round(statistics.mean(c["corners"] for c in moves), 2) if moves else 0,
            "mean move time (s)": round(statistics.mean(self.move_seconds), 1) if self.move_seconds else 0,
            "95% move time (s)": round(pct(self.move_seconds, 0.95), 1),
            "magnet over 12 s (%)": round(100 * sum(1 for c in every if c["held"] > MAGNET_BUDGET_S) / max(1, len(every)), 1),
            "planned gap min (mm)": round(min((c["planned"] for c in every), default=0), 2),
            "trailing piece touches (%)": round(100 * sum(1 for c in every if c["trailing"] < 0) / max(1, len(every)), 1),
            "trailing gap min (mm)": round(min((c["trailing"] for c in every), default=0), 2),
            "stops short (mm)": round(statistics.mean(c["short"] for c in every), 1) if every else 0,
            "short with overshoot (mm)": round(statistics.mean(c["short_overshoot"] for c in every), 1) if every else 0,
            "leftmost point (mm)": round(min((c["min_x"] for c in every), default=0), 1),
            "carries left of 50 mm": sum(1 for c in every if c["min_x"] < 50 - 1e-6),
            "mean reset time (s)": round(statistics.mean(r["seconds"] for r in resets_ok), 0) if resets_ok else 0,
            "plan ms mean / max": f"{statistics.mean(self.plan_ms):.0f} / {max(self.plan_ms):.0f}" if self.plan_ms else "",
            "examples": self.examples,
            "failed": self.failed,
        }


def table(rows: list[dict]) -> str:
    keys = [k for k in rows[0] if k not in ("variant", "examples", "failed")]
    out = ["| | " + " | ".join(r["variant"] for r in rows) + " |", "|---" * (len(rows) + 1) + "|"]
    for key in keys:
        out.append(f"| {key} | " + " | ".join(str(r[key]) for r in rows) + " |")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--plies", type=int, default=80)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--capture-bias", type=float, default=0.5, help="random games: chance to take when a capture exists")
    parser.add_argument("--engine", action="store_true", help="games from Stockfish (3000 nodes a move, 15%% seeded random moves)")
    parser.add_argument("--lag", type=float, default=5.5, help="how far the carried piece trails the magnet (mm)")
    parser.add_argument("--variant", action="append", choices=list(VARIANTS), help="only these (repeatable)")
    parser.add_argument("--config", type=Path, default=gd.DEFAULT_CONFIG)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    config = gd.load_config(args.config)
    rng = random.Random(args.seed)
    if args.engine:
        import chess.engine
        engine = chess.engine.SimpleEngine.popen_uci("stockfish")
        engine.configure({"Threads": 1})
        games = [engine_game(rng, args.plies, engine) for _ in range(args.games)]
        engine.quit()
    else:
        games = [random_game(rng, args.plies, args.capture_bias) for _ in range(args.games)]
    rows = []
    for name in args.variant or list(VARIANTS):
        variant = Variant(name, VARIANTS[name], config, args.lag)
        t0 = time.perf_counter()
        for moves in games:
            variant.play(moves)
        row = variant.summary()
        rows.append(row)
        print(f"{name}: {len(games)} games, {variant.moves} moves, {time.perf_counter() - t0:.0f} s", file=sys.stderr, flush=True)
    print(table(rows))
    for row in rows:
        for example in row["examples"]:
            print(f"- {row['variant']}: {example}")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
