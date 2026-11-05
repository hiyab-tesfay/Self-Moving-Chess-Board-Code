"""Replaying the board's CARRY sequences through the MuJoCo model, with the magnet as applied forces.

What runs here is the Pi's plan: `gantry_driver.plan_carry` gives the CARRY lines (one per
piece carried, via-points from board_paths), and `Timeline` replays each the way protocol.h
runs it on the ESP32: travel to the piece at full speed, servo arm up and settle, magnet on,
grab dwell, each carry move at carry speed with the firmware's trapezoid (every move starts and
stops; there is no look-ahead), magnet off, release dwell, arm down and settle. The magnet is
not a MuJoCo object: `Magnet.apply` works out the pull on the piece whose steel disc is nearest
the magnet axis and applies it at the disc with mj_applyFT, every step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path

import mujoco
import numpy as np

import model as board_model
from model import Params

TILT_WARN_DEG = 15.0
TILT_FALLEN_DEG = 60.0
MAGNET_MIN_OFF_S = 0.5     # MAGNET_MIN_OFF_MS in firmware config.h: M3 waits this long after M5


# ----------------------------------------------------------------------------- the firmware's motion

class Trapezoid:
    """One move from rest to rest at up to `speed` with acceleration `accel`: motion.h's profile."""

    def __init__(self, x0: float, y0: float, x1: float, y1: float, speed: float, accel: float):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.length = math.hypot(x1 - x0, y1 - y0)
        self.ux, self.uy = ((x1 - x0) / self.length, (y1 - y0) / self.length) if self.length else (0.0, 0.0)
        self.accel = accel
        ramp = speed * speed / (2 * accel)
        if 2 * ramp >= self.length:
            self.t_acc = math.sqrt(self.length / accel)
            self.v_peak = accel * self.t_acc
            self.duration = 2 * self.t_acc
        else:
            self.t_acc = speed / accel
            self.v_peak = speed
            self.duration = 2 * self.t_acc + (self.length - 2 * ramp) / speed

    def distance(self, t: float) -> float:
        if t <= 0:
            return 0.0
        if t >= self.duration:
            return self.length
        a, ta, v = self.accel, self.t_acc, self.v_peak
        if t < ta:
            return 0.5 * a * t * t
        if t < self.duration - ta:
            return 0.5 * a * ta * ta + v * (t - ta)
        tr = self.duration - t
        return self.length - 0.5 * a * tr * tr

    def position(self, t: float) -> tuple[float, float]:
        s = self.distance(t)
        return self.x0 + self.ux * s, self.y0 + self.uy * s


@dataclass
class Phase:
    kind: str            # "move", "servo", "dwell", "magnet"
    t0: float
    t1: float
    carry: int           # index of the CARRY line this belongs to (-1: none)
    label: str = ""
    move: Trapezoid | None = None
    up: bool = False     # servo: arm up; magnet: on
    carrying: bool = False   # a move with the magnet on


