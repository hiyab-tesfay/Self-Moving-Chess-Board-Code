# MuJoCo simulation of the board

A physics model of the DUEN board: the board top, the 32 printed pieces (their real meshes),
the gantry carriage with the magnet holder under the board, and the magnet's pull as applied
forces. It replays the carries that **our own planner** produces (`pi/carry_plan.py`,
`pi/board_paths.py`, `pi/gantry_driver.py`) the way the ESP32 runs them
(`firmware/gantry_firmware/protocol.h`), and reports whether the carried piece arrives,
whether any other piece is knocked, whether anything tips, and whether a piece is lost on the
way. The numbers all come from `pi/gantry_config.json`; the piece sizes come from the STLs.

`results.md` is the output of the last `run_sim.py --all`; the videos and plots are in `out/`
(not committed).

## Running it

```sh
# once: MuJoCo (and trimesh/matplotlib for the mesh preparation and the plots)
python3 -m pip install mujoco trimesh matplotlib opencv-python-headless python-chess numpy

# once: the piece meshes (PawnTopFinal, KnightNoSpikes, BishopNoSpikes, RookNoSpikes,
# QueenNoSpikes, KingTest) and the gantry's em holder, from the team Drive
python3 prepare_meshes.py /path/to/piece/stls --holder "/path/to/3D Printing/em holder.stl"

python3 -m unittest test_sim          # a few seconds: loads, rests, one carry arrives
python3 run_sim.py --scenario knight  # one scenario, with a video in out/
python3 run_sim.py --all              # everything in results.md (a few minutes)
python3 run_sim.py --scenario castle --friction 0.2 --f0 8 --carry-speed 80 --accel 1200
python3 model.py                      # writes out/duen_board.xml for `python3 -m mujoco.viewer`
```

The prepared meshes (`meshes/*.stl`, Z up, base centre at the origin, millimetres) and the
measurements (`meshes/pieces.json`) are committed, so only `mujoco`, `numpy` and
`python-chess` are needed to run the simulation; `opencv` for the MP4s, `matplotlib` for the
path plots. On a Mac the offscreen renderer needs `MUJOCO_GL=cgl` (set by `run_sim.py`) or
`glfw`; both work here.

Scenarios: `knight` (g1->f3 from the start, through the lane between the f and g pawns),
`capture` (1.e4 d5 2.exd5), `castle` (kingside after 1.e4 e5 2.Nf3 Nc6 3.Bc4 Bc5),
`enpassant` (1.e4 Nf6 2.e5 d5 3.exd6), `promotion` (e7e8=Q with a white queen docked in w:0),
`reset` (a full reset from a mid-game position with four pieces in the lanes, split into jobs
of 40 carries as the driver does), `game` (a seeded random game of 40 plies, every carry in
sequence, so errors accumulate the way they would on the real board). `--all` runs each
with and without the servo lift, then the knight carry at carry speeds 40/60/80/100 mm/s x
accelerations 400/800/1200 mm/s^2, then the knight carry at frictions 0.15..0.4 x pulls
3/6/12 N, then three extra rows (the castle with a 20 % infill print, the knight with a
permanent magnet at 15 mm and at 4 mm arm-down height).

## What is modelled

