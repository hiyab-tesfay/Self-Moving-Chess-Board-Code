# Gantry: moving the pieces

The board moves pieces with a **permanent magnet** under the top (no electromagnet on this build:
the servo lifting the magnet to the board is what grabs a piece), carried by an XY gantry built from
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

## Quick start: the gantry dashboard

The dashboard (`console/console_server.py` + `console/console.html`) is a web page that drives the
gantry: connect, home, jog, arm up / down, magnet, chess moves, free carries, Tune the squares,
and the camera check. Run it on any computer with the ESP32 on USB, or on the Pi.

**On your own computer (Mac or Linux), ESP32 plugged in by USB:**

```bash
git clone -b pi-gantry-touchscreen https://github.com/hiyab-tesfay/Self-Moving-Chess-Board-Code.git
cd Self-Moving-Chess-Board-Code
bash gantry/console/console.sh        # first run makes gantry/console/.venv (python-chess, pyserial)
```

It opens http://127.0.0.1:8788: pick the ESP32's port (`/dev/cu.usbserial-…` on a Mac,
`/dev/ttyUSB0` on Linux), press **Connect**, then home (Motors off → push the carriage into the
reference beneath the centre of the **a2 pawn** → **Set home here**). No board? `bash gantry/console/console.sh sim` runs
the same page against a pretend board (needs `g++` / `make` for the first build).

**On the Pi (the ESP32 plugged into the Pi):** the page listens on the Pi's 127.0.0.1 only, so
reach it through an SSH tunnel from your computer:

```bash
ssh smart@chessboard.local 'cd ~/chess-gantry/gantry/console && setsid nohup ~/chess-board-pi-only/.venv/bin/python console_server.py --http 8788 > ~/.local/state/console_server.log 2>&1 < /dev/null &'
ssh -N -L 8788:127.0.0.1:8788 smart@chessboard.local    # leave running, then open http://localhost:8788
```

Only one program can have the ESP32 open: close the dashboard before the touch screen starts a
game with the gantry (and the other way round). The settings that matter for a new board are in
`pi/gantry_config.json` (squares, `mm_per_turn`, `swap_xy`, `drag_lag_mm`, `servo_deg`); the
firmware is flashed from the dashboard's **⚡ Flash** button (see Flashing).
Before the first carry, find your arm's angles with `pi/arm_cal.py` (see Servo).

The current setup uses `swap_xy: false` and `home_square: "a2"`. X runs along the files and Y along
the ranks. For manual homing, place the magnet beneath the centre of a2 and press **Set home here**;
the carriage stays there while its coordinates are assigned. **Return home** returns to a2 with the
arm down. `home_beside: "a8"` still describes the motor direction from the frame corner. After changing
the axis mapping, discard the old remembered position and home at a2 once before moving.

## The numbers