class Timeline:
    """The carriage's x, y, arm height and magnet state against time, for a list of CARRY lines."""

    def __init__(self, geometry: board_model.Geometry, config: dict, params: Params, start=(0.0, 0.0)):
        self.geo, self.config, self.params = geometry, config, params
        self.phases: list[Phase] = []
        self.start_xy = (float(start[0]), float(start[1]))
        self.x, self.y = self.start_xy
        self.t = 0.0
        self.travel_speed = float(config["travel_speed_mm_s"])
        self.carry_speed = float(config["carry_speed_mm_s"])
        self.grab_s = float(config["grab_settle_s"])
        self.release_s = float(config["release_settle_s"])
        self.servo_settle_s = float(config.get("servo_settle_s", 0.3))
        self.last_magnet_off = -1e9
        self.carries: list[dict] = []

    def _add(self, phase: Phase) -> None:
        self.phases.append(phase)
        self.t = phase.t1

    def move(self, x: float, y: float, speed: float, carry: int, carrying: bool, label: str) -> None:
        trap = Trapezoid(self.x, self.y, x, y, speed, self.params.accel_mm_s2)
        self._add(Phase("move", self.t, self.t + trap.duration, carry, label, trap, carrying=carrying))
        self.x, self.y = x, y

    def dwell(self, seconds: float, carry: int, label: str) -> None:
        self._add(Phase("dwell", self.t, self.t + seconds, carry, label))

    def servo(self, up: bool, carry: int) -> None:
        if not self.params.servo:
            return
        # STEP_SERVO then a settle dwell in the firmware; the arm itself takes servo_travel_s of that.
        self._add(Phase("servo", self.t, self.t + self.servo_settle_s, carry, "arm up" if up else "arm down", up=up))

    def magnet(self, on: bool, carry: int) -> None:
        if on and self.t - self.last_magnet_off < MAGNET_MIN_OFF_S:
            self.dwell(MAGNET_MIN_OFF_S - (self.t - self.last_magnet_off), carry, "magnet cooling")
        self._add(Phase("magnet", self.t, self.t, carry, "magnet on" if on else "magnet off", up=on))
        if not on:
            self.last_magnet_off = self.t

    def add_carry(self, line: str) -> dict:
        """One `CARRY from to [via ...]` line, sequenced as protocol.h's carry()/push_pickup()/push_release()."""
        places = line.split()[1:]
        if len(places) < 2:
            raise ValueError(f"not a carry: {line!r}")
        src, dst, vias = places[0], places[1], places[2:]
        index = len(self.carries)
        info = {"line": line, "from": src, "to": dst, "vias": vias, "t_start": self.t, "index": index,
                "src_mm": self.geo.place_mm(src), "dst_mm": self.geo.place_mm(dst),
                "path_mm": [self.geo.place_mm(p) for p in [src, *vias, dst]]}
        sx, sy = info["src_mm"]
        self.move(sx, sy, self.travel_speed, index, False, f"travel to {src}")
        self.servo(True, index)
        self.magnet(True, index)
        info["t_grab"] = self.t
        self.dwell(self.grab_s, index, "grab")
        for place in [*vias, dst]:
            px, py = self.geo.place_mm(place)
            self.move(px, py, self.carry_speed, index, True, f"carry to {place}")
        self.magnet(False, index)
        info["t_release"] = self.t
        self.dwell(self.release_s, index, "release")
        self.servo(False, index)
        info["t_end"] = self.t
        self.carries.append(info)
        return info

    def finish(self, settle_s: float = 0.5) -> None:
        self.dwell(settle_s, -1, "settle")

    @property
    def duration(self) -> float:
        return self.t

    def state(self, t: float, cursor: list[int]) -> tuple[float, float, float, bool, Phase | None]:
        """Carriage x, y (mm), arm height fraction (0 retracted .. 1 extended), magnet on, and the phase at time t.

        `cursor` is a one-element list holding the index of the current phase; t must not go backwards.
        Phases that have ended (a magnet switch has no duration) are skipped, and their effect is
        in the cached state at the start of the next phase.
        """
        i = cursor[0]
        while i < len(self.phases) and self.phases[i].t1 <= t:
            i += 1
        cursor[0] = i
        x, y, arm, mag = self.start_state(i)
        phase = self.phases[i] if i < len(self.phases) else None
        if phase is not None:
            if phase.kind == "move":
                x, y = phase.move.position(t - phase.t0)
            elif phase.kind == "servo":
                frac = min(1.0, max(0.0, (t - phase.t0) / max(self.params.servo_travel_s, 1e-6)))
                arm = frac if phase.up else 1.0 - frac
        return x, y, arm, mag, phase

    def start_state(self, i: int) -> tuple[float, float, float, bool]:
        """x, y, arm, magnet at the start of phase i (cached; built once after the phases are complete)."""
        if not hasattr(self, "_starts"):
            starts = []
            x, y = self.start_xy
            arm, mag = (0.0 if self.params.servo else 1.0), False
            for p in self.phases:
                starts.append((x, y, arm, mag))
                if p.kind == "move":
                    x, y = p.move.x1, p.move.y1
                elif p.kind == "servo":
                    arm = 1.0 if p.up else 0.0
                elif p.kind == "magnet":
                    mag = p.up
            starts.append((x, y, arm, mag))
            self._starts = starts
        return self._starts[min(i, len(self._starts) - 1)]


