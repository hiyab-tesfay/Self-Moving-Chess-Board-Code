# Gantry: moving the pieces

The board moves pieces with an electromagnet under the top, carried by an XY gantry built from
the "2D Plotter V2" design in the team Drive (2020 rails, a 2040 beam that carries the magnet,
GT2 belts, V-wheels, two NEMA 17s on DRV8825 drivers) and an MG90 servo arm that lifts the
magnet to the board for a carry and drops it clear afterwards. This folder is the software for
it, split in two so each half stays small:

| Where | What it knows | Files |
| --- | --- | --- |
| The board (ESP32 Dev Module; also an Arduino Mega on a CNC shield) | millimetres, steps, the magnet, the servo arm, and where the squares are, so `CARRY e2 e4` is one command. Nothing about chess. | `firmware/gantry_firmware/` |
| The Raspberry Pi | which piece is where, and the way round the others with their real widths; turns a chess move into `CARRY` commands and watches them finish | `pi/gantry_driver.py`, `pi/carry_plan.py`, `pi/board_paths.py`, `pi/gantry_config.json` |

They talk over USB serial in a small GRBL-style language (`firmware/gantry_firmware/protocol.h`
lists it). The millimetre half of that language is GRBL's own, so the Pi side also drives a real
GRBL board (the `grbl.zip` in the Drive folder on an Uno + CNC shield) with `--gcode` and
`$100=40`; it then plans every move itself and there is no servo.

Nothing here has run on the real frame yet. Everything has run on a pretend board on a laptop
(`firmware/host/`), which uses the real motion and protocol code with the pins replaced by
counters, driven by the real Pi code.

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
| speed with a piece (`CARRY_SPEED_MM_S`) | | 60 mm/s |
| acceleration (`MAX_ACCEL_MM_S2`) | | 800 mm/s² |
| pulley pitch radius | 40 mm / 2π | 6.37 mm |
| belt pull at standstill | 0.13 N·m / 6.37 mm | 20 N |
| belt pull available at 180 rpm (about half) | | ~10 N |
| needed: 1 kg beam at 800 mm/s² + wheel friction + dragging a piece | 0.8 + ~2 + ~3 | ~6 N |

So the 13 N·cm motor has roughly a 2× margin at these settings. If a motor ever stalls or
skips, halve `MAX_ACCEL_MM_S2` first; speed matters less than acceleration here. One square
(50 mm) with a piece takes about 0.9 s; a full-board diagonal takes about 3 s; a pawn push
from the moment the job arrives is about 5 s including the arm and the magnet settling, a
knight through the lanes about 6.5 s, a capture (dock the taken piece, then move) about 15 s.

Why 1/8 microstepping: 4.8 kHz at full speed is comfortable for the step loop on an ESP32
and still fine on an AVR; 1/16 doubles the step rate for no gain the belt can show.

DRV8825 setup: current limit `Vref = I / 2`, so **0.45 V** for a 0.9 A setting (0.35 V for
0.7 A); measure the motor's rated current off its label first. Microstep jumpers M0 M1 M2:
1/8 = on on off, 1/16 = off off on, 1/32 = on on on. The 12 V 2 A supply covers both motors
plus the electromagnet; the servo takes 5 V (see Servo).

## Wiring

Pins are in `firmware/gantry_firmware/config.h`. The default block is the ESP32 Dev Module
with the same motor pins as `Prototype_Program/Prototype_Program.ino`:

| Signal | ESP32 pin | Notes |
| --- | --- | --- |
| X DIR / STEP / EN | 18 / 19 / 21 | motor 1 in the prototype |
| Y DIR / STEP / EN | 22 / 25 / 26 | motor 2 in the prototype |
| magnet | 27 | gate of a logic-level MOSFET (or a relay module) switching the 12 V electromagnet; flyback diode across the coil |
| servo signal | 13 | the MG90's signal wire; its power comes from 5 V, not the ESP32. The prototype sketch has no servo pin, so 13 is a free choice (not a strapping pin, LEDC-capable); set `PIN_SERVO -1` without a servo |
| X / Y homing switch (optional) | 34 / 35 | to ground, with an external pull-up (34/35 have none); `-1` without switches |