Motor: 1.8° NEMA 17, 200 full steps per turn, 13 N·cm (18.4 oz·in) short body, about 1 A per
phase (the Drive parts list; the Amazon listing was not readable, so confirm the current on the
motor's label). Driver: DRV8825. Belt: GT2, 2 mm pitch, on the **20-tooth pulley** that the
plotter design specifies (`GT2 20 Teeth pulley` in `2D Plotter V2.step`); `PULLEY_TEETH` in
`config.h` if a different one is fitted. On the bench one turn moves the carriage 41 mm, not
40, so `config.h` and `gantry_config.json` count from the measured `MM_PER_TURN` / `mm_per_turn`.
With the 1500 µs step pulse the top speed on full steps is 1e6 / 3000 / 4.878 = **68 mm/s**.

**Where the board is (Sketch 5, measured 2026-11-06).** Sketch 5 of the Fusion design is the
470 × 420 mm rectangle the magnet's centre can travel ("Sketch 5 traces the center of the magnet
around the perimeter of where the magnet can move"), so its bottom-left corner is home and its
millimetres are what the board counts. Measured from the photo of the sketch: the 8 ranks fill
the full 420 mm height, so the squares are **52.5 mm**; the files are centred across with 25 mm
each side, so **a1's centre is at (51.25, 26.25)** and h8's at (418.75, 393.75); a docking column
of 17 circles (24.7 mm apart) sits on each side edge, **x 0 and x 470**, of which every other
circle is a slot (the widest piece is 27 mm), 9 a side from y 12.35. The magnet sits 60 mm right
of the Y gantry's left edge, but Sketch 5 already traces the magnet, so that offset is not added
again (`magnet_home_mm: [0, 0]`; adding it put every square more than a square to the left).
Both docking columns are in reach. If the board check shows every square off by the same
amount, the console's "a1 is here" moves `magnet_home_mm` and nothing else; if the far squares
drift, it is mm per turn or the square size (`square_mm` takes [x, y]).

| Quantity | Formula | Value |
| --- | ---: | ---: |
| mm per motor turn, on paper | teeth × pitch = 20 × 2 | 40 mm |
| mm per motor turn, **measured on the bench** (2026-11-06, X and Y) | | **41 mm** |
| steps per mm on full steps (the bench: M0–M2 not wired) | 200 × 1 / 41 | **4.878** (`$100`, firmware 0.7's default) |
| steps per mm at 1/8 microstep, once the pins are wired | 200 × 8 / 41 | 39.02 |
| resolution on full steps | 41 / 200 | 0.205 mm |
| travel speed at power-up (`DEFAULT_SPEED_MM_S`, `$110`) | | 60 mm/s = 90 rpm = 2.4 kHz steps |
| speed with a piece at power-up (`CARRY_SPEED_MM_S`, `$111`) | | 40 mm/s |
| acceleration at power-up (`DEFAULT_ACCEL_MM_S2`, `$120`) | | 400 mm/s² |
| ceiling for both speeds (`MAX_SPEED_MM_S`) | | 120 mm/s = 180 rpm = 4.8 kHz steps |
| ceiling for acceleration (`MAX_ACCEL_MM_S2`) | | 800 mm/s² |
| pulley pitch radius | 40 mm / 2π | 6.37 mm |
| belt pull at standstill | 0.13 N·m / 6.37 mm | 20 N |
| belt pull available at 180 rpm (about half) | | ~10 N |
| needed: 1 kg beam at 800 mm/s² + wheel friction + dragging a piece | 0.8 + ~2 + ~3 | ~6 N |

So on paper the 13 N·cm motor has roughly a 2× margin even at the ceilings, but only on a
stiff 12 V supply and free-running rails. The bench gantry stalled and buzzed there, so the
firmware starts at half of both (60 mm/s, 400 mm/s², 40 mm/s with a piece), and the three
numbers can be changed over serial (`$110`, `$111`, `$120`, see The protocol) without
reflashing, never past the ceilings. If a motor stalls or skips, lower `$120` first, then the
speeds; then work through Stalls and buzzing below. At the power-up values one square (50 mm)
with a piece takes about 1.3 s; a full-board diagonal about 8.5 s empty and 12.5 s with a piece;
a pawn push from the moment the job arrives (carriage on a1) is about 7.5 s including the arm
and the magnet settling, a knight through the lanes about 9.5 s, a capture about the same (the
person has already taken the captured piece off, so it is one carry). With 0.2's 120 mm/s,
800 mm/s² and 60 mm/s with a piece, the square was 0.9 s, the empty diagonal 4.3 s, the pawn
push 4.7 s and the knight 6 s.

Why 1/8 microstepping: 4.8 kHz at full speed is comfortable for the step loop on an ESP32
and still fine on an AVR; 1/16 doubles the step rate for no gain the belt can show.

DRV8825 setup: current limit `Vref = I / 2`, so **0.45 V** for a 0.9 A setting (0.35 V for
0.7 A); measure the motor's rated current off its label first. Microstep jumpers M0 M1 M2:
1/8 = on on off, 1/16 = off off on, 1/32 = on on on. The 12 V 2 A supply covers both motors;
the servo takes 5 V of its own (see Servo).

## Wiring

Pins are in `firmware/gantry_firmware/config.h`. The default block is the ESP32 Dev Module
with the same motor pins as `Prototype_Program/Prototype_Program.ino` (firmware 0.5 had them
swapped, after Zain's 16d7e82; on the bench that put the axes the wrong way round, so 0.6 is back
to this):

| Signal | ESP32 pin | Notes |
| --- | --- | --- |
| X DIR / STEP / EN | 18 / 19 / 21 | motor 1 in the prototype; one turn = 40 mm |
| Y DIR / STEP / EN | 22 / 25 / 26 | motor 2 in the prototype |
| magnet | none (`PIN_MAGNET -1`) | a permanent magnet lifted by the servo; 0.7.1 and earlier switched GPIO 32 for an electromagnet (gate of a MOSFET, flyback diode across the coil) |
| servo signal | 12 | the MG90's signal wire; its power comes from 5 V, not the ESP32. The prototype sketch has no servo pin; 12 is the bench wiring of 2026-11-05 (LEDC-capable; it is a strapping pin, so the servo must not pull it high at power-up); set `PIN_SERVO -1` without a servo |
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
- **If an electromagnet is ever fitted** (`PIN_MAGNET` set again), it is switched by a
  logic-level MOSFET (gate to the ESP32 pin through about 100 Ω, a 10 kΩ pull-down so it is off
  while the ESP32 boots) with a flyback diode across the coil, cathode to +12 V, never straight
  from a GPIO. This build has none: a permanent magnet, lifted by the servo.
- **ESP32 GPIO is 3.3 V only.** Nothing at 5 V goes into a pin: not a 5 V endstop board, not a
  servo's feedback, not a 5 V logic signal. The DRV8825 and MG90 inputs accept 3.3 V signals.
- **One common ground.** The 12 V supply's ground, the 5 V rail's ground, the ESP32's GND and
  the drivers' logic GND are tied together; a floating ground is what makes steppers hum and
  pins read noise.
- **A fuse on the motor rail**, 12 V (or 24 V if the supply changes): 3 A fast-blow for two
  motors at 0.9 A.
- **Power-on order:** USB / logic first, then the motor supply, so the drivers see their enable
  pins held high (disabled) before they get motor power. Off in the other order. The firmware's
  safe-at-boot state (next section) is what makes this order safe.
- **The servo's 5 V** comes from the supply's 5 V rail or a small BEC, never from the ESP32's
  3.3 V pin or its 5 V / VIN pin: an MG90 stalls at most of an amp. On 2026-11-06 the ESP32 died
  right after two real carries: the reply garbled the moment the arm went up and USB dropped
  seconds after it went down. The servo (its supply, and angles never set on the arm, so it may
  have been pushing at a stop) is the likeliest cause; check both before a new ESP32 goes in.

## Safety policies

The firmware enforces these whatever the Pi sends; `config.h` holds the numbers with a comment
on what each protects. The Pi side adds its own on top.

| Policy | Default | What it protects | What happens |
| --- | ---: | --- | --- |
| `MAGNET_MAX_ON_MS` | 0 (off: permanent magnet); 15 000 with an electromagnet | the electromagnet coil (a held-on coil cooks) | after this long on, the magnet switches off, the arm drops, the board stops: alarm `magnet on too long`. A `CARRY` whose planned magnet time would pass it is refused before it starts (`error:10`), so the rule never drops a piece in a lane |
| `MAGNET_MIN_OFF_MS` | 0 (off: permanent magnet); 500 with an electromagnet | the coil's duty cycle | `M3` within this of the last `M5` is refused (`error:9`); inside a sequence the next pick-up waits instead |
| M3 preconditions | | a carry only starts with the arm up at a piece (with a permanent magnet `M3`/`M5` only mark holding a piece) | `M3` is refused while not homed (`error:8`), while moving (`error:3`), with the arm down (`error:12`) |
| `IDLE_DISABLE_MS` | 30 000 | motors and DRV8825s (holding current is pure heat) | drivers disabled at boot and after this long with no motion; re-enabled for the next move |
| `ENABLE_SETTLE_MS` | 5 | the DRV8825 charge pump | wait between enabling the drivers and the first step |
| `MAX_STEP_HZ` | 20 000 | the DRV8825's 1.9 µs minimum pulse, the ESP32 loop | every feed is capped so no motor is asked for more steps per second, whatever `$110`, `$111` and `$100` say |
| Speed and acceleration ceilings: `MAX_SPEED_MM_S`, `MAX_ACCEL_MM_S2` | 120, 800 | the motors' torque margin | `$110`, `$111` and `$120` can lower the speeds and the acceleration or raise them back, never past these (`error:13`); a reset brings back the power-up values (60, 40, 400) |
| Move budget: `MOVE_BUDGET_FACTOR` × planned + `MOVE_BUDGET_MARGIN_MS` | 2 × + 1 000 | motors at full current while something is stuck | a move that runs past its budget stops, the magnet drops, the drivers switch off: alarm `move took too long`. Open-loop steppers cannot feel a jammed carriage; this catches a starved loop or a stuck sequence, and a stall shows up at the next camera check |
| Bump homing | 25 mm/s, 8 mm | the motors (a switchless bump is the most current they ever draw) | homing keeps its own low speed and short bump; nothing can raise them, and a `$110` below 25 lowers the homing speed with it |
| `HOST_TIMEOUT_MS` | 20 000 | a piece left under a live magnet when the Pi crashes | with a sequence running and no line from the host for this long, the board **finishes the current `CARRY`** (no piece is dropped in a lane), then releases the magnet, lowers the arm, disables the drivers and reports `\|Host:lost` until `$X` or `$H`. Status polls (`?`) keep it alive and do not clear it |
| Servo arm | | the pieces and the arm | rapid motion with the arm up is refused (`G0`, `GOTO`, `CARRY`, any `G1` faster than the carry speed `$111`: `error:11`); the arm drops at boot, on alarm, on hold, on `$X`, on host lost and after every `M5` |
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

The MG90 on the carriage carries the permanent magnet on a short arm: **extended**, the magnet
sits just under the board top and takes the piece above it; **retracted**, it hangs clear so
the carriage can travel at full speed without dragging whatever it passes under. The firmware
drives it on `PIN_SERVO` with a 50 Hz pulse of 500–2500 µs (an LEDC channel on the ESP32, the
Servo library on AVR). Since 0.7.2 (`servo_arm.h`) the pulse **sweeps** to a new angle at
360°/s (90° in 0.25 s) instead of jumping, so the servo never starts at full current, and the
arm is held at both ends (a limp arm could be pulled up toward the pieces).

- **Angles are real degrees since 0.8.5**: 0–180 = 500–2500 µs, 90 = 1500 µs = the centre; 0.8.6
  allows −20…200 (278–2722 µs) for a servo that turns past its rating. (Until 0.8.4 a "degree" was
  4.44 µs, about 0.4 of a real one.) The board **boots limp** (0.8.1+, no pulses until an `M7`/`M8`
  or a carry lifts the arm), stays limp through alarms, homing and `M5` (0.8.2), and `M9` lets go.
- **Calibrating the arm**: with the ESP32 on the Pi and nothing else holding the port, run
  `~/chess-board-pi-only/.venv/bin/python ~/chess-gantry/gantry/pi/arm_cal.py [start] [--zero RAW]`:
  arrows step 1° / 5°, `h` holds, `f` lets go, `u` / `b` mark up and down, `t` tests, `s` saves
  `servo_deg` into `pi/gantry_config.json` (the Pi sends it as `$S=up,down,settle` on every
  connect). `--zero RAW` counts from that board angle. If the arm's range sits off the end of the
  servo's 180°, move the horn on the spline (hold the servo at the middle of the range you need,
  refit the horn with the arm there): no pulse can turn the shaft past its internal stop.
  This build: `servo_deg` up 122, down 37.
- `SERVO_EXTEND_DEG` / `SERVO_RETRACT_DEG` (defaults 95 / 85 since 0.8.5, 5° either side of centre)
  are placeholders until `$S` arrives; **to set them by hand on the real arm**: with the board homed, send `$S=up,down` (0.7.2; `$S=up,down,settle` sets the settle
  ms too) from the console's command box, then `M7` and `M8`, and adjust until extended stops
  1–2 mm under the board top and retracted clears the pull on the thickest piece, **neither
  pressing against anything**: a servo held against a stop stalls at most of an amp. `$S` is
  refused with the arm up; a reset goes back to `config.h`, so put the numbers that work there.
  `SERVO_SETTLE_MS` (400) is how long the firmware gives the arm to arrive before the carriage
  moves.
- Inside a `CARRY`: travel to the piece → arm up (`M7`) → settle → magnet on (`M3`) → grab
  dwell → the carry moves at carry speed → magnet off (`M5`) → release dwell → arm down
  (`M8`) → settle. `$T` exercises the arm on e4 and in both of its carries. Status shows
  `|Servo:up` or `|Servo:down`.
- Power: the MG90 takes **5 V** from the supply's 5 V rail (or a BEC off the 12 V), ground to
  the common ground, and only its **signal wire** to GPIO 13 (never 12: a strapping pin that stops the ESP32 booting). **Never power it from the ESP32's
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
the laptop tests. Firmware 0.7.2 compiles to **290 KB of flash and 23 KB of RAM** on the ESP32
Dev Module. For a CNC shield it compiles for an **Arduino Mega 2560** (`arduino:avr:mega`,
`-DGANTRY_CNC_SHIELD`, the `Servo` library installed: 31 KB flash, 3.0 KB RAM); an Uno's 2 KB
of RAM no longer fits it. On the ESP32 the serial port gets a 512-byte transmit buffer, so a
reply is queued and the step loop never waits for the UART.

Open the serial monitor at **115200** with "Newline" line endings: half a second after reset it
says `[duen-gantry 0.7.2 ready: $X to unlock, $H to home, $T to self-test, CARRY e2 e4 to move a
piece]`. `SELFTEST_AT_BOOT` (off by default) makes the sketch home itself five seconds after
power-up and then run `$T`, with nothing attached to the USB port; use it for a bench test, not
on a board with pieces on it.

## The first power-up

Before the first `$H`, check these with the carriage somewhere in the middle of its travel and
no pieces on the board:

1. **DRV8825 microstep pins.** Unconnected M0/M1/M2 mean full steps, and every move would
   be 8× too long. Tie **M0 and M1 to 3.3 V** (M2 low) for 1/8, or wire them to GPIOs and
   put those in `PIN_M0`…`PIN_M2` so the sketch sets them, or set `MICROSTEPS 1` to match
   full steps (or, until the next reset, `$100=5`). Then `$X` and `G0 X10` must move the
   carriage **10 mm**: measure it.
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
6. **Servo.** `M7` must lift the magnet to just under the board top, `M8` must drop it clear,
   neither pushing against a stop; find the angles with `$S=up,down`, then put them in
   `SERVO_EXTEND_DEG` / `SERVO_RETRACT_DEG`. With the arm up, `G0 X100` is
   refused (`servo arm up: M8 first`): that is intended.
7. **Magnet.** With the board homed, a piece on e4 and the arm up (`M7`), `GOTO`-ing away at
   carry speed must drag the piece along; `M8` (or `M5`, which also drops the arm) must leave
   it behind. With the permanent magnet `M3` drives no pin.
8. **Squares.** `GOTO e4`, `GOTO a1`, `GOTO h8`: the magnet should sit under those centres. If
   not, calibrate from the Pi (next section) rather than editing `config.h`: the Pi sends its
   numbers with `$B=` at every connection.
9. **Stop at any time:** `!` halts the motion immediately, drops the magnet and the arm; `$X`
   then `$H` to carry on. `$T` runs the board's own test once it is homed.

Then from the laptop or the Pi: `python3 gantry_driver.py --port … --calibrate` to find a1
and the square size, and `--move e2e4` with a piece on e2 for the first real carry.

### Stalls and buzzing

A motor that stops and hums, buzzes without turning, or loses its place is short of torque for
what it is asked. The motion settings change over serial (RAM only, see The protocol), so each
check below needs no reflash.

- **Hums but never moves, even very slowly** (`G1 Y1 F6` is 4 steps a second): the steps are not
  reaching the driver. Firmware 0.4 held each STEP pulse high for 3 µs; on the bench that never
  turned either motor, while Zain's sketch with 1500 µs pulses moved both. 0.5 boots with 1500 µs
  (`$0`). Once both motors move, try shorter pulses (`$0=500`, `$0=100`) for more speed; the
  shortest that still moves both is the one to keep (`STEP_PULSE_US` in `config.h`). At 1500 µs
  the step rate tops out at 333/s: 66 mm/s on full steps but only 8 mm/s at 1/8 steps.
- **Slow it down first.** `$120=200`, `$110=30`, `$111=20`, and try the same move. If it runs,
  raise them a step at a time until it stalls and settle well below that, then put the values
  in `DEFAULT_SPEED_MM_S`, `CARRY_SPEED_MM_S` and `DEFAULT_ACCEL_MM_S2` in `config.h` (a reset
  forgets `$1xx`). If it stalls even at 30 mm/s, the cause is one of the next four.
- **Microstep pins.** For 1/8, M0 and M1 high (to 3.3 V) and M2 low on each DRV8825. Floating,
  they read low: full steps, so every move goes 8× too far and 8× too fast (a 60 mm/s travel
  turns the motor as if for 480 mm/s, far past where it has torque). Check it: `$X` just after
  power-up, mark the carriage, `G0 X40` (one pulley turn), and measure with a ruler. 40 mm is
  right; 320 mm, or a scream and a stall, is full steps: fix the pins, or `$100=5` until you
  can. For any other distance d, `$100` = 40 × 40 / d. `$100` forgets the position (alarm
  `not homed`), so `$HM` or `$H` after it.
- **Motor supply at least 12 V under load.** Measure across the driver's VMOT and GND while a
  move runs, not at rest. The DRV8825 shuts off below 8.2 V (the motor stops and buzzes as it
  restarts), and the torque at speed falls with the voltage long before that. Battery packs sag
  under the step current and as they empty; a 12 V 2 A bench supply rules this out.
- **Vref on each driver.** Current limit = 2 × Vref, set per motor (0.45 V ≈ 0.9 A for these
  motors, see the electrical checklist). Too low and the motor is weak; too high and the driver
  overheats, cuts out for a moment and the motor stutters.
- **Binding.** `M18` (drivers off), then push the carriage slowly by hand over the whole travel
  on both axes. It must roll with the same light resistance everywhere. A tight spot (an
  eccentric nut over-tightened on a V-wheel, a belt too tight, rails out of parallel) is where
  the motor will stall.

## The protocol

Two levels, one line per command, every command answered `ok` or `error:<code> <text>`
(`protocol.h` has the full list and the reasons).

**Board level**, firmware 0.4: the board knows the squares.

| Command | Does |
| --- | --- |
| `$B=52.5,51.25,26.25,0,470,12.35,49.41` | square size, a1 centre (x, y), White's and Black's docking-column x, first slot y, slot pitch, in the board's millimetres (Sketch 5). The Pi sends it from `gantry_config.json` on every connection; `$B=52.5,51.25,26.25` sets the squares only; an eighth number (0.7) is the square size along Y when it differs from X's |
| `$S=80,10` | (0.7.2) the servo arm's up and down angles in degrees, `$S=80,10,400` with the settle time in ms: found on the real arm without a reflash; refused with the arm up (`error:11`); the arm goes to the new down angle at once; a reset goes back to `config.h`. `$$` shows them |
| `GOTO e4` | move the empty magnet to a place: a square, a docking slot (`w:3`, `b:0`), or `x,y` in squares (`5.5,0.5` is the lane crossing between e1, f1, e2 and f2; `-0.5,4` the lane beside the a file) |
| `CARRY g1 f3` | the whole carry as one sequence (see Servo): travel, arm up, magnet on, grab, the moves at carry speed, magnet off, release, arm down. Without via-points the board routes itself: straight when the two places share a file, rank or diagonal, otherwise out to a corner, along the lanes and in from a corner |
| `CARRY g1 f3 5.5,0.5 5.5,1.5` | the same carry through the via-points the Pi chose. Via-points are places too; a slot name (`b:1`) is fine |
| `CARRY d5 b:0 …` | a piece into slot 0 of Black's docking lane. The firmware keeps the lanes for a later version; no planner sends a piece to one now (see Captured pieces) |
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
and the arm), `$$` settings (the values in use now, `$B`, `$G` guards and `$S` servo included),
`$I` version.