# ----------------------------------------------------------------------------- the magnet

class Magnet:
    """The pull of the magnet on the nearest steel disc, as placeholder formulas (see README)."""

    def __init__(self, params: Params):
        self.p = params

    def normal_pull(self, d_mm: float) -> float:
        p = self.p
        return p.f0_n * (p.d0_mm / (max(d_mm, 0.0) + p.d0_mm)) ** 2

    def radial_weight(self, r_mm: float) -> float:
        """How much of the normal pull a disc r_mm off the axis still feels: full inside the magnet, none past release."""
        p = self.p
        if r_mm <= p.magnet_radius_mm:
            return 1.0
        if r_mm >= p.release_radius_mm:
            return 0.0
        return (p.release_radius_mm - r_mm) / (p.release_radius_mm - p.magnet_radius_mm)

    def lateral_shape(self, r_mm: float) -> float:
        """Grows linearly to 1 at the magnet radius, falls to 0 at the release radius."""
        p = self.p
        if r_mm <= p.magnet_radius_mm:
            return r_mm / p.magnet_radius_mm
        if r_mm >= p.release_radius_mm:
            return 0.0
        return (p.release_radius_mm - r_mm) / (p.release_radius_mm - p.magnet_radius_mm)

    def forces(self, r_mm: float, d_mm: float) -> tuple[float, float]:
        """(normal pull toward the magnet, lateral pull toward the axis), newtons."""
        fn0 = self.normal_pull(d_mm)
        return fn0 * self.radial_weight(r_mm), self.p.lateral_fraction * fn0 * self.lateral_shape(r_mm)


# ----------------------------------------------------------------------------- results

@dataclass
class CarryResult:
    index: int
    line: str
    piece: str                   # the body carried ("N_g1"), or "" when nothing was under the magnet
    source: str
    target: str
    target_mm: tuple[float, float]
    pick_offset_mm: float = 0.0  # how far the piece sat from the magnet axis when the magnet switched on
    final_error_mm: float = float("nan")
    final_mm: tuple[float, float] = (0.0, 0.0)
    max_lag_mm: float = 0.0      # largest offset from the axis while carried
    lost: bool = False
    lost_mm: tuple[float, float] | None = None
    lost_near: str = ""
    lost_phase: str = ""
    others_max_mm: float = 0.0   # largest move of any other piece during this carry
    others_name: str = ""
    max_tilt_deg: float = 0.0
    tilt_name: str = ""
    fallen: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def arrived(self, tolerance_mm: float = 2.0) -> bool:
        return bool(self.piece) and not self.lost and self.final_error_mm <= tolerance_mm


@dataclass
class JobResult:
    label: str
    carries: list[CarryResult]
    others_max_mm: float = 0.0   # largest drift of any piece that was not carried, over the whole job
    others_name: str = ""
    sim_seconds: float = 0.0
    wall_seconds: float = 0.0
    trace: list = field(default_factory=list)   # (t, carriage x, carriage y, carry index, held x, held y) every 10 ms
    planned: list = field(default_factory=list) # per carry: the via points in mm, from the CARRY line
    start_positions: dict = field(default_factory=dict)   # body -> (x, y) mm as the job began

    @property
    def lost(self) -> list[CarryResult]:
        return [c for c in self.carries if c.lost or not c.piece]

    @property
    def worst_error(self) -> CarryResult | None:
        ok = [c for c in self.carries if c.piece and not c.lost]
        return max(ok, key=lambda c: c.final_error_mm) if ok else None

    @property
    def max_tilt(self) -> tuple[float, str]:
        best = max(self.carries, key=lambda c: c.max_tilt_deg, default=None)
        return (best.max_tilt_deg, best.tilt_name) if best else (0.0, "")

    @property
    def fallen(self) -> list[str]:
        return sorted({name for c in self.carries for name in c.fallen})


# ----------------------------------------------------------------------------- the simulation