Define `GANTRY_CNC_SHIELD` for a CNC Shield v3 (X and Y driver sockets, spindle-enable pin for
the magnet, spindle-direction pin 13 for the servo, the X/Y endstop headers). With the servo and
the safety guards the firmware needs about 2.9 KB of RAM, so on that shield it wants an
**Arduino Mega**, not an Uno (2 KB); see Flashing.

Without homing switches the firmware homes by driving each axis slowly past its end so the
motor stalls against the frame, then calls that spot 0. A stalled stepper is harmless at
25 mm/s; make sure nothing fragile is at the home corner.

### Electrical checklist

Each of these ruins a part when it is skipped. Values marked *measure* are for the person at
the bench, not from this file.

- **DRV8825 current limit before the motors are connected.** `I = 2 × Vref` on these boards:
  0.45 V ≈ 0.9 A, about right for these NEMA 17s (*measure*: the motor's rated current is on
  its label; set `Vref` to half of it or a little under). Set it with the motor unplugged, the
  driver powered, a meter from the trimpot to ground.
- **Never plug or unplug a stepper while the driver is powered.** The DRV8825 dies on the
  spot; this is the most common way to lose one. Logic off, motor supply off, then move wires.
- **Heat sinks on both drivers**, stuck on the chip, with air around them. At 0.9 A they run
  warm; at 1 A without a sink they thermal-cycle and skip steps.
- **The electromagnet is switched by a logic-level MOSFET** (gate to the ESP32 pin through
  about 100 Ω, a 10 kΩ pull-down so it is off while the ESP32 boots) **with a flyback diode**
  across the coil, cathode to +12 V. It is never driven straight from a GPIO: the coil draws
  far more than a pin can give and its kick-back on switch-off would destroy the pin.
- **ESP32 GPIO is 3.3 V only.** Nothing at 5 V goes into a pin: not a 5 V endstop board, not a
  servo's feedback, not a 5 V logic signal. The DRV8825 and MG90 inputs accept 3.3 V signals.
- **One common ground.** The 12 V supply's ground, the 5 V rail's ground, the ESP32's GND and
  the drivers' logic GND are tied together; a floating ground is what makes steppers hum and
  pins read noise.
- **A fuse on the motor rail**, 12 V (or 24 V if the supply changes): 3 A fast-blow for two
  motors at 0.9 A plus the magnet (*measure* the magnet's current: a 12 V electromagnet of this
  size draws 0.3–1 A).
- **Power-on order:** USB / logic first, then the motor supply, so the drivers see their enable
  pins held high (disabled) before they get motor power. Off in the other order. The firmware's
  safe-at-boot state (next section) is what makes this order safe.
- **The servo's 5 V** comes from the supply's 5 V rail or a small BEC, never from the ESP32's
  3.3 V pin or its 5 V USB pin: an MG90 stalls at several hundred mA and would reset the board.

## Safety policies

The firmware enforces these whatever the Pi sends; `config.h` holds the numbers with a comment
on what each protects. The Pi side adds its own on top.