**Motion settings**, firmware 0.4 (`$0` from 0.5), for finding and curing a stall without reflashing:

| Command | Sets | At power-up | Range |
| --- | --- | ---: | ---: |
| `$0=<us>` | how long each STEP pulse stays high; the step rate is capped at 1e6 / (2 × pulse), so the low time is at least as long. On the bench 3 µs pulses (0.4) never reached the drivers, so both motors hummed without turning; Zain's sketch, at 1500 µs, moves them | 1500 | 2–5000 |
| `$110=<mm/s>` | travel speed: `G0`, `GOTO`, a `CARRY`'s travel to the piece, the top of `G1` | 60 | 1–120 |
| `$111=<mm/s>` | carry speed: a `CARRY`'s moves with the piece, and the fastest `G1` allowed with the arm up | 40 | 1–120 |
| `$120=<mm/s²>` | acceleration of every move | 400 | 1–800 |
| `$100=<steps/mm>` | steps per mm, both axes. Forgets the position: alarm `not homed` until `$H` or `$HM` | 4.87805 (200 / 41, full steps) | 1–400 |
| `$130=<mm>`, `$131=<mm>` | the soft limits along X and Y (0.7). The Pi and the console send them from `travel_mm` less `magnet_home_mm` | 470, 420 | 10–1000 |

