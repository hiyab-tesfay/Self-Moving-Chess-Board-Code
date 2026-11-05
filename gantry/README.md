# Gantry: moving the pieces

The board moves pieces with a magnet under the top, carried by an XY gantry built from the
"2D Plotter V2" design in the team Drive (2020 rails, a 2040 beam that carries the magnet,
GT2 belts, V-wheels, two NEMA 17s on DRV8825 drivers). This folder is the software for it,
split in two so each half stays small:

| Where | What it knows | Files |
| --- | --- | --- |
| The board (ESP32, or an Arduino with a CNC shield) | millimetres, steps, the magnet. Nothing about chess. | `firmware/gantry_firmware/` |
| The Raspberry Pi | where the squares are; turns a chess move into millimetre commands | `pi/gantry_driver.py`, `pi/gantry_config.json` |

They talk over USB serial in a small GRBL-style language (`firmware/gantry_firmware/protocol.h`
lists it). Because it is GRBL's language, the Pi side also drives a real GRBL board (the
`grbl.zip` in the Drive folder on an Uno + CNC shield) with no changes beyond `$100=40`.

Nothing here has run on the real frame yet. Everything has run on a pretend board on a laptop
(`firmware/host/`), which uses the real motion code with the pins replaced by counters.

## The numbers

Motor: 1.8° NEMA 17, 200 full steps per turn, 13 N·cm (18.4 oz·in) short body, about 1 A per
phase (the Drive parts list; the Amazon listing was not readable, so confirm the current on the
motor's label). Driver: DRV8825. Belt: GT2, 2 mm pitch, on the **20-tooth pulley** that the
plotter design specifies (`GT2 20 Teeth pulley` in `2D Plotter V2.step`); `PULLEY_TEETH` in
`config.h` if a different one is fitted.

| Quantity | Formula | Value |
| --- | ---: | ---: |
| mm per motor turn | teeth × pitch = 20 × 2 | 40 mm |
| steps per mm at 1/8 microstep | 200 × 8 / 40 | **40** |
| steps per mm at 1/16 | 200 × 16 / 40 | 80 |
| resolution at 1/8 | 1 / 40 | 0.025 mm |
| top speed (`MAX_SPEED_MM_S`) | | 120 mm/s = 180 rpm = 4.8 kHz steps |
| speed with a piece (`carry_speed_mm_s`) | | 60 mm/s |
| acceleration (`MAX_ACCEL_MM_S2`) | | 800 mm/s² |
| pulley pitch radius | 40 mm / 2π | 6.37 mm |
| belt pull at standstill | 0.13 N·m / 6.37 mm | 20 N |
| belt pull available at 180 rpm (about half) | | ~10 N |
| needed: 1 kg beam at 800 mm/s² + wheel friction + dragging a piece | 0.8 + ~2 + ~3 | ~6 N |

So the 13 N·cm motor has roughly a 2× margin at these settings. If a motor ever stalls or
skips, halve `MAX_ACCEL_MM_S2` first; speed matters less than acceleration here. One square
(50 mm) with a piece takes about 0.9 s; a full-board diagonal takes about 3 s; a pawn push
from the moment the job arrives is 3–5 s including the magnet settling.

Why 1/8 microstepping: 4.8 kHz at full speed is comfortable for the step loop on an ESP32
and still fine on an AVR; 1/16 doubles the step rate for no gain the belt can show.

DRV8825 setup: current limit `Vref = I / 2`, so **0.45 V** for a 1 A motor (0.35 V for a
0.7 A one). Microstep jumpers M0 M1 M2: 1/8 = on on off, 1/16 = off off on, 1/32 = on on on.
The 12 V 2 A supply covers both motors plus the electromagnet.

## Wiring

Pins are in `firmware/gantry_firmware/config.h`. The default block is the ESP32 Dev Module
with the same pins as `Prototype_Program/Prototype_Program.ino`:

| Signal | ESP32 pin | Notes |
| --- | --- | --- |
| X DIR / STEP / EN | 18 / 19 / 21 | motor 1 in the prototype |
| Y DIR / STEP / EN | 22 / 25 / 26 | motor 2 in the prototype |
| magnet | 27 | gate of a logic-level MOSFET (or a relay module) switching the 12 V electromagnet; flyback diode across the coil |
| servo (optional) | 32 | MG90 that lowers the permanent magnet; set `PIN_SERVO -1` without one |
| X / Y homing switch (optional) | 34 / 35 | to ground, with an external pull-up (34/35 have none); `-1` without switches |

Define `GANTRY_CNC_SHIELD` for an Uno/Nano on a CNC Shield v3 (X and Y driver sockets,
spindle-enable pin for the magnet, the X/Y endstop headers).

Without homing switches the firmware homes by driving each axis slowly past its end so the
motor stalls against the frame, then calls that spot 0. A stalled stepper is harmless at
25 mm/s; make sure nothing fragile is at the home corner.

## What goes where

- **The ESP32** ("the Arduino") gets `firmware/gantry_firmware/`. That is the only thing
  that needs flashing.
- **The Pi** gets `pi/` and runs `gantry_driver.py`, which talks to the ESP32 over the USB
  cable. Nothing is flashed on the Pi. For a first test the same script runs on a laptop
  with the ESP32 plugged into it (`pip install pyserial`, then `--port /dev/cu.usbserial-…`
  on a Mac, `/dev/ttyUSB0` on the Pi).

## Flashing, and the first power-up

Open `firmware/gantry_firmware/gantry_firmware.ino` in the Arduino IDE (the `.h` files in the
folder come along). Board: **ESP32 Dev Module** (Boards Manager: "esp32" by Espressif; it is
a large download, a few GB of disk). No library is needed unless `PIN_SERVO` is set, in which
case install "ESP32Servo". Upload, then open the serial monitor at **115200** with "Newline"
line endings: it says `[duen-gantry 0.1 ready: $X to unlock, $H to home]`.

The sketch uses only the plain Arduino API (`pinMode`, `digitalWrite`, `micros`, `Serial`),
and the motion and protocol code is the same that runs in the laptop tests. It compiles for
the ESP32 Dev Module (esp32 core 2.0.17: 275 KB of flash, 21 KB of RAM) and for an Arduino
Uno with `GANTRY_CNC_SHIELD` (15 KB of flash, 990 bytes of RAM):

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/gantry_firmware
arduino-cli compile --fqbn arduino:avr:uno --build-property compiler.cpp.extra_flags=-DGANTRY_CNC_SHIELD firmware/gantry_firmware
```

Before the first `$H`, check these with the carriage somewhere in the middle of its travel:

1. **DRV8825 microstep pins.** Unconnected M0/M1/M2 mean full steps, and every move would
   be 8× too long. Tie **M0 and M1 to 3.3 V** (M2 low) for 1/8, or wire them to GPIOs and
   put those in `PIN_M0`…`PIN_M2` so the sketch sets them, or set `MICROSTEPS 1` to match
   full steps. Then `G0 X10` after `$X` must move the carriage **10 mm**: measure it.
2. **Current limit.** `Vref = I / 2` on the DRV8825's trimpot, so 0.45 V for a 1 A motor
   (0.35 V for 0.7 A). Set it with the motor unplugged.
3. **Direction.** `$X`, then `G0 X10`: the carriage must move toward the far end of X, away
   from the corner you want as the origin (the bottom-left corner of sketch 5). If it goes
   the other way, flip `INVERT_X_DIR` in `config.h` and re-upload. Same for `G0 Y10` and
   `INVERT_Y_DIR`.
4. **Homing.** `$H`: each axis drives slowly to its origin end, stalls there for a moment
   (that is normal without switches; it is the DRV8825 skipping, not damage), backs off
   2 mm, and `?` then shows `MPos:0.000,0.000|Homed:1`.
5. **Travel.** `G0 X400`, then `G0 X450`, then smaller steps until the carriage is 3–5 mm
   from the far end; that X is `MAX_X_MM`. Same for Y. Put them in `config.h` and in
   `travel_mm` in `gantry_config.json` if they differ from 470 × 420.
6. **Magnet.** `M3` must pull a piece on the board top; `M5` must let it go. If `M3` does
   nothing, check `PIN_MAGNET` and the MOSFET; if it is on when it should be off, flip
   `MAGNET_ACTIVE_HIGH`.
7. **Stop at any time:** `!` halts the motion immediately; `$X` then `$H` to carry on.

Then from the laptop or the Pi: `python3 gantry_driver.py --port … --calibrate` to find a1
and the square size, and `--move e2e4` with a piece on e2 for the first real carry.

## The Pi side

`pi/gantry_config.json` says where the squares are, in mm from the origin. The origin is the
one the mechanical team recommends: the magnet's centre at the **bottom-left corner of
sketch 5** in the Fusion design, the sketch that traces where the magnet can move. That
rectangle is **470 × 420 mm**, which is the travel and the firmware's soft limits
(`MAX_X_MM`, `MAX_Y_MM`). Files run along +X, ranks along +Y.

- `a1_center_mm`: the magnet's position when it is under the centre of a1.
- `square_mm`: the pitch of the squares.
- `park_columns_mm`: the columns beside the board where captured pieces go, White's on the
  left (`w`), Black's on the right (`b`); `park_rows_mm` lists the slot centres along them.
  In the sketch each side has one lane of twelve circles at a 35 mm pitch, so the file has
  one column per side with twelve rows.
- `travel_mm`: the soft limits, the same as `MAX_X_MM`/`MAX_Y_MM` in the firmware.

The travel and the lane pitch come from the sketch; the square size (50 mm) and the a1
centre (60, 35) are read off a photo of it and are confirmed at the frame with

```sh
python3 gantry_driver.py --calibrate
```

which homes, asks you to steer the magnet under the centre of a1 by typing moves such as
`x+10`, `y-0.5` or `x-100` (millimetres) and `ok`, then the same for h1; a1 to h1 is seven
squares, so that gives the square size, and both numbers are written into the config.
Every point of a job is checked against the travel before anything moves, so a parking
slot that does not fit says so instead of running into the frame.

One mismatch to settle with the website side: its planner parks captured pieces in two
columns of eight per side (slots `w:8`–`w:15` at x = −2), while the sketch has one lane of
twelve per side plus a row along the top. Until the planner's slot layout is made to match,
jobs that use the second column fail cleanly with "column -2 is not configured".

```sh
cd gantry/pi
python3 -m pip install pyserial
python3 gantry_driver.py --home
python3 gantry_driver.py --goto 100 100
python3 gantry_driver.py --move e2e4
echo '{"label":"e2e4","steps":[{"op":"carry","piece":"P","from":"e2","to":"e4","path":[[4,1],[4,3]]}]}' | python3 gantry_driver.py
python3 gantry_driver.py --dry-run --move e2e4    # prints the G-code and the predicted steps, sends nothing
```

The job format on standard input is the one the website's game server produces, from
`backend/board_motion.py` in kennynakao/chess-website: a list of carries in square units,
with lanes at x.5 and parking at x = -1, -2 and 8, 9. To plug the gantry into website board
games, set in `~/.config/duen-chess/relay.env` on the Pi:

```sh
CHESS_MOTION_COMMAND="/home/smart/chess-board-pi-only/.venv/bin/python /home/smart/Self-Moving-Chess-Board-Code/gantry/pi/gantry_driver.py"
```

and restart `duen-relay`. Exit status 0 means the pieces have stopped; anything else stops the
game until someone checks the board. Playing in person on the touchscreen (`pi_game.py`,
`pi_touch.py`) is unchanged and does not use this.

## Path finding, and making room

`pi/board_paths.py` checks every carry in millimetres: pieces are circles `piece_mm` across
and the carried piece must stay `clearance_mm` (3 mm) clear of every other piece along the
whole way. The piece models measure 23 mm (pawn), 25 mm (knight, bishop, rook) and 27 mm
(king, queen) across the base, so `piece_mm` is 27. The search runs over tile centres,
the midpoints between tiles and the crossings between four tiles, plus the parking lanes,
and A* picks the shortest route. With 50 mm squares and 27 mm bases, a lane between two
occupied tiles is 23 mm wide, so a piece cannot pass there; the finder goes round instead,
and an empty parking lane counts as a corridor. When every way is shut, it moves one
blocking piece to a free tile, makes the move, and puts the blocker back: the job then has
three carries, which the firmware and the camera check already handle. The first knight
move of a game is such a case: a pawn steps aside for it.

```sh
python3 gantry_driver.py --dry-run --move g1f3            # from the starting position
python3 gantry_driver.py --dry-run --move a1a8 --occupied a1,a4,b2
```

Jobs from the website carry the paths the game server planned; when a job also lists
`"before"` (the squares and slots occupied as it starts), the Pi finds every path afresh
with these checks. Both `piece_mm` and `clearance_mm` can be set in `gantry_config.json`.

## Testing without hardware

```sh
make -C gantry/firmware/host test          # motion and protocol checks: 56 of them
cd gantry/pi && python3 -m unittest test_board_paths test_gantry_driver
```

The second suite builds `firmware/host/virtual_gantry`, a pretend board that runs the real
`motion.h` and `protocol.h` with a simulated clock, and drives it with the real Pi code
(`--port sim`). It checks that a pawn push grabs on e2, releases on e4, and counts exactly
4000 steps on the Y motor (100 mm × 40 steps/mm); that a capture parks the taken piece first;
that an unreachable slot fails before anything moves; and that the website's stdin/exit-code
contract holds.

## What the design files say

Measured from the files in the team Drive (`Gantry/3D Printing` and `CAD`):

- `2D Plotter V2.step` (the gantry): one `GT2 20 Teeth pulley`, two `NEMA 17 Stepper`, a
  `20x40 V Slot Profile` and a `20x20 V Slot Profile`, Delrin and solid V-wheel kits, four
  idler pulleys, 625 bearings, M5 hardware. The original controller is an Arduino Uno with
  a `CNC shield v1` and A4988 drivers, and the pen lift is an SG90 servo; the team's build
  swaps in DRV8825 drivers and the ESP32, and the servo pin is where the SG90 was. In the
  assembly the X pulley plate sits at x = −339 and the X motor plate at x = +150, the base
  plates at y = −282 and +222, which is consistent with the 470 × 420 mm travel.
- `em holder.stl`: 29 × 31 × 52 mm, so the electromagnet is about 27 mm across and fits
  the 35 mm lane pitch and 50 mm squares with room to spare.
- `camera holder.stl`: 40 × 30 × 20 mm. `67mm rod.stl`: a 3.3 mm rod, 67 mm long.
  `ss bar assembly.stl`: 15 × 258 × 31 mm. `ss base 2 assembly.stl`: 116 × 60 × 66 mm.
- `WOODEN BOX.STEP` (SolidWorks): the enclosure, five panels (top, bottom, two sides, a
  corner); the drawing `WOODEN BOX.png` gives it as 30 in × 30 in with 6 in sides, which is
  the outer rectangle in sketch 5.
- `Chess Board Design.f3d` is sketches only, which is why its STEP export and the web share
  are empty. Open it in Fusion, or save sketch 5 as a DXF, to get the square positions.

## What is still open

- Square size and a1 centre: run `--calibrate` at the frame. The travel (470 × 420) is from
  the sketch; confirm it by homing and jogging to the far corner, and check the parking
  slots the same way.
- The website planner's parking layout (two columns of eight) against the sketch's (one lane
  of twelve per side plus the top row).
- Motor current: read it off the motor's label and set `Vref` (the pulley is confirmed 20 T).
- Direction: if +X or +Y moves the wrong way, flip `INVERT_X_DIR` / `INVERT_Y_DIR`.
- Magnet: the Drive notes weigh an electromagnet against a servo-lifted N52 permanent magnet;
  the firmware supports both (`PIN_MAGNET`, `PIN_SERVO`). Tune `grab_settle_s` once the real
  pull through the board top is known.
- Homing switches would make homing faster and quieter than bump homing; two microswitches
  and `PIN_X_MIN`/`PIN_Y_MIN` are all it takes.