| Policy | Default | What it protects | What happens |
| --- | ---: | --- | --- |
| `MAGNET_MAX_ON_MS` | 15 000 | the electromagnet coil (a held-on coil cooks) | after this long on, the magnet switches off, the arm drops, the board stops: alarm `magnet on too long`. A `CARRY` whose planned magnet time would pass it is refused before it starts (`error:10`), so the rule never drops a piece in a lane |
| `MAGNET_MIN_OFF_MS` | 500 | the coil's duty cycle | `M3` within this of the last `M5` is refused (`error:9`); inside a sequence the next pick-up waits instead |
| M3 preconditions | | a magnet that pulls nothing only heats | `M3` is refused while not homed (`error:8`), while moving (`error:3`), with the arm down (`error:12`) |
| `IDLE_DISABLE_MS` | 30 000 | motors and DRV8825s (holding current is pure heat) | drivers disabled at boot and after this long with no motion; re-enabled for the next move |
| `ENABLE_SETTLE_MS` | 5 | the DRV8825 charge pump | wait between enabling the drivers and the first step |
| `MAX_STEP_HZ` | 20 000 | the DRV8825's 1.9 µs minimum pulse, the ESP32 loop | every feed is capped so no motor is asked for more steps per second; acceleration is capped at `MAX_ACCEL_MM_S2` by construction (no command can raise it) |
| Move budget: `MOVE_BUDGET_FACTOR` × planned + `MOVE_BUDGET_MARGIN_MS` | 2 × + 1 000 | motors at full current while something is stuck | a move that runs past its budget stops, the magnet drops, the drivers switch off: alarm `move took too long`. Open-loop steppers cannot feel a jammed carriage; this catches a starved loop or a stuck sequence, and a stall shows up at the next camera check |
| Bump homing | 25 mm/s, 8 mm | the motors (a switchless bump is the most current they ever draw) | homing keeps its own low speed and short bump; nothing can raise them |
| `HOST_TIMEOUT_MS` | 20 000 | a piece left under a live magnet when the Pi crashes | with a sequence running and no line from the host for this long, the board **finishes the current `CARRY`** (no piece is dropped in a lane), then releases the magnet, lowers the arm, disables the drivers and reports `\|Host:lost` until `$X` or `$H`. Status polls (`?`) keep it alive and do not clear it |
| Servo arm | | the pieces and the arm | rapid motion with the arm up is refused (`G0`, `GOTO`, `CARRY`, any `G1` faster than carry speed: `error:11`); the arm drops at boot, on alarm, on hold, on `$X`, on host lost and after every `M5` |
| Motion needs a position | | the frame | `GOTO`, `CARRY`, `$T` and `M3` need `$H` first (`error:8`). `G0`/`G1` after `$X` are allowed in +X/+Y only (the soft limits at 0,0), so the first power-up checks work; the limit error then says `(not homed: $H first)` |
| `BOOT_SETTLE_MS` | 500 | a half-initialised board | every output is put safe first (drivers disabled, magnet off, arm down), then the firmware waits this long before listening, and drops anything that arrived meanwhile |
| Over-long line | | a truncated command acting as a different one | a serial line longer than the buffer (192 characters on the ESP32) is answered `error:1 line too long` and ignored |

On the Pi (`gantry_driver.py`, `pi_gantry.py`): a job is **refused** while the board reports an
alarm (other than the usual `not homed` after power-up), a hold or `Host:lost`, until a person
checks the pieces and runs `gantry_driver.py --unlock` (`$X`, then `$H`); a job with more than
`max_carries_per_job` (40) carries is a planning error and is refused before anything moves (a
long reset is split into several jobs instead); a wait for the board that times out sends `!`
then `M5` and `M8`; any exception during a job sends `M5`, `M8` and `!` best effort, so the
magnet is never left on and the arm never left up.

## Servo

The MG90 on the carriage carries the electromagnet on a short arm: **extended**, the magnet
touches the underside of the board top and can take a piece; **retracted**, it hangs clear so
the carriage can travel at full speed without dragging whatever it passes under. The firmware
drives it on `PIN_SERVO` with a 50 Hz pulse of 500–2500 µs (an LEDC channel on the ESP32, the
Servo library on AVR).

- `SERVO_EXTEND_DEG` / `SERVO_RETRACT_DEG` (defaults 90 / 0) are placeholders **to set on the
  real arm**: with the board homed, type `M7` and `M8` in the serial monitor and adjust until
  extended just touches the board top and retracted clears the pull on the thickest piece.
  `SERVO_SETTLE_MS` (300) is how long the firmware gives the arm to arrive before the magnet
  switches or the carriage moves.
- Inside a `CARRY`: travel to the piece → arm up (`M7`) → settle → magnet on (`M3`) → grab
  dwell → the carry moves at carry speed → magnet off (`M5`) → release dwell → arm down
  (`M8`) → settle. `$T` exercises the arm on e4 and in both of its carries. Status shows
  `|Servo:up` or `|Servo:down`.
- Power: the MG90 takes **5 V** from the supply's 5 V rail (or a BEC off the 12 V), ground to
  the common ground, and only its **signal wire** to GPIO 13. **Never power it from the ESP32's
  3.3 V pin**: it stalls at several hundred milliamps and would brown the board out mid-carry.
- `PIN_SERVO -1` means no servo: the sequence skips the arm steps, `M7`/`M8` answer `ok` and do
  nothing, and status has no `Servo` field. The pretend board follows `PIN_SERVO`, or
  `--no-servo` / `--servo`.

## What goes where

- **The ESP32** ("the Arduino") gets `firmware/gantry_firmware/`. That is the only thing
  that needs flashing.