Each answers `ok`; `error:3 busy` while anything moves or a program (a `GOTO`, `CARRY`, `$T` or
`G4`) runs; `error:2 bad number`; or `error:13 $110 must be 1..120`, naming the range of the
setting sent. They are kept in **RAM only**: a reset or power cycle brings back the `config.h`
values (`DEFAULT_SPEED_MM_S`, `CARRY_SPEED_MM_S`, `DEFAULT_ACCEL_MM_S2`, `STEPS_PER_MM`), so
put the numbers that work there. The upper ends are the compiled ceilings `MAX_SPEED_MM_S` and
`MAX_ACCEL_MM_S2`; nothing goes past them. Homing keeps its own 25 mm/s (less if `$110` is
lower), and `MAX_STEP_HZ` caps every feed on top (at `$100=400` that is 50 mm/s). With an
electromagnet (`MAGNET_MAX_ON_MS` 15 s) a `CARRY`'s magnet check (`error:10`) plans with the
`$111` and `$120` in use and refused the longest routes through the lanes at 40 mm/s; with the
permanent magnet there is no such check. The numbers follow GRBL's, the units and meanings do not: GRBL's
`$110`/`$111` are X and Y rates in mm/min and are saved; here they are travel and carry speed in
mm/s and are not.

Error codes: 1 unknown command, 2 bad number, 3 busy, 4 outside the travel limits, 5 locked
(`$X`/`$H`), 6 not a square, slot or x,y, 7 sequence too long, 8 not homed, 9 magnet cooling,
10 magnet would be on too long, 11 servo arm up (`M8` first), 12 servo arm down (`M7` first),
13 setting out of range (`error:13 $110 must be 1..120`).

