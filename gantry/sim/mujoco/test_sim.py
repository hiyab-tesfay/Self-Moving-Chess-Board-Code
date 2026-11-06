"""Quick checks of the MuJoCo board (under a minute):

  * the model builds from gantry_config.json and the prepared meshes, with 32 pieces;
  * 32 pieces left alone on their squares for 0.5 s move less than 0.5 mm and stay upright;
  * the firmware's CARRY sequence replays in the right order with the config's dwells;
  * one carry e2 -> e4 arrives within 2 mm with a magnet that holds (friction 0.2, lateral pull
    equal to the normal pull), and with the placeholder numbers from the README it is not lost
    and ends within 8 mm (it lags: see results.md for why).

    cd gantry/sim/mujoco && python3 -m unittest test_sim
"""
from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

import model as board_model  # noqa: E402
import simulate  # noqa: E402
import gantry_driver  # noqa: E402


class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = board_model.load_config()

    def test_model_loads_with_32_pieces(self):
        sim = simulate.Sim(self.config)
        self.assertEqual(len(sim.piece_names), 32)
        kinds = [sim.details["pieces"][n]["kind"] for n in sim.piece_names]
        self.assertEqual(sorted(kinds).count("p"), 16)
        self.assertEqual(kinds.count("k"), 2)
        self.assertLessEqual(sim.model.opt.timestep, 0.001)
        # The meshes say what the config says.
        for kind, width in self.config["piece_sizes_mm"].items():
            self.assertAlmostEqual(sim.details["meshes"]["pieces"][kind]["base_diameter_mm"], float(width), delta=0.05)

    def test_pieces_rest_on_their_squares(self):
        sim = simulate.Sim(self.config)
        pieces = gantry_driver.board_paths.pieces_at_start()
        sim.place(pieces, settle_s=0.0)
        before = sim.positions_mm().copy()
        sim.settle(0.5)
        moved = np.hypot(*(sim.positions_mm() - before).T)
        self.assertLess(float(moved.max()), 0.5, f"a piece drifted {moved.max():.3f} mm at rest")
        self.assertLess(float(sim.tilts_deg().max()), 1.0)
        for body, place in sim.where.items():
            x, y = sim.geo.place_mm(place)
            px, py = sim.body_xy_mm(body)
            self.assertLess(math.hypot(px - x, py - y), 0.5, f"{body} is not on {place}")

    def test_carry_sequence_matches_the_firmware(self):
        geo = board_model.Geometry(self.config)
        tl = simulate.Timeline(geo, self.config, board_model.Params())
        tl.add_carry("CARRY g1 f3 5.5,0.5 5.5,1.5")
        kinds = [p.kind for p in tl.phases]
        self.assertEqual(kinds, ["move", "servo", "magnet", "dwell", "move", "move", "move", "magnet", "dwell", "servo"])
        labels = [p.label for p in tl.phases]
        self.assertEqual(labels[0], "travel to g1")
        self.assertEqual(labels[-1], "arm down")
        dwell = [p for p in tl.phases if p.kind == "dwell"]
        self.assertAlmostEqual(dwell[0].t1 - dwell[0].t0, float(self.config["grab_settle_s"]))
        self.assertAlmostEqual(dwell[1].t1 - dwell[1].t0, float(self.config["release_settle_s"]))
        carry_moves = [p for p in tl.phases if p.kind == "move" and p.carrying]
        self.assertEqual(len(carry_moves), 3)
        self.assertEqual(carry_moves[-1].move.x1, geo.square_center("f3")[0])
        # A 50 mm move at 60 mm/s and 800 mm/s^2 takes the trapezoid's 0.908 s, as the README says.
        self.assertAlmostEqual(carry_moves[1].move.duration, 2 * 60 / 800 + (50 - 2 * 60 ** 2 / 1600) / 60, places=3)

    def _carry_e2e4(self, params: board_model.Params) -> simulate.CarryResult:
        sim = simulate.Sim(self.config, params)
        job = gantry_driver.simple_move_job("e2e4")
        sim.place(job["pieces"])
        lines = gantry_driver.plan_carry(job, gantry_driver.BoardMap(self.config), self.config)
        return sim.run_lines([text for text, _ in lines], "e2e4").carries[0]

    def test_pawn_e2_e4_arrives_with_a_magnet_that_holds(self):
        params = board_model.Params(friction=0.2, lateral_fraction=1.0)
        c = self._carry_e2e4(params)
        self.assertEqual(c.piece, "P_e2")
        self.assertFalse(c.lost)
        self.assertLess(c.final_error_mm, 2.0, f"the pawn ended {c.final_error_mm:.2f} mm from e4")
        self.assertLess(c.others_max_mm, 0.5)
        self.assertLess(c.max_tilt_deg, simulate.TILT_WARN_DEG)

    def test_pawn_e2_e4_with_the_placeholder_magnet(self):
        c = self._carry_e2e4(board_model.Params())
        self.assertEqual(c.piece, "P_e2")
        self.assertFalse(c.lost)
        self.assertLess(c.final_error_mm, 8.0)
        self.assertLess(c.others_max_mm, 0.5)


if __name__ == "__main__":
    unittest.main()