- **The Pi** gets `pi/` and runs `gantry_driver.py`, which talks to the ESP32 over the USB
  cable; the touch-screen app and the website relay call it (see The Pi deployment). Nothing
  is flashed on the Pi. For a first test the same script runs on a laptop with the ESP32
  plugged into it (`pip install pyserial`, then `--port /dev/cu.usbserial-…` on a Mac,
  `/dev/ttyUSB0` on the Pi).

## Flashing

With arduino-cli (the esp32 core by Espressif, 2.0.17 is what this was built with):

```sh
arduino-cli core install esp32:esp32
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/gantry_firmware
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/ttyUSB0 firmware/gantry_firmware
```

or open `firmware/gantry_firmware/gantry_firmware.ino` in the Arduino IDE (the `.h` files in
the folder come along), board **ESP32 Dev Module**, and upload. No library is needed on the
ESP32 (the servo uses the core's LEDC). The sketch uses the plain Arduino API (`pinMode`,
`digitalWrite`, `micros`, `Serial`) and the motion and protocol code is the same that runs in
the laptop tests. Firmware 0.2 compiles to **288 KB of flash and 23 KB of RAM** on the ESP32
Dev Module. For a CNC shield it compiles for an **Arduino Mega 2560** (`arduino:avr:mega`,
`-DGANTRY_CNC_SHIELD`, the `Servo` library installed: 29 KB flash, 2.9 KB RAM); an Uno's 2 KB
of RAM no longer fits it.

Open the serial monitor at **115200** with "Newline" line endings: half a second after reset it
says `[duen-gantry 0.2 ready: $X to unlock, $H to home, $T to self-test, CARRY e2 e4 to move a
piece]`. `SELFTEST_AT_BOOT` (off by default) makes the sketch home itself five seconds after
power-up and then run `$T`, with nothing attached to the USB port; use it for a bench test, not
on a board with pieces on it.

## The first power-up

Before the first `$H`, check these with the carriage somewhere in the middle of its travel and
no pieces on the board:

1. **DRV8825 microstep pins.** Unconnected M0/M1/M2 mean full steps, and every move would
   be 8× too long. Tie **M0 and M1 to 3.3 V** (M2 low) for 1/8, or wire them to GPIOs and
   put those in `PIN_M0`…`PIN_M2` so the sketch sets them, or set `MICROSTEPS 1` to match
   full steps. Then `$X` and `G0 X10` must move the carriage **10 mm**: measure it.
2. **Current limit.** `Vref = I / 2` on the DRV8825's trimpot (0.45 V ≈ 0.9 A). Set it with the
   motor unplugged (see the electrical checklist).
3. **Direction.** `$X`, then `G0 X10`: the carriage must move toward the far end of X, away
   from the corner you want as the origin (the bottom-left corner of sketch 5). If it goes
   the other way, flip `INVERT_X_DIR` in `config.h` and re-upload. Same for `G0 Y10` and
   `INVERT_Y_DIR`. (Before homing only +X/+Y moves are allowed; a `G0 X-5` is refused with
   `outside the travel limits (not homed: $H first)`, which is the soft limit doing its job.)
4. **Homing.** `$H`: each axis drives slowly to its origin end, stalls there for a moment
   (that is normal without switches; it is the DRV8825 skipping, not damage), backs off
   2 mm, and `?` then shows `MPos:0.000,0.000|Homed:1`.
5. **Travel.** `G0 X400`, then `G0 X450`, then smaller steps until the carriage is 3–5 mm
   from the far end; that X is `MAX_X_MM`. Same for Y. Put them in `config.h` and in
   `travel_mm` in `gantry_config.json` if they differ from 470 × 420.
6. **Servo.** `M7` must lift the magnet to the board top, `M8` must drop it clear; adjust
   `SERVO_EXTEND_DEG` / `SERVO_RETRACT_DEG` until they do. With the arm up, `G0 X100` is
   refused (`servo arm up: M8 first`): that is intended.
7. **Magnet.** With the board homed and the arm up (`M7`), `M3` must pull a piece on the board
   top; `M5` must let it go and drop the arm. If `M3` does nothing, check `PIN_MAGNET` and the
   MOSFET; if it is on when it should be off, flip `MAGNET_ACTIVE_HIGH`. Left on, it switches
   itself off after 15 s with the alarm `magnet on too long`; `$X` clears that.