## The bench console

A page on the Mac that drives the gantry over USB, for the bench: make a chess move on the page
and the gantry plays it on the real board.

```sh
bash gantry/console/console.sh        # opens http://127.0.0.1:8788; pick the ESP32's port, Connect
bash gantry/console/console.sh sim    # the pretend board
```

- **The gantry never takes a piece off the board.** A capture is always done by a person, as on the
  touch screen (whose camera warning asks for it) and the website: the console plans `Layout(captures="remove")`,
  waits with "Take the black pawn off d5 by hand", then carries the capturing piece onto the emptied square.
  The docking lanes stay in the firmware and the path finder but nothing is sent to them. A promotion
  carries the pawn and leaves a note to swap it by hand.
- **Homing.** Always the bottom-left corner: Motors off, push the carriage into the corner,
  Set home here (`$HM`), or Bump home (`$H`).
- **Moves are plain motion commands** (G1, M7, M3, M5, M8), not `CARRY`, so the page's travel
  and carry speeds work on firmware 0.3 too; 0.4 also takes the page's acceleration (`$120`). A
  piece is held for the whole carry: a permanent magnet needs no rests. Only on firmware that
  cuts the magnet (`$G` above 0: 0.7.1 and earlier) does a longer carry set the piece down on its
  clear path and pick it up again, 3 s inside that limit ("Rest the piece every" on the page
  adds a limit of its own).
- **Real millimetres.** On connecting the page sends `$100` (steps/mm from `mm_per_turn`,
  41 mm on the bench), `$130` and `$131` (the travel) when the board has other values; a new
  `$100` means home again. One motor turn (`X/Y: one turn +/−`) sends exactly one turn's steps
  (200 on full steps): measure how far the carriage went and the page gives mm per turn, with a
  button to save it into `gantry_config.json`. `+` is away from home (X toward the h-file, Y
  toward rank 8), `−` toward it. It has no limits and needs no home, since someone is at the
  gantry running it: past the board's own soft limits the page relabels the spot with `G92` for
  the turn and puts the true count back afterwards (also after Stop). The distance check (up to
  400 mm) measures the same with a ruler. Each axis has its own top speed (the X axis drags the
  heavy beam and stalls differently); a diagonal slows down so neither axis passes its own.
- **The board (Sketch 5).** Check the board visits a1, h1, h8, a8 and the first and last slot
  of each usable lane, stopping at each so a person can see the magnet is centred. "a1 is
  here" (steer the magnet over a1 first) shifts the whole sketch by the difference
  (`magnet_home_mm`) and saves it.