| Thing | How |
| --- | --- |
| Board top | an infinite contact plane at z = 0 (what the pieces slide on) and a 30 x 30 in plate under it as the visible box top; squares and the two docking lanes drawn as thin geoms from the config's `square_mm`, `a1_center_mm`, `park_columns_mm`, `park_rows_mm` |
| Pieces | 32 free bodies. Visual: the real mesh. Collision: a cylinder of the measured base diameter (contacts the board and other pieces) plus the mesh's convex hull (contacts other pieces only, so a crown can hit a crown). Mass and inertia from the mesh volume at PLA density plus a steel disc in the base pocket; the body frame is the centre of the base on the underside, so a body position is the point the planner reasons about |
| Gantry | the magnet holder (`em holder.stl`) and a magnet cylinder on a mocap body under the board; a 2040 beam and two 2020 rails as primitives. The carriage follows the firmware's trapezoid exactly: every move starts and stops (no look-ahead), travel at `travel_speed_mm_s`, carries at `carry_speed_mm_s`, `MAX_ACCEL_MM_S2` from config.h |
| Servo lift | the holder's height ramps between 15 mm and 1 mm below the board underside over 0.15 s inside the firmware's 0.3 s settle; with `--no-servo` the magnet stays at 1 mm and the arm steps are left out, as the firmware does without `PIN_SERVO` |
| CARRY sequence | `Timeline.add_carry` mirrors `protocol.h`: travel to `from`, arm up + settle, magnet on, grab dwell, one move per via point and to `to`, magnet off, release dwell, arm down + settle, and the 500 ms `MAGNET_MIN_OFF_MS` wait between carries when they come fast |
| Magnet | not a MuJoCo object. Each step the piece whose steel disc is nearest the magnet axis (within the release radius) gets a downward pull F_n(d) and a lateral pull toward the axis, applied at the disc with `mj_applyFT`. The extra friction the pull causes comes from the contact itself |
| Contacts | 1 ms steps, elliptic friction cones, `noslip_iterations=3` so a piece under a sub-limit lateral force does not creep (checked: zero drift under 0.3 N with a 0.357 N limit); stiff contacts (`solref 0.002`) so the 1 N pull does not sink the piece |

Not modelled: the belt/motor compliance (the carriage is kinematic), the magnet's field in
any real sense, air, felt or surface texture, and the camera.

## Every assumed number

The pieces are measured, the geometry is the config's; these are the assumptions, in
`model.Params`, each overridable on the command line:

| Parameter | Default | Status |
| --- | ---: | --- |
| `board_mm` board top thickness | 3 mm | **assumed** (the box drawing does not give it) |
| `gap_mm` magnet top to board underside, arm up | 1 mm | **assumed** |
| `retract_mm` the same, arm down | 15 mm | **assumed** |
| `friction` piece on board (and piece on piece) | 0.3 | **assumed** (PLA on wood/laminate; felt or PTFE would be lower) |
| `pla_density_g_cm3`, `infill` | 1.25, 1.0 (solid) | **assumed**; a real 20 % infill print is about a third of the mass, which matters for the king (below) |
| `disc_diameter_mm` x `disc_thickness_mm` steel disc in the base | 10 x 2 mm | **assumed**; the pocket the meshes have is 10.1 mm wide and about 5 mm deep, so it fits |
| `disc_recess_mm` disc bottom above the base underside | 0 | **assumed** flush |
| `f0_n` pull at contact | 6 N | **placeholder** until the real pull through the board top is measured |
| `d0_mm` in F_n(d) = F0 (d0 / (d + d0))^2 | 3 mm | **placeholder** |
| `lateral_fraction` the lateral pull saturates at this x F_n | 0.4 | **placeholder** |
| `magnet_radius_mm` offset where it saturates | 7.5 mm | the magnet's radius (15 mm N52) |
| `release_radius_mm` beyond which the piece is lost | 12 mm | **placeholder** |
| `accel_mm_s2` | 800 | `MAX_ACCEL_MM_S2` in config.h |
| `servo_travel_s` time the MG90 takes to move | 0.15 s | **assumed**, inside the 0.3 s settle |
| `box_in` enclosure top | 30 in | the WOODEN BOX drawing |

With the defaults the pull through the board is F_n(3 + 1 mm) = 6 x (3/7)^2 = **1.10 N** and
the lateral pull can reach 0.44 N. Those two numbers decide everything below.

## What the meshes say