8. **Squares.** `GOTO e4`, `GOTO a1`, `GOTO h8`: the magnet should sit under those centres. If
   not, calibrate from the Pi (next section) rather than editing `config.h`: the Pi sends its
   numbers with `$B=` at every connection.
9. **Stop at any time:** `!` halts the motion immediately, drops the magnet and the arm; `$X`
   then `$H` to carry on. `$T` runs the board's own test once it is homed.

Then from the laptop or the Pi: `python3 gantry_driver.py --port … --calibrate` to find a1
and the square size, and `--move e2e4` with a piece on e2 for the first real carry.

## The protocol

Two levels, one line per command, every command answered `ok` or `error:<code> <text>`
(`protocol.h` has the full list and the reasons).

**Board level**, firmware 0.2: the board knows the squares.

| Command | Does |
| --- | --- |
| `$B=50,60,35,17.5,452.5,17.5,35` | square size, a1 centre (x, y), White's and Black's docking-lane x, first slot y, slot pitch. The Pi sends it from `gantry_config.json` on every connection; `$B=50,60,35` sets the squares only |
| `GOTO e4` | move the empty magnet to a place: a square, a docking slot (`w:3`, `b:0`), or `x,y` in squares (`5.5,0.5` is the lane crossing between e1, f1, e2 and f2; `-0.5,4` the lane beside the a file) |
| `CARRY g1 f3` | the whole carry as one sequence (see Servo): travel, arm up, magnet on, grab, the moves at carry speed, magnet off, release, arm down. Without via-points the board routes itself: straight when the two places share a file, rank or diagonal, otherwise out to a corner, along the lanes and in from a corner |
| `CARRY g1 f3 5.5,0.5 5.5,1.5` | the same carry through the via-points the Pi chose. Via-points are places too; a slot name (`b:1`) is fine |
| `CARRY d5 b:0 …` | a taken piece into slot 0 of Black's lane |
| `$T` | the board's own test, homed first: a1, h1, h8, a8, e4, arm and magnet on e4, carry e4 → f6 → e4 through the lanes, back to a1 |

A `GOTO`, `CARRY` or `$T` is queued as a **program** of up to 48 steps (moves, magnet, arm,
dwells) and answered `ok` at once; the board then runs it by itself while `?` shows
`<Run|…|Prog:k/n>`. One program at a time: a second `CARRY` while one runs is `error:3 busy`,
so the Pi polls `?` until `Idle` before the next.

**Millimetre level**, GRBL's language, so the same Pi code also drives a GRBL board:

`?` status: `<Idle|MPos:260.000,185.000|Mag:0|Homed:1|Servo:down>` (states `Run`, `Home`,
`Hold`, `Alarm:reason`; `|Prog:k/n` while a program runs; `|Host:lost` after the host went quiet
mid-program). `$H` home (magnet off, arm down first), `$X` unlock (magnet off, arm down),
`G0 X.. Y..` rapid, `G1 X.. Y.. F..` at F mm/min, `G4 P..` dwell, `G92` set position,
`M3`/`M5` magnet, `M7`/`M8` arm, `M17`/`M18` motors, `!` stop now (drops the program, the magnet
and the arm), `$$` settings (`$B`, `$G` guards and `$S` servo included), `$I` version.

Error codes: 1 unknown command, 2 bad number, 3 busy, 4 outside the travel limits, 5 locked
(`$X`/`$H`), 6 not a square, slot or x,y, 7 sequence too long, 8 not homed, 9 magnet cooling,
10 magnet would be on too long, 11 servo arm up (`M8` first), 12 servo arm down (`M7` first).

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
- `piece_sizes_mm`, `piece_mm`, `clearance_mm`, `lane_clearance_mm`: for the path finder.
- `servo`, `servo_settle_s`: whether the board has the arm, and how long it takes (only for
  the time estimate; the board does the sequencing).
- `protocol`: `duen` (firmware 0.2, `CARRY`) or `grbl` (G-code); `max_carries_per_job`: 40.

### What the driver sends, and why

