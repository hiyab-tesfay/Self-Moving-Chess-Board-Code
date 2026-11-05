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
motor's label). Driver: DRV8825. Belt: GT2, 2 mm pitch, 20-tooth pulley (count the teeth on the
pulley set that arrived; `PULLEY_TEETH` in `config.h` if it differs).

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

## Flashing

Open `firmware/gantry_firmware/gantry_firmware.ino` in the Arduino IDE (the `.h` files in the
folder come with it). ESP32: board "ESP32 Dev Module", library "ESP32Servo" if a servo is used.
Serial monitor at 115200: it says `[duen-gantry 0.1 ready: $X to unlock, $H to home]`.
Then try `$H`, `?`, `G0 X50 Y50`, `M3`, `M5`.

## The Pi side

`pi/gantry_config.json` says where the squares are, in mm from the homed corner:

- `a1_center_mm`: the magnet's position when it is under the centre of a1.
- `square_mm`: the pitch of the squares.
- `park_columns_mm`: the two columns beside the board where captured pieces go, White's on
  the left (`w`), Black's on the right (`b`), eight rows each at the board's row pitch.
- `travel_mm`: the soft limits, the same as `MAX_X_MM`/`MAX_Y_MM` in the firmware.

To measure them: `--home`, then `--goto X Y` until the magnet sits under the centre of a1 and
write that down; a1 to b1 gives the square size. Every point of a job is checked against the
travel before anything moves, so a parking slot that does not fit says so instead of running
into the frame. The default numbers in the file are placeholders: with a 450 mm travel and
50 mm squares, Black's parking columns at 507 and 535 mm are out of reach, on purpose, until
the real travel is measured.

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

## Testing without hardware

```sh
make -C gantry/firmware/host test          # motion and protocol checks: 56 of them
cd gantry/pi && python3 -m unittest test_gantry_driver
```

The second suite builds `firmware/host/virtual_gantry`, a pretend board that runs the real
`motion.h` and `protocol.h` with a simulated clock, and drives it with the real Pi code
(`--port sim`). It checks that a pawn push grabs on e2, releases on e4, and counts exactly
4000 steps on the Y motor (100 mm × 40 steps/mm); that a capture parks the taken piece first;
that an unreachable slot fails before anything moves; and that the website's stdin/exit-code
contract holds.

## What is still open

- Real travel, square size and the parking columns: measure at the frame and put them in
  `gantry_config.json` and `config.h`.
- Pulley tooth count and motor current: read off the parts, adjust `config.h` and `Vref`.
- Direction: if +X or +Y moves the wrong way, flip `INVERT_X_DIR` / `INVERT_Y_DIR`.
- Magnet: the Drive notes weigh an electromagnet against a servo-lifted N52 permanent magnet;
  the firmware supports both (`PIN_MAGNET`, `PIN_SERVO`). Tune `grab_settle_s` once the real
  pull through the board top is known.
- Homing switches would make homing faster and quieter than bump homing; two microswitches
  and `PIN_X_MIN`/`PIN_Y_MIN` are all it takes.