- **Tune the squares.** Pick Tune (beside Chess moves / Free carry) and click a square: the empty
  magnet goes where the gantry thinks it is. Nudge it onto the real centre (the pad or the arrow
  keys, 0.5–10 mm a press) and press Right place (or Enter). Each answer is saved in
  `gantry_config.json` as `square_nudges_mm` (how far that square really is from Sketch 5) and a
  correction is fitted from all of them: one answer shifts the whole board, a few far apart (a1,
  h1, h8, a8, then the middle; "Next corner to tune" walks them) also fix spacing and tilt. Answered
  squares go exactly where they were put; docking slots follow the shift but never past the travel.
  On the pretend board the answers are a copy that disappears on Disconnect. Only the console and
  the Pi's mm moves use them: the firmware's own `CARRY`/`$B` still go by a1 and the square size.
- **Overshoot.** A carried piece trails the magnet by 5–6 mm (the MuJoCo model; the path
  simulation's leash model), so every carry would stop that far short of the square. The page
  takes the magnet 5 mm past the last point along the last leg and back (`Overshoot`, 0 to
  turn it off); never outside the travel or the magnet's reach.
- **Stall checks:** one motor turn, the distance check, a speed ladder (out and back at rising speeds; press It
  stalled and the page suggests 70 % of the last clean speed for travel and 60 % for carries),
  a motors-off push test for binding, and a count of the board restarting by itself (supply
  dips).
- **Stop** (or Esc) writes `!` at once: motion stops, magnet off, arm down. A move stopped
  part-way can be retried after Unlock, or marked as put back or finished by hand. Undo carries
  the last move back; Reset board carries every piece home, docked ones included.

Settings are saved in `console/console_settings.json` on this computer only. Tests:
`python3 gantry/console/test_console.py` (against the pretend board).

## The Pi side

`pi/gantry_config.json` says where the squares are, in **Sketch 5's millimetres**, from the
sketch's bottom-left corner (the Fusion design's sketch of where the magnet's centre moves, 470 ×
420 mm), which is home. Files run along +X, ranks along +Y. `magnet_home_mm` ([0, 0]) is where the
magnet's centre sits on the sketch at home; `pi/board_layout.py` subtracts it, so a frame whose
home turns out elsewhere needs only that number changed.

- `mm_per_turn` ([41, 41], measured), `full_steps_per_turn` (200), `microsteps` (1 while the
  DRV8825 pins are not wired): give `$100`.

- `a1_center_mm`: the magnet's position when it is under the centre of a1.
- `square_mm`: the pitch of the squares.
- `park_columns_mm`: the docking lanes beside the board, White's on the left (`w`), Black's on
  the right (`b`); `park_rows_mm` lists the slot centres along them. In the sketch each side has
  one lane of twelve circles at a 35 mm pitch, so the file has one column per side with twelve
  rows. `docking_lanes` says which may be used; one outside the magnet's reach is left out with
  a warning. The bench console docks captures there; the touch screen and the website do not
  yet (see Captured pieces).
- `travel_mm`: the sketch's far corner; less `magnet_home_mm` it is the soft limits (`$130`,
  `$131`, `MAX_X_MM`/`MAX_Y_MM` in the firmware: 470 × 420).
- `piece_sizes_mm`, `piece_mm`, `clearance_mm`, `lane_clearance_mm`: for the path finder.
- `servo`, `servo_settle_s`: whether the board has the arm, and how long it takes (only for
  the time estimate; the board does the sequencing).
- `protocol`: `duen` (firmware 0.2, `CARRY`) or `grbl` (G-code); `max_carries_per_job`: 40.

### What the driver sends, and why

The Pi has the camera and the game, so **the Pi plans**: `carry_plan.py` turns a chess move into
the pieces to carry (a capture is a `remove` step for the taken piece, then the taker's carry;
castling is the king then the rook; en passant removes the pawn that was passed; a promotion
carries the pawn and leaves a note that it now counts as the promoted piece), and
`board_paths.py` finds each piece a way round the others with their real base widths, moving a
blocker aside and back when there is none. **The ESP32 runs the motion**: it gets one
`CARRY from to via…` per piece, and does the travel, the arm, the magnet, the dwells and the
speed profile itself, so a serial hiccup or a slow Pi can never leave a sequence half-done. On
every connection the driver sends `$B=` from the config so the board's idea of e4 is the Pi's,
checks the status (and refuses to start on an alarm, a hold or `Host:lost`), homes if
`home_on_start` is set, then sends the carries one at a time, polling `?` until `Idle` with a
timeout that grows with the carry's predicted length.

### Captured pieces

**The gantry does not take captured pieces off the board; a person does.** (The bench console is the exception: it docks them in the right lane.) When the computer's
move captures, the job starts with

```
{"op": "remove", "piece": "p", "square": "d5"}
```