class Sim:
    def __init__(self, config: dict, params: Params | None = None, mesh_dir: Path = board_model.MESH_DIR):
        self.config = config
        self.params = params or Params()
        self.geo = board_model.Geometry(config)
        self.xml, self.details = board_model.build_xml(config, self.params, mesh_dir)
        self.model = mujoco.MjModel.from_xml_string(self.xml)
        self.data = mujoco.MjData(self.model)
        self.magnet = Magnet(self.params)
        self.piece_names = list(self.details["pieces"])
        self.piece_ids = np.array([mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, n) for n in self.piece_names])
        self.qpos_addr = {n: self.model.jnt_qposadr[self.model.body_jntadr[i]] for n, i in zip(self.piece_names, self.piece_ids)}
        self.magnet_mocap = self.model.body_mocapid[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "magnet")]
        self.beam_mocap = self.model.body_mocapid[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "beam")]
        self.z_ext = self.details["z_extended_mm"]
        self.z_ret = self.details["z_retracted_mm"]
        self.where: dict[str, str] = {}      # body -> place it was put on (bookkeeping only)
        self.time = 0.0
        self.carriage = (0.0, 0.0)
        self.frame_hook = None               # callable(sim, t) for the recorder
        self.frame_dt = 0.05
        self._next_frame = 0.0

    # ------------------------------------------------------------------ placing pieces

    def symbol_of(self, body: str) -> str:
        return self.details["pieces"][body]["symbol"]

    def place(self, pieces: dict[str, str], settle_s: float = 0.5) -> dict[str, str]:
        """Put a piece of each symbol on every square/slot of `pieces` (place -> symbol); the rest go on the spares shelf.

        Returns place -> body. A piece that is at home keeps its own body; the others take any body of the symbol.
        """
        mujoco.mj_resetData(self.model, self.data)
        free = {n for n in self.piece_names}
        assignment: dict[str, str] = {}
        for place, symbol in pieces.items():
            home = f"{symbol}_{place}"
            if home in free:
                assignment[place] = home
                free.discard(home)
        for place, symbol in pieces.items():
            if place in assignment:
                continue
            candidates = sorted(n for n in free if self.symbol_of(n) == symbol)
            if not candidates:
                raise ValueError(f"no spare {symbol!r} body for {place} (the set has 32 pieces)")
            assignment[place] = candidates[0]
            free.discard(candidates[0])
        for place, body in assignment.items():
            x, y = self.geo.place_mm(place)
            self._set_pose(body, x, y)
        shelf_y = -250.0
        for k, body in enumerate(sorted(free)):
            self._set_pose(body, 40.0 * k, shelf_y)
        self.where = {body: place for place, body in assignment.items()}
        self.data.mocap_pos[self.magnet_mocap] = [0.0, 0.0, (self.z_ext if not self.params.servo else self.z_ret) * 1e-3]
        self.data.mocap_pos[self.beam_mocap][0] = 0.0
        self.carriage = (0.0, 0.0)
        mujoco.mj_forward(self.model, self.data)
        self.settle(settle_s)
        return assignment

    def _set_pose(self, body: str, x_mm: float, y_mm: float) -> None:
        a = self.qpos_addr[body]
        white = self.symbol_of(body).isupper()
        self.data.qpos[a:a + 7] = [x_mm * 1e-3, y_mm * 1e-3, 0.0, 1.0 if white else 0.0, 0.0, 0.0, 0.0 if white else 1.0]
        v = self.model.jnt_dofadr[self.model.body_jntadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, body)]]
        self.data.qvel[v:v + 6] = 0.0

    def settle(self, seconds: float) -> None:
        steps = int(round(seconds / self.model.opt.timestep))
        for _ in range(steps):
            self.data.qfrc_applied[:] = 0.0
            mujoco.mj_step(self.model, self.data)
        self.time += seconds

    # ------------------------------------------------------------------ reading the state

    def positions_mm(self) -> np.ndarray:
        return self.data.xpos[self.piece_ids, :2] * 1e3

    def tilts_deg(self) -> np.ndarray:
        zz = self.data.xmat[self.piece_ids].reshape(-1, 3, 3)[:, 2, 2]
        return np.degrees(np.arccos(np.clip(zz, -1.0, 1.0)))

    def body_xy_mm(self, body: str) -> tuple[float, float]:
        i = self.piece_names.index(body)
        return tuple(self.positions_mm()[i])

    def describe(self, body: str) -> str:
        spec = self.details["pieces"][body]
        colour = "white" if spec["symbol"].isupper() else "black"
        x, y = self.body_xy_mm(body)
        return f"{colour} {board_model.NAMES[spec['kind']]} ({body}, near {self.geo.nearest_place(x, y)})"

    # ------------------------------------------------------------------ running a job

    def run_lines(self, lines: list[str], label: str = "") -> JobResult:
        """Replay CARRY lines (anything else is skipped) and measure what the pieces did."""
        import time as clock
        wall = clock.time()
        tl = Timeline(self.geo, self.config, self.params, start=self.carriage)
        carry_lines = [ln for ln in lines if ln.startswith("CARRY")]
        for ln in carry_lines:
            tl.add_carry(ln)
        tl.finish(0.5)
        results = [CarryResult(c["index"], c["line"], "", c["from"], c["to"], c["dst_mm"]) for c in tl.carries]
        for c, res in zip(tl.carries, results):
            res.duration_s = c["t_end"] - c["t_start"]
        job_start = self.positions_mm().copy()
        carried_bodies: set[int] = set()
        cursor = [0]
        dt = self.model.opt.timestep
        n_steps = int(math.ceil(tl.duration / dt))
        held: int | None = None          # index into piece_ids of the piece the magnet holds
        current = -1                     # carry index in progress
        measured = 0                     # carries whose outcome has been measured
        expected = -1                    # index of the piece the current carry is for
        carry_start_pos = job_start.copy()
        trace: list = []
        t_local = 0.0
        for step in range(n_steps):
            x, y, arm, mag, phase = tl.state(t_local, cursor)
            carry = phase.carry if phase is not None else -1
            if carry >= 0 and carry != current:
                current = carry
                carry_start_pos = self.positions_mm().copy()
                held = None
                # The piece this carry is for: whatever sits nearest its source. It is not an "other"
                # piece even before the magnet switches on (a permanent magnet pulls it as it arrives).
                sx, sy = tl.carries[current]["src_mm"]
                d_src = np.hypot(carry_start_pos[:, 0] - sx, carry_start_pos[:, 1] - sy)
                expected = int(np.argmin(d_src)) if d_src.min() <= self.params.release_radius_mm else -1
            z_top = self.z_ret + (self.z_ext - self.z_ret) * arm       # mm
            self.data.mocap_pos[self.magnet_mocap] = [x * 1e-3, y * 1e-3, z_top * 1e-3]
            self.data.mocap_pos[self.beam_mocap][0] = x * 1e-3
            self.data.qfrc_applied[:] = 0.0
            res = results[current] if current >= 0 else None
            # The pull acts while the firmware has the magnet on; a permanent magnet pulls all the time,
            # weaker with the arm down. `mag` (the firmware's M3..M5 window) decides what counts as a carry.
            if mag or self.params.permanent_magnet:
                pos = self.data.xpos[self.piece_ids]
                r = np.hypot(pos[:, 0] * 1e3 - x, pos[:, 1] * 1e3 - y)
                nearest = int(np.argmin(r))
                if held is None and r[nearest] <= self.params.release_radius_mm:
                    held = nearest
                if held is not None and mag and res is not None and not res.piece:
                    res.piece = self.piece_names[held]
                    res.pick_offset_mm = float(r[held])
                    carried_bodies.add(held)
                if held is not None:
                    rh = float(r[held])
                    if rh > self.params.release_radius_mm:
                        if mag and res is not None and res.piece == self.piece_names[held] and not res.lost:
                            res.lost = True
                            res.lost_mm = (float(pos[held, 0] * 1e3), float(pos[held, 1] * 1e3))
                            res.lost_near = self.geo.nearest_place(*res.lost_mm)
                            res.lost_phase = phase.label if phase is not None else ""
                        held = None
                    else:
                        if mag and res is not None and phase is not None and phase.kind == "move":
                            res.max_lag_mm = max(res.max_lag_mm, rh)
                        self._pull(held, x, y, z_top, rh)
            else:
                held = None
            mujoco.mj_step(self.model, self.data)
            t_local += dt
            if step % 10 == 0:                        # the other pieces, the tilts and the trace, every 10 ms
                if current >= 0:
                    self._watch(results[current], carry_start_pos, expected)
                hx, hy = (float("nan"), float("nan"))
                if held is not None:
                    hx, hy = (float(v) for v in self.data.xpos[self.piece_ids[held], :2] * 1e3)
                trace.append((self.time + t_local, x, y, current, hx, hy))
            while measured < len(tl.carries) and t_local >= tl.carries[measured]["t_end"]:
                self._measure(results[measured])
                measured += 1
            if self.frame_hook is not None and self.time + t_local >= self._next_frame:
                self.carriage = (x, y)
                self.frame_hook(self, self.time + t_local, phase, tl)
                self._next_frame = self.time + t_local + self.frame_dt
        while measured < len(tl.carries):
            self._measure(results[measured])
            measured += 1
        self.time += tl.duration
        self.carriage = (tl.x, tl.y)
        job = JobResult(label, results, sim_seconds=tl.duration, wall_seconds=clock.time() - wall, trace=trace,
                        planned=[c["path_mm"] for c in tl.carries],
                        start_positions={n: (float(p[0]), float(p[1])) for n, p in zip(self.piece_names, job_start)})
        final = self.positions_mm()
        others = [i for i in range(len(self.piece_names)) if i not in carried_bodies]
        if others:
            drift = np.hypot(*(final[others] - job_start[others]).T)
            k = int(np.argmax(drift))
            job.others_max_mm, job.others_name = float(drift[k]), self.piece_names[others[k]]
        return job

    def _measure(self, res: CarryResult) -> None:
        """Where the carried piece is now that its carry is over (release dwell and arm down have passed)."""
        final = self.positions_mm()
        if res.piece:
            i = self.piece_names.index(res.piece)
            res.final_mm = (float(final[i, 0]), float(final[i, 1]))
            res.final_error_mm = float(math.hypot(final[i, 0] - res.target_mm[0], final[i, 1] - res.target_mm[1]))
            self.where[res.piece] = res.target
        tilts = self.tilts_deg()
        res.fallen = sorted(self.piece_names[i] for i in np.where(tilts > TILT_FALLEN_DEG)[0])

    def _pull(self, held: int, x_mm: float, y_mm: float, z_top_mm: float, r_mm: float) -> None:
        body = int(self.piece_ids[held])
        pos = self.data.xpos[body]
        rot = self.data.xmat[body].reshape(3, 3)
        p = self.params
        disc_bottom = pos + rot @ np.array([0.0, 0.0, p.disc_recess_mm * 1e-3])
        disc_centre = pos + rot @ np.array([0.0, 0.0, (p.disc_recess_mm + p.disc_thickness_mm / 2) * 1e-3])
        d_mm = max(disc_bottom[2] * 1e3, 0.0) - z_top_mm       # clamp: contact penetration is not a shorter gap
        fn, fl = self.magnet.forces(r_mm, d_mm)
        if r_mm > 1e-9:
            ux, uy = (x_mm - pos[0] * 1e3) / r_mm, (y_mm - pos[1] * 1e3) / r_mm
        else:
            ux, uy = 0.0, 0.0
        force = np.array([fl * ux, fl * uy, -fn])
        mujoco.mj_applyFT(self.model, self.data, force, np.zeros(3), disc_centre, body, self.data.qfrc_applied)

    def _watch(self, res: CarryResult, start_pos: np.ndarray, expected: int = -1) -> None:
        pos = self.positions_mm()
        moved = np.hypot(*(pos - start_pos).T)
        if res.piece:
            moved[self.piece_names.index(res.piece)] = 0.0
        if expected >= 0:
            moved[expected] = 0.0
        k = int(np.argmax(moved))
        if moved[k] > res.others_max_mm:
            res.others_max_mm, res.others_name = float(moved[k]), self.piece_names[k]
        tilts = self.tilts_deg()
        j = int(np.argmax(tilts))
        if tilts[j] > res.max_tilt_deg:
            res.max_tilt_deg, res.tilt_name = float(tilts[j]), self.piece_names[j]