| Piece | Base (mm) | Config said | Height (mm) | Pocket in the base | Mass, solid PLA + disc (g) |
| --- | ---: | ---: | ---: | --- | ---: |
| pawn | 23.01 | 23 | 37.7 | 10.2 x 5.0 mm | 9.0 |
| knight | 25.00 | 25 | 46.0 | 10.1 x 4.75 mm | 9.4 |
| bishop | 25.01 | 25 | 59.9 | 10.1 x 5.0 mm | 16.4 |
| rook | 25.00 | 25 | 42.9 | 10.1 x 5.0 mm | 12.5 |
| queen | 27.00 | 27 | 62.9 | 10.1 x 4.75 mm | 21.1 |
| king | 27.01 | 27 | 75.8 | 10.1 x 5.0 mm | 24.5 |

The config's estimates are exactly right. The pawn and knight bases are 23 / 25 mm for their
bottom 2 mm and then chamfer in; the king and queen keep 27 mm for 7 mm. Every piece is
widest at its base, so the planner's circles are the right shape (the king's crown is 26 mm,
the queen's 24 mm).

## Lane clearances with these diameters

A lane between two tiles is 25 mm from each neighbour's centre, and the planner wants 1 mm of
air there, so the carried piece and the neighbour may add up to 48 mm:

| Carried \ beside | pawn 23 | knight/bishop/rook 25 | queen/king 27 |
| --- | --- | --- | --- |
| pawn 23 | 46: 2 mm air | 48: 1 mm | 50: no |
| knight/bishop/rook 25 | 48: 1 mm | 50: no | 52: no |
| queen/king 27 | 50: no | 52: no | 54: no |

So a knight goes between pawns with the 1 mm the README promises, nothing but a pawn goes
between two minor pieces, and the queen and king never use a lane next to an occupied tile.

The lanes **beside** the board are narrower than that. The docking columns are at 17.5 and
452.5 mm, 42.5 mm from the a and h files (not 50), so the edge lanes x = -0.5 and 7.5 run
only 21.25 mm from the edge squares' centres: not even a pawn (needs 24 mm) can slide along
them past an occupied a- or h-square. A docked piece whose slot exit is level with an
occupied edge square, and whose neighbouring slots are taken, therefore needs that edge
square emptied first. That is exactly where the make-room logic fired in the scenarios:

* promotion: the queen leaving **w:0** past the king on **a1** (king to a2 and back);
* reset: the pawn from **b:2** past **h2** (h2 to h3 and back), the knight from **w:2** past
  **a2**, the knight from **b:0** past **h1**, the bishop from **w:0** past **a1**: 8 of the
  36 carries of the reset were make-room carries, which is 22 % more motion;
* the castle (the rook h1->f1 goes round through the lanes beside g2/h2 and f2/g2 instead of
  past the king on g1) and the random game needed none.

So the make-room logic is needed, and routinely, when pieces come back out of the lanes.
Moving the docking columns to 50 mm from the edge files (x = 10 and 460 mm, inside the
470 mm travel) would make the edge lanes as wide as the inner ones and remove most of those
carries; so would docking pieces away from the slot rows level with the back ranks.

## What the results say about the planner

See `results.md` for the tables. In short, with the placeholder magnet:

* **The paths are fine; the magnet model is the limit.** In every scenario where the carried
  piece stayed with the magnet, no other piece moved more than 0.01 mm, except one case
  below: the 1 mm lane margin holds even though the carried piece trails the magnet by up to
  7 mm, because at a corner the trailing piece swings in behind the new direction within
  about 10 mm of travel, before it is level with the neighbour.
* **Every carried piece ends 5 to 6 mm short of the target.** With Coulomb friction the piece
  slides where the lateral pull equals the friction mu (W + F_n): with mu = 0.3 and
  F_n = 1.10 N that is 0.36 N for a pawn against a 0.44 N saturated pull, a steady lag of
  0.36 / 0.44 x 7.5 = 6 mm, and when the carriage stops nothing pulls the piece the rest of
  the way (the pull at 5 mm is below the static friction). Because the pull adds friction in
  the same proportion, a stronger magnet barely helps: in the friction x pull sweep F0 from
  3 to 12 N moves the error by under a millimetre, while mu = 0.2 gives 3.5 mm, mu = 0.15
  gives 2.5 mm, and mu = 0.4 loses the knight on the first move. The speed sweep is flat
  (5.0 to 6.0 mm at every speed and acceleration, nothing lost, nothing knocked): in this
  friction-bound regime the speed does not matter between 40 and 100 mm/s.