before the carry of the capturing piece (for en passant it names the passed pawn's square). The
screen or the website shows "Take the black pawn off d5, then press Check."; each time the person
presses Check the camera looks at the square, and only once it is empty does the gantry play the
move, as a single carry straight onto the emptied square. The driver sends nothing for a `remove`
step (it prints `# person removes p from d5` in `--dry-run` and the log) and plans the carries
after it as if the square were already empty: **whoever runs the job has already waited for the
removal** (`pi_gantry.removal_needed()` on the touch screen; the relay for the website). The
removed piece leaves the Pi's layout: the person keeps it. At the end of a game the reset carries
every piece that is still on the board to a home square and says "Put a black pawn on e7." for
each square whose piece the person took; the reset job still expects the full starting position,
so the camera check after it only passes once those pieces are back.

The docking lanes beside the board (`w:0`…, `b:0`…, `$B=`'s last four numbers, `park_columns_mm`)
stay in the firmware, the driver and the path finder for a later version that parks the captured
pieces itself; no planner sends a piece to one now, and `carry_plan.Layout` has no slots.

From the starting position, with the numbers in `gantry_config.json`:

```
e2e4:  CARRY e2 e4                                      ; Moving the white pawn from e2 to e4
g1f3:  CARRY g1 f3 5.5,0.5 5.5,1.5                      ; Moving the white knight from g1 to f3
e4d5:  # person removes p from d5                       ; The person takes the black pawn off d5 (the caller waits for the camera before this job runs)
       CARRY e4 d5                                      ; Moving the white pawn from e4 to d5
```

Via-points are in squares (the firmware's own unit), lane crossings at `x.5`; a docking slot,
when one is ever used, is named. The travel and the lane pitch come from the sketch; the square
size (50 mm) and the a1 centre (60, 35) are read off a photo of it and are confirmed at the frame
with

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
(a piece left in a docking slot may be written `w:n`/`b:n` and is carried home), or a JSON
object. In `--game`, a capture prints the person's removal as a line of its own and then the
one carry.

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

Options in `gantry_config.json`, each measured by the path simulation below:

- `docking_lanes: ["w", "b"]`: both docking columns hold pieces (Sketch 5 puts them on the
  travel's side edges). 80 random capture-heavy games on this layout (`path_sim.py --variant
  "as configured"`, 2026-11-06): 6230 moves, none failed, no capture left to the person, closest
  planned gap 1.25 mm. (Before 2026-11-06 the config was `docking_lanes: ["b"]` and
  `reach_mm: [50, 0, 470, 420]`; the table below is from then.)
- `path_smoothing: true`: after A*, straight shortcuts over the grid path wherever the open
  clearance (3 mm) holds. About half the corners go (every corner is a stop on the gantry); a
  shortcut that is one leg the firmware would take round its own corner gets its midpoint as a
  via-point (`firmware_keeps_line`), so `CARRY` stays on the planned line.
- `corner_lag_mm`, `turn_penalty_mm`: off and 2 mm. The trailing piece cuts inside a corner, but
  when both legs of a corner are clear the short chord it cuts is too; the simulation found it
  changes nothing.

A reset puts the back ranks home before the pawns (a queen cannot pass between two pawns that
are already home) and a stray standing deep in the home ranks before one in the middle; the
driver also carries a blocked piece later in a reset instead of failing (`"any_order"` jobs).

### The path simulation

```sh
python3 gantry/sim/path_sim.py                             # 100 seeded random games per setting
python3 gantry/sim/path_sim.py --games 40 --engine         # Stockfish games (stockfish on PATH)
```

It plays many games through the same code the console uses (dock mode, `best_slot`,
`gantry_driver.route`, the fallbacks), resets the board after each, and prints a table per
setting: failed moves and resets, captures the person had to take, make-room carries, path
length and corners, the time at the console's speeds, how often the magnet would be on past
12 s (the console rests it), the planned and the trailing piece's clearance (the piece pulled
along on a 5.5 mm leash), how far short it stops with and without the overshoot, the leftmost
point used, and the planning time.

What it found (2026-11-05; 200 random capture-heavy games, 15,692 moves, and 40 Stockfish
games, 3,592 moves, each game reset afterwards):

| | both lanes, old | right lane only | + shortcuts (chosen) |
|---|---|---|---|
| failed moves / resets | 0 / 0 | 0 / 0 | 0 / 0 |
| captures the person takes (no way into the lane) | 5 + 1 | 6 + 1 | 6 + 1 |
| corners per carry | 0.53 | 0.52 | 0.26 |
| mean path, mm | 124.3 | 124.2 | 122.1 |
| mean reset, s | 387 | 387 | 373 |
| trailing piece's closest gap, mm | 0.95 | 0.95 | 0.93 |

Losing the left side costs one capture in ~16,000 moves. Before the reset order and retry,
2 of 200 resets (and 2 of 40 Stockfish ones) failed with a queen walled in on c8 or f8; before
the speed-up the worst plan took 6 s on this Mac. The corner check and a higher corner cost
change nothing. Every carry still stops 5.5 mm short without the overshoot; with it, 0. About
16 % of carries keep the magnet on past 12 s at 25 mm/s and rest it once: a faster carry speed,
once the axes are reliable, is what shortens those.

The lanes beside the board (x = −0.5 and 7.5) run halfway between the edge file and the
docking lane, the same rule in the path finder, the driver and the firmware (`place_mm`). With
the docking lanes 42.5 mm from the edge files, that lane is 21.25 mm from each, so a piece
cannot slide along it past an occupied edge square; a piece that is ever docked leaves and
returns through the lane crossing at its own height instead, which the path finder works out.

```sh
python3 gantry_driver.py --dry-run --move g1f3            # from the starting position
python3 gantry_driver.py --dry-run --move a1a8 --occupied a1,a4,b2
```

Jobs from the website and from `carry_plan.py` carry `"before"` (the squares and slots
occupied as the job starts) and `"pieces"` (which piece is on each), so the Pi finds every path
afresh with these checks and the real widths; a job without them is run with the paths it came
with. A `remove` step empties its square for the carries after it, so the capturing piece's path
ends on a free square.

## The Pi deployment

**The gantry terminal over SSH (2026-11-07).** The ESP32 plugs into the Pi (USB-A on the Pi to USB-C on the
ESP32; it shows up as `/dev/ttyUSB0`), and the Mac drives it through the Pi:

```bash
bash deploy_pi.sh        # copy this folder to the Pi's ~/chess-gantry/gantry (backup first, in ~/chess-gantry-backups)
bash pi_terminal.sh      # ssh -t into the Pi and open console/gantry_term.py there
```

`gantry_term.py` uses the browser console's own code (`console_server.Console`), so moves, arm handling and
Tune answers behave the same. Commands: `home`, `up`/`down`, `x+ 10`, `y- 0.5`, `go e4`, `turn x+`, `keys`
(arrow keys / WASD jog, `+`/`-` step, `u`/`j` arm, Enter = right place), `tune e4` then jog then `ok`, `tunes`,
`forget e4`, `raw $$`, `stop`, `quit`; `help` lists them.

**Playing Stockfish from the terminal** (bench test of the whole chain without the camera; needs
`sudo apt install stockfish` on the Pi): `home`, then `play white intermediate`. Stockfish plays Black and the
gantry carries its pieces; on your turn make your move by hand and type it (`e4`, `Nf3`, `e7e8q`), or
`gantry e4` to have the gantry move your piece too. A capture stops and asks you to take the piece off,
then Enter. `play watch beginner` lets Stockfish play both sides; `board` shows the game, `stop` leaves it
(the game is kept), `new` starts over, `move <m>` has the gantry play one move. In the real game the touch
app (`pi_touch.py` with `pi_gantry.py`) does the same with the camera checking every move; it uses the
gantry when `CHESS_GANTRY_PORT` is set (`auto` finds `/dev/ttyUSB0`). The terminal and the touch app
cannot use the ESP32's port at the same time. Tune answers are saved in the Pi's
`pi/gantry_config.json`, and `deploy_pi.sh` keeps that file once it has answers. It runs on the touch app's
Python (`~/chess-board-pi-only/.venv`, which has pyserial and python-chess). `bash pi_terminal.sh --port sim`
uses the pretend board on the Pi.


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
move (so the layout follows the pieces: a piece the person took leaves the board), and, for the
computer's move, first `removal_needed(board, move)`: `("d5", "p")` when the move captures (the
passed pawn's square for en passant), else `None`. When a removal is needed the app asks the
person to take that piece off and, each time they press Check, looks at the square with the
camera; only once it is empty does it call `play(board, move, say)`, which plans the carries
with `carry_plan.py` (the job begins with the `remove` step, which the driver treats as already
done), runs them through `gantry_driver.run_job`, reports each carry through `say(text)` for the
screen, and returns any note left for the person (a promotion: the pawn now counts as the
promoted piece). `play()` never waits for a removal itself. The app then verifies the board with
the camera against `expected_squares()`, which no longer lists the captured piece. `reset(say)`
carries every piece on the board home for the next game and returns what the person must still
do ("Put a black pawn on e7." for each piece they took); after it `expected_squares()` is the
full starting position, so the check passes once those pieces are back. Every fault is a
`GantryError` whose text goes on the screen; after one, `unlock()` (or
`gantry_driver.py --unlock`) is the way back once someone has looked at the board. The link is
opened on the first `play()`/`reset()` if `connect()` was not called.

**The website relay** (`duen-relay`, playing against someone online) uses the same driver as a
command: with `CHESS_GANTRY=arduino` and `CHESS_GANTRY_DIR`/`CHESS_GANTRY_PORT` set, it runs

```sh
<dir>/gantry_driver.py --port $CHESS_GANTRY_PORT --config $CHESS_GANTRY_CONFIG --quiet
```

with the move's job as JSON on standard input; exit status 0 means the pieces have stopped where
the job said, the last line of standard output is a JSON reply (`{"ok": true, "x_mm": …,
"y_mm": …, "servo": "down", …}` or `{"ok": false, "error": "…"}`), and anything else stops the
game until someone checks the board. The job format is the one `backend/board_motion.py` in
kennynakao/chess-website produces (carries in square units with `"before"` and `"pieces"`, and a
`remove` step first for a capture, which the relay must have waited for, camera-checked, before
running the job); the earlier `CHESS_MOTION_COMMAND` form (the same script with no arguments)
still works.

## Testing without hardware

```sh
make -C gantry/firmware/host test                                   # motion, protocol, servo, guard and settings checks: 300 of them
cd gantry/pi && python3 -m unittest discover -s . -p 'test_*.py'    # paths 19, carry plan 26, driver 32
cd gantry/console && python3 -m unittest test_console                  # the bench console, 16
cd ~/chess-board-pi-only && CHESS_GANTRY_PORT=sim CHESS_GANTRY_DIR=~/chess-gantry/gantry/pi .venv/bin/python -m unittest test.test_pi_gantry
```

The firmware checks run `motion.h` and `protocol.h` on this computer with a fake clock: step
counts and the trapezoid, limits, hold, bump and switch homing, CoreXY, the board commands
(`GOTO`, `CARRY` with and without via-points, `$B`, `$T`, `!`), the servo order for a carry
(arm up → magnet on → move → magnet off → arm down) and the refusals with the arm up, the
magnet limits, the move budget, idle disable with the enable settle, the step-rate cap, a
host that goes quiet during a carry, and the motion settings: the power-up values, `$110`,
`$111`, `$120` and `$100` accepted, out of range and busy, a move timed against the trapezoid
the new numbers predict, the carry legs at `$111`, the magnet check at the current carry speed,
and `$100=5` clearing the home so a 10 mm move is 50 steps.

The Pi suite builds `firmware/host/virtual_gantry`, a pretend board that runs the same code with
a simulated clock (`--speed` times faster than real time) and prints what it does as trace lines
(`# SERVO up X=… Y=…`, `# MAG on …`, `# MOVE steps=… to X=… Y=…`, `# MOTORS off`), and drives
it with the real Pi code (`--port sim`). It checks that a pawn push lifts the arm and switches
the magnet on over e2, moves exactly 4000 steps on the Y motor (100 mm × 40 steps/mm), and
releases and lowers over e4; that a capture (a `remove` step, then the taker) is exactly one
`CARRY` onto the emptied square with no slot anywhere, and that a hand-written carry into a slot
is still understood; that a knight is one `CARRY` with via-points through the lanes; that a held
board is refused until `--unlock`, that a fault in mid-carry sends `M5`, `M8`, `!`; that a board
which lost its host finishes the carry and is then refused until `--unlock`; the website's
stdin/exit-code contract; G-code mode; `--dry-run`; and `--game … --reset` (the removals printed,
the reset's "Put a … on …" notes). The bridge's own tests check `removal_needed()` over a short
game, then play it with a capture against the pretend board (one carry for the capture, the
expected squares after every move) and reset it (the notes for the pieces the person took, the
full start expected afterwards).

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
- **The docking lanes are unused in this version.** A person takes captured pieces off and the
  camera confirms it before the gantry moves (see Captured pieces). The lanes stay in the
  firmware, the driver and the path finder for a later version that parks pieces itself; when
  that comes, note that they sit 42.5 mm from the edge files, so a piece cannot slide between an
  occupied edge square and a docked piece (docking works through the lane crossing at each
  slot's height), and that the website planner's two-columns-of-eight layout is a separate
  mismatch with the sketch.
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