The Pi has the camera and the game, so **the Pi plans**: `carry_plan.py` turns a chess move into
the pieces to carry (a capture docks the taken piece first, castling is the king then the rook,
en passant docks the pawn that was passed, a promotion brings a docked piece of that type or
leaves a note), and `board_paths.py` finds each piece a way round the others with their real
base widths, moving a blocker aside and back when there is none. **The ESP32 runs the motion**:
it gets one `CARRY from to via…` per piece, and does the travel, the arm, the magnet, the dwells
and the speed profile itself, so a serial hiccup or a slow Pi can never leave a sequence
half-done. On every connection the driver sends `$B=` from the config so the board's idea of e4
is the Pi's, checks the status (and refuses to start on an alarm, a hold or `Host:lost`), homes
if `home_on_start` is set, then sends the carries one at a time, polling `?` until `Idle` with a
timeout that grows with the carry's predicted length.

From the starting position, with the numbers in `gantry_config.json`:

```
e2e4:  CARRY e2 e4                                      ; Moving the white pawn from e2 to e4
g1f3:  CARRY g1 f3 5.5,0.5 5.5,1.5                      ; Moving the white knight from g1 to f3
e4d5:  CARRY d5 b:0 3.5,3.5 4.5,3.5 6.5,1.5 6.5,0.5 7.5,0.5 b:1   ; Taking the black pawn off d5 to slot b:0
       CARRY e4 d5                                      ; Moving the white pawn from e4 to d5
```