* **The lag is what breaks the lane margin.** In the reset, the pawn brought back to d2
  landed 5.7 mm off its centre, toward the lane y = 2.5 squares; the bishop carried along
  that lane two carries later brushed it and pushed it 3.5 mm. The planner's clearances
  assume centred pieces, so a 5 mm placement error turns a 1 mm lane into a collision.
  Either the error has to come down (below), or the planner should route with the
  positions the camera sees rather than the square centres.
* **The king is lost at its first acceleration** (the castle, the promotion's make-room
  carry, every king move in the random game): at 24.5 g its weight eats the thin margin, the
  offset passes the 7.5 mm peak and the pull falls away. The queen is marginal (lost in the
  random game after the king, carried in the reset). A 20 % infill print (about 6 g of
  plastic) is carried. Once a king is left behind, the next carries go through it: in the
  random game the queen carried d8->e8 hit the king still on e8 and fell over. So the real
  pull and the real friction need measuring before the speeds mean anything; the sweep
  tables say what happens when they are better or worse.
* **Nothing tips while carried.** Even at 1200 mm/s^2 the tipping moment of a dragged king
  (m a h = 0.025 x 1.2 x 0.03 N m) is a quarter of what its 27 mm base resists; the only
  fallen pieces in `results.md` come from carries driven through a king the planner believed
  had moved: the king is knocked over (promotion), or the carried queen is (random game).
* **With the servo and without, the outcomes are the same** (the forces only act while the
  magnet is on). The servo matters with a permanent magnet, which `--permanent-magnet`
  models: the retracted N52 at 15 mm pulls 0.12 N and moves nothing it passes over at
  120 mm/s; even at 4 mm (0.54 N) a bishop it passes under moves 0.01 mm. In the model the
  arm-down height is uncritical; the magnet switching off is not what keeps the other pieces
  still, the friction is.

What would make the pieces arrive, from the model: lower friction (a smooth laminate or a
PTFE pad under each piece; mu = 0.15 takes the error from 5.7 to 2.5 mm), a stronger lateral
gradient than 0.4 F_n at 7.5 mm (a magnet larger than the disc, or a smaller disc), or the
firmware/driver compensating the lag: end every carry with the magnet a few mm past the
target along the last direction, or finish with a short move back toward the piece. Which
is right depends on the measured pull, which is the first thing to do at the frame.

## Reading the results

* *Final error*: distance from the carried piece's base centre to the target centre after
  the release dwell and the arm going down.
* *Max lag*: largest offset between the piece and the magnet axis while carried; losing
  happens past `release_radius_mm`.
* *Largest move of another piece*: the most any piece that was not being carried moved
  during a carry (and, per job, over the whole job).
* *Tilt*: angle between a piece's axis and vertical; over 15 deg is flagged, over 60 deg is
  "fallen".
* *Lost*: the piece left the release radius while the magnet was on, with the nearest
  square, or nothing was under the magnet when it switched on (the piece a previous carry
  should have delivered was not there).

## Files

`model.py` builds the MJCF from the config and `meshes/pieces.json`; `simulate.py` has the
trapezoid, the CARRY timeline, the magnet forces and the measurements; `run_sim.py` the
scenarios, sweeps, videos, plots and `results.md`; `test_sim.py` the quick checks;
`prepare_meshes.py` turns the Drive STLs into `meshes/`.

Two things noticed in the Pi code on the way: `BoardMap.square_mm` numbers docking slots
eight per column (`index // 8`) while `carry_plan` and `board_paths` use twelve per lane, so
`place_mm("w:8")` raises although the firmware and the planner both accept w:8; the
simulation maps slots through `point_mm` instead. And `carry_motions()` likewise goes through
`square_mm` for slots.