Via-points are in squares (the firmware's own unit), lane crossings at `x.5`; a docking slot is
named. The travel and the lane pitch come from the sketch; the square size (50 mm) and the a1
centre (60, 35) are read off a photo of it and are confirmed at the frame with

```sh
python3 gantry_driver.py --calibrate
```

which homes, asks you to steer the magnet under the centre of a1 by typing moves such as
`x+10`, `y-0.5` or `x-100` (millimetres) and `ok`, then the same for h1; a1 to h1 is seven
squares, so that gives the square size, and both numbers are written into the config, from
where every later connection sends them to the board. Every point of a job is checked against
the travel before anything moves, so a parking slot that does not fit says so instead of
running into the frame.

```sh
cd gantry/pi
python3 -m pip install pyserial
python3 gantry_driver.py --home
python3 gantry_driver.py --goto 100 100
python3 gantry_driver.py --move e2e4                        # one carry, path found round the start position
python3 gantry_driver.py --game e2e4 e7e5 g1f3 b8c6         # a whole game's carries, both sides
python3 gantry_driver.py --game e2e4 e7e5 g1f3 b8c6 --reset # then everything back home
python3 gantry_driver.py --reset --fen "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
python3 gantry_driver.py --reset --layout "e4=P,d5=p,b:0=p"
python3 gantry_driver.py --unlock                           # after a person has checked the board
python3 gantry_driver.py --dry-run --move g1f3              # prints the CARRY lines and the prediction, sends nothing
python3 gantry_driver.py --dry-run --gcode --move g1f3      # the same as G-code for a GRBL board
echo '{"label":"e2e4","steps":[{"op":"carry","piece":"P","from":"e2","to":"e4","path":[[4,1],[4,3]]}]}' | python3 gantry_driver.py
```

`--port sim` runs any of these against the pretend board. `--game` and `--reset` need
`python-chess` (the Pi's `.venv` has it); `--reset` from `--layout` takes `square=piece` pairs
with docked pieces as `w:n`/`b:n`, or a JSON object.

## Path finding, and making room

`pi/board_paths.py` checks every carry in millimetres. Each piece is a circle of its own
base width, from the piece models: 23 mm (pawn), 25 mm (knight, bishop, rook), 27 mm
(queen, king) — `piece_sizes_mm` in the config, with `piece_mm` (27) for a piece whose type
is not known. The search runs over tile centres, the midpoints between tiles and the
crossings between four tiles (the lanes), plus the parking lanes, and A* picks the shortest
route: a knight goes out to the corner of its square, along the lane between two
neighbours and into its target from the corner, the way a plotter would, never over a
piece. Along a lane the carried piece keeps `lane_clearance_mm` (1 mm) of air from each
neighbour — with 50 mm squares a 25 mm knight between two 23 mm pawns has exactly that —
and `clearance_mm` (3 mm) everywhere else. An empty parking lane counts as a corridor.
Only when no lane is wide enough (a 27 mm queen beside a 23 mm pawn needs 26 mm of
lane; there are 25) does it move one blocking piece to a free tile, make the move and put
the blocker back, which becomes three `CARRY` lines in order. If a piece brushes a neighbour
at the frame, raise `lane_clearance_mm`; if the squares turn out bigger, nothing changes but
`square_mm`.

The lanes beside the board (x = −0.5 and 7.5) run halfway between the edge file and the
docking lane, the same rule in the path finder, the driver and the firmware (`place_mm`). With
the docking lanes 42.5 mm from the edge files, that lane is 21.25 mm from each, so a piece
cannot slide along it past an occupied edge square: docked pieces leave and return through the
lane crossing at their own height instead, which the path finder works out.

```sh
python3 gantry_driver.py --dry-run --move g1f3            # from the starting position
python3 gantry_driver.py --dry-run --move a1a8 --occupied a1,a4,b2
```

Jobs from the website and from `carry_plan.py` carry `"before"` (the squares and slots
occupied as the job starts) and `"pieces"` (which piece is on each), so the Pi finds every path
afresh with these checks and the real widths; a job without them is run with the paths it came
with.

## The Pi deployment

The Pi runs the touch-screen app from `~/chess-board-pi-only` (branch `pi-only`); the gantry
code lives in this repo on branch `gantry`. Put it beside the main checkout as a worktree so
both branches are on disk at once:

```sh
cd ~/Self-Moving-Chess-Board-Code
git fetch origin gantry
git worktree add ~/chess-gantry gantry
~/chess-board-pi-only/.venv/bin/pip install pyserial
make -C ~/chess-gantry/gantry/firmware/host virtual_gantry    # optional: the pretend board, for --port sim
```

(`git worktree add ~/chess-gantry gantry` after a `git fetch` creates the branch from
`origin/gantry` the first time; later, `cd ~/chess-gantry && git pull`.)

Environment, in `~/.config/duen-chess/relay.env` for the website relay and in the touch-screen
app's autostart environment (`scripts/install_touch_autostart.py` in the app):

| Variable | Meaning | Default |
| --- | --- | --- |
| `CHESS_GANTRY_DIR` | this `gantry/pi` folder | the first of `~/chess-gantry/gantry/pi`, `~/Self-Moving-Chess-Board-Code/gantry/pi` that exists |
| `CHESS_GANTRY_PORT` | the ESP32's serial device (`/dev/ttyUSB0`; `ls /dev/ttyUSB* /dev/ttyACM*` after plugging in), or `sim` | unset: no gantry |
| `CHESS_GANTRY_CONFIG` | the config file | `<dir>/gantry_config.json` |

**The touch-screen app** (`pi_touch.py`) uses `pi_gantry.py` in `~/chess-board-pi-only`:
`GantryPlayer.from_env()` gives `None` when `CHESS_GANTRY_PORT` is unset and the app plays as
before, asking the person to move the computer's pieces. With a gantry, the app calls
`new_game()` at the start, `human_moved(board, move)` after the camera confirms the person's
move (so the layout follows the pieces: a piece the person took leaves the board), and
`play(board, move, say)` for the computer's move, which plans the carries with `carry_plan.py`,
runs them through `gantry_driver.run_job`, reports each carry through `say(text)` for the
screen, and returns any note left for the person (a promotion with no spare piece docked). The
app then verifies the board with the camera against `expected_squares()`. `reset(say)` carries
everything home for the next game and returns what the person must still place (pieces they
took stay with them). Every fault is a `GantryError` whose text goes on the screen; after one,
`unlock()` (or `gantry_driver.py --unlock`) is the way back once someone has looked at the board.
The link is opened on the first `play()`/`reset()` if `connect()` was not called.

**The website relay** (`duen-relay`, playing against someone online) uses the same driver as a
command: with `CHESS_GANTRY=arduino` and `CHESS_GANTRY_DIR`/`CHESS_GANTRY_PORT` set, it runs

```sh
<dir>/gantry_driver.py --port $CHESS_GANTRY_PORT --config $CHESS_GANTRY_CONFIG --quiet
```

with the move's job as JSON on standard input; exit status 0 means the pieces have stopped where
the job said, the last line of standard output is a JSON reply (`{"ok": true, "x_mm": …,
"y_mm": …, "servo": "down", …}` or `{"ok": false, "error": "…"}`), and anything else stops the
game until someone checks the board. The job format is the one `backend/board_motion.py` in
kennynakao/chess-website produces (carries in square units with `"before"` and `"pieces"`); the
earlier `CHESS_MOTION_COMMAND` form (the same script with no arguments) still works.

## Testing without hardware

```sh
make -C gantry/firmware/host test                                   # motion, protocol, servo and guard checks: 186 of them
cd gantry/pi && python3 -m unittest discover -s . -p 'test_*.py'    # paths 12, carry plan 9, driver 25
cd ~/chess-board-pi-only && CHESS_GANTRY_PORT=sim CHESS_GANTRY_DIR=~/chess-gantry/gantry/pi .venv/bin/python -m unittest test.test_pi_gantry
```

The firmware checks run `motion.h` and `protocol.h` on this computer with a fake clock: step
counts and the trapezoid, limits, hold, bump and switch homing, CoreXY, the board commands
(`GOTO`, `CARRY` with and without via-points, `$B`, `$T`, `!`), the servo order for a carry
(arm up → magnet on → move → magnet off → arm down) and the refusals with the arm up, the
magnet limits, the move budget, idle disable with the enable settle, the step-rate cap, and a
host that goes quiet during a carry.

The Pi suite builds `firmware/host/virtual_gantry`, a pretend board that runs the same code with
a simulated clock (`--speed` times faster than real time) and prints what it does as trace lines
(`# SERVO up X=… Y=…`, `# MAG on …`, `# MOVE steps=… to X=… Y=…`, `# MOTORS off`), and drives
it with the real Pi code (`--port sim`). It checks that a pawn push lifts the arm and switches
the magnet on over e2, moves exactly 4000 steps on the Y motor (100 mm × 40 steps/mm), and
releases and lowers over e4; that a capture is two `CARRY` lines, dock first; that a knight is
one `CARRY` with via-points through the lanes; that a held board is refused until `--unlock`,
that a fault in mid-carry sends `M5`, `M8`, `!`; that a board which lost its host finishes the
carry and is then refused until `--unlock`; the website's stdin/exit-code contract; G-code mode;
`--dry-run`; and `--game … --reset`. The bridge's own test plays a short game with a capture
against the pretend board and resets it.

## What the design files say

Measured from the files in the team Drive (`Gantry/3D Printing` and `CAD`):

- `2D Plotter V2.step` (the gantry): one `GT2 20 Teeth pulley`, two `NEMA 17 Stepper`, a
  `20x40 V Slot Profile` and a `20x20 V Slot Profile`, Delrin and solid V-wheel kits, four
  idler pulleys, 625 bearings, M5 hardware. The original controller is an Arduino Uno with
  a `CNC shield v1` and A4988 drivers, and the pen lift is an SG90 servo; the team's build
  swaps in DRV8825 drivers and the ESP32, and the MG90 arm is where the SG90 was. In the
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

- **Square size and a1 centre are estimates** (50 mm, (60, 35) from a photo of sketch 5) until
  `--calibrate` is run at the frame. The travel (470 × 420) is from the sketch; confirm it by
  homing and jogging to the far corner, and check the docking slots the same way.
- **Piece base diameters are estimates** from the models (23 / 25 / 27 mm); measure the
  printed pieces and put the numbers in `piece_sizes_mm`. The clearances (3 mm, 1 mm in a
  lane) are guesses to tune when a piece first brushes another.
- **The docking lanes sit 42.5 mm from the edge files**, so a piece cannot slide between an
  occupied edge square and a docked piece. Docking still works (through the lane crossing at
  each slot's height), but a reset after a long game may find no way for some piece and say so;
  the website planner's two-columns-of-eight layout is a separate mismatch with the sketch.
- **Servo angles** (`SERVO_EXTEND_DEG` / `SERVO_RETRACT_DEG`) are placeholders to set on the
  real arm; `SERVO_SETTLE_MS` and `grab_settle_s` to tune once the real pull through the board
  top is known.
- **Motor current:** read it off the motor's label and set `Vref` (the pulley is confirmed 20 T).
- **Direction:** if +X or +Y moves the wrong way, flip `INVERT_X_DIR` / `INVERT_Y_DIR`.
- **The move budget cannot feel a jam.** Open-loop steppers step whether or not the carriage
  moves; a stalled carry shows as a wrong position at the next camera check and a homing.
  Homing switches would make homing faster, quieter and gentler on the motors than bump homing:
  two microswitches and `PIN_X_MIN`/`PIN_Y_MIN` are all it takes.
- **An Uno no longer fits the firmware** (2.9 KB of RAM with the servo and the guards); the
  CNC-shield build is for a Mega.
