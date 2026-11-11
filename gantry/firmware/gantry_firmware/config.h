// Everything the firmware knows about this particular board: pins, mechanics, limits.
// Change numbers here, not in motion.h or protocol.h.
#pragma once

// ----------------------------------------------------------------------------- pins
// Default: ESP32 Dev Module, motor 1 = X and motor 2 = Y as in Prototype_Program.ino. Firmware 0.5 tried
// Zain's swapped mapping (his commit 16d7e82: motor 2 = X, X direction inverted); on the bench of
// 2026-11-06 the axes came out the wrong way round with it, so 0.6 is back to this one, which is also
// the mapping the direction check and the board tour of 2026-11-05 passed with.
// Build with -DGANTRY_CNC_SHIELD (or uncomment the define) for an Arduino Uno/Nano on a CNC
// Shield v3, which is what the GRBL kit in the Drive folder expects.
// #define GANTRY_CNC_SHIELD

#ifndef GANTRY_CNC_SHIELD
#define PIN_X_DIR 18    // motor 1: one turn (200 full steps, M0-M2 floating) moves it 41 mm, measured 2026-11-06
#define PIN_X_STEP 19
#define PIN_X_EN 21
#define PIN_Y_DIR 22    // motor 2 (it sat on 27 for an hour on 2026-11-05; Zain's prototype also used 22)
#define PIN_Y_STEP 25
#define PIN_Y_EN 26
#define PIN_MAGNET -1   // -1: no electromagnet. This build lifts a PERMANENT magnet with the servo (the user,
                        // 2026-11-06), so "magnet on" (M3) only means "holding a piece" and no pin is driven.
                        // Until 0.7.1 this was 32 and M3/M5 switched it; with an electromagnet wired, set it
                        // again (gate of its MOSFET) and the coil limits below.
#define PIN_SERVO 13    // signal wire of the MG90 that pushes the magnet up to the board and pulls it back. Moved off
                        // GPIO 12 on 2026-11-06: 12 is a strapping pin (MTDI), and with the servo on it the ESP32 booted
                        // with it high (boot:0x33), ran its flash at 1.8 V, could not read it and boot-looped. Never put
                        // anything on GPIO 12, 0, 2, 5 or 15. -1 = no servo: the magnet stays at the board top.
#define PIN_X_MIN -1    // homing switches at the X=0 and Y=0 ends; -1 when there are none (bump homing). The bench of
#define PIN_Y_MIN -1    // 2026-11-05 has none. With switches, use GPIO 34/35 (input-only: they need external pull-ups).
// DRV8825 microstep inputs M0 M1 M2. Left unconnected they read LOW = full steps, which is how the
// bench runs (MICROSTEPS 1 below). To use 1/8 steps instead, tie M0 and M1 to 3.3 V (M2 low) or
// wire them to GPIOs here, and set MICROSTEPS 8.
#define PIN_M0 -1
#define PIN_M1 -1
#define PIN_M2 -1
#else
#define PIN_X_DIR 5
#define PIN_X_STEP 2
#define PIN_X_EN 8      // one enable pin for all drivers on the shield
#define PIN_Y_DIR 6
#define PIN_Y_STEP 3
#define PIN_Y_EN 8
#define PIN_MAGNET 12   // "Spindle enable"
#define PIN_SERVO 13    // "Spindle direction" header carries the servo signal; -1 = no servo
#define PIN_X_MIN 9
#define PIN_Y_MIN 10
#define PIN_M0 -1       // on the shield the microstep mode is set with the jumpers under each driver
#define PIN_M1 -1
#define PIN_M2 -1
#endif

#define ENDSTOP_PRESSED_LEVEL 0   // switches wired to ground with a pull-up read 0 when pressed
// How long each STEP pulse stays high, in us. The DRV8825 itself needs only 1.9 us, but on the bench
// the 3 us pulses of firmware 0.4 never reached the drivers (both motors hummed and did not turn,
// even at 4 steps/s), while Zain's sketch, with 1500 us high and 1500 us low, moves both. So the
// default is his 1500. The step rate is capped so that the low time is at least as long as the
// high time: 1500 us allows 333 steps/s, 68 mm/s with the drivers on full steps (4.88 steps/mm). $0
// changes it at run time (STEP_PULSE_MIN_US..STEP_PULSE_MAX_US) to find the shortest that works.
#define STEP_PULSE_US 1500
#define STEP_PULSE_MIN_US 2
#define STEP_PULSE_MAX_US 5000

// ----------------------------------------------------------------------------- mechanics
// Motor: 1.8 degree NEMA 17 (200 full steps per turn), 13 N.cm / 18.4 oz.in short body.
// Driver: DRV8825. Microstep jumpers M0 M1 M2: 1/8 = on on off, 1/16 = off off on, 1/32 = on on on.
// Belt: GT2, 2 mm pitch, on the 20-tooth pulley from the 2D Plotter V2 design: 40 mm per turn on
// paper. Measured on the bench on 2026-11-06 with the console's one-turn buttons: one turn moves
// the carriage 41 mm on both X and Y (the belt rides a little outside the pitch line).
// MICROSTEPS 1: the DRV8825's M0-M2 pins are not connected on the bench, so the drivers take full steps.
//   steps per mm = 200 * MICROSTEPS / MM_PER_TURN = 4.878 (one step is 0.205 mm).
// $100 changes it at run time; the Pi and the console send the config's value (gantry_config.json
// "mm_per_turn") on every connection, so a better measurement needs no reflash.
#define MOTOR_FULL_STEPS_PER_REV 200
#define MICROSTEPS 1
#define PULLEY_TEETH 20
#define BELT_PITCH_MM 2.0f
#define MM_PER_TURN 41.0f
#define STEPS_PER_MM ((float)MOTOR_FULL_STEPS_PER_REV * MICROSTEPS / MM_PER_TURN)

#define INVERT_X_DIR false        // flip if +X moves the wrong way
#define INVERT_Y_DIR false
#define COREXY false              // false: each motor drives its own axis (the 2D Plotter V2 layout)

// ----------------------------------------------------------------------------- travel and speed
// Soft limits in mm from the homed corner: how far the centre of the magnet can travel. 0,0 is
// where the magnet sits with both carriages pushed into the bottom-left corner. Sketch 5 of the
// Fusion design traces exactly this: the 470 x 420 mm the magnet's centre can travel, its bottom-left
// corner being home (the magnet's 60 mm offset from the Y gantry's left edge is already in the sketch).
// $130 / $131 change them at run time; the Pi and the console send them on every connection.
#define MAX_X_MM 470.0f
#define MAX_Y_MM 420.0f

// Ceilings: no command can set more than these ($110 and $111 up to MAX_SPEED_MM_S, $120 up to
// MAX_ACCEL_MM_S2). The 13 N.cm motor's belt pull at 180 rpm leaves about a 2x margin at both.
#define MAX_SPEED_MM_S 120.0f
#define MAX_ACCEL_MM_S2 800.0f
// What the sketch starts with at power-up; $110 (travel speed), $111 (carry speed) and $120
// (acceleration) change them over serial until the next reset (RAM only, nothing is saved).
// Half the ceilings: a stepper's torque falls with speed, and a 13 N.cm NEMA 17 on a supply that
// sags (a battery pack) runs out of it well before 120 mm/s, which is a stall or a buzz on the
// bench. The lower acceleration leaves margin for a stiff spot in the rails. See README.md
// ("Stalls and buzzing").
#define DEFAULT_SPEED_MM_S 60.0f      // G0, GOTO, CARRY's travel to the piece, the top of G1
#define DEFAULT_ACCEL_MM_S2 400.0f    // every move
#define HOMING_SPEED_MM_S 25.0f       // never above the travel speed ($110)
#define HOMING_BUMP_MM 8.0f       // without switches, drive this far past the expected end so the stall is certain
#define HOMING_PULL_OFF_MM 2.0f   // then back off and call that 0

#define HOME_X_FIRST true

// ----------------------------------------------------------------------------- magnet and servo
#define MAGNET_ACTIVE_HIGH true
// The MG90 arm that carries the magnet: extended = magnet just under the board top (1-2 mm short of
// touching it, so the servo never presses into the board and has less to pull off a piece), retracted
// = dropped clear of it so the carriage can travel without dragging pieces, and short of the frame.
// 90 / 0 are placeholders: set them on the real arm with M7 (extend) and M8 (retract) while
// watching it. A servo held against a stop stalls at most of an amp: the likeliest cause of the
// ESP32 dying on 2026-11-06. The servo takes 5 V from the supply's 5 V rail (or a BEC), never from the ESP32's 3.3 V pin.
// `$S=up,down[,settle]` changes them at run time (0.7.2), so they can be found on the arm itself;
// a reset goes back to these.
#define SERVO_EXTEND_DEG 95    // 0.8.5: 5 deg either side of the centre (1500 us) until $S sets the calibrated
#define SERVO_RETRACT_DEG 85   // angles (the Pi sends them on connect): small enough to hit nothing
#define SERVO_SETTLE_MS 3000      // the full 0-180 sweep takes 2.9 s at SERVO_SWEEP_DEG_PER_S; time for the arm to arrive before the magnet switches or the carriage moves
// 0-180 deg here is 1100-1900 us (2026-11-07), not the full 500-2500: the user's bench sketch that moves
// this MG90S without trouble sweeps only 1100-1900 us, 5 us every 20 ms, "to avoid driving into the
// mechanical end stops". The old 0 deg = 500 us drove it into its end stop: it stalled, drew most of
// an amp from the USB (the servo is on VIN) and the USB link dropped.
// 0.8.5 (2026-11-10): REAL degrees. 0-180 = 500-2500 us (11.1 us per deg), 90 = 1500 us = the centre the horn
// was refitted at. Until 0.8.4 one "degree" was 4.44 us, about 0.4 of a real one, so +-90 in the
// calibration tool was only about +-36 real degrees. The end stops are kept off by the calibrated $S angles.
#define SERVO_MIN_PULSE_US 500
#define SERVO_MAX_PULSE_US 2500
// 0.8.3: angles go past 0-180 on the same scale (4.44 us per deg), for a horn mounted off the end of the range.
// The pulse never leaves SERVO_PULSE_FLOOR_US..SERVO_PULSE_CEIL_US, the MG90's rated ~500-2500 us.
#define SERVO_MIN_DEG -20    // 0.8.6: past both nominal ends (500 / 2500 us), for a servo that turns further
#define SERVO_MAX_DEG 200    // than its rating; -20 = 278 us, 200 = 2722 us. Past its internal stop it only buzzes.
#define SERVO_PULSE_FLOOR_US 270
#define SERVO_PULSE_CEIL_US 2730
// How the arm is driven (0.7.2, servo_arm.h): the pulse sweeps to a new angle at SERVO_SWEEP_DEG_PER_S
// (90 deg in 0.25 s, inside SERVO_SETTLE_MS) instead of jumping (0 jumps). The magnet is a PERMANENT
// magnet that the arm lifts (no electromagnet on this build), so the arm is held down too
// (SERVO_RELEASE_DOWN_MS 0): a limp arm could be pulled up toward the pieces. A positive value stops
// the pulses that long after the arm is down, only for an arm that cannot be pulled up. What keeps the
// MG90 from stalling is the two angles: up stops just short of the board, down short of the frame
// ($S=up,down while watching the arm).
#define SERVO_SWEEP_DEG_PER_S 62    // real deg since 0.8.5: 180 deg in 2.9 s (was 180 * 0.4 real deg), about the bench sketch's 5 us per 20 ms (3.2 s)
#define SERVO_RELEASE_DOWN_MS 0

// ----------------------------------------------------------------------------- the board, as the firmware knows it
// So that the Pi can say "CARRY g1 f3" and the board works out the millimetres itself. These are
// the defaults; the Pi sends its calibrated values with $B=square,a1x,a1y,parkWx,parkBx,parkRow0,parkPitch[,squareY].
// Squares: a1's centre is at (A1_X_MM, A1_Y_MM) and each square is SQUARE_MM; x.5 are the lanes
// between squares. Sketch 5 (measured 2026-11-06): 52.5 mm squares filling the 420 mm height,
// centred across the 470 mm (25 mm each side), so a1's centre is at 51.25, 26.25. Captured pieces
// dock in a column of circles on each side edge of the travel, White's at x = PARK_W_X_MM (0),
// Black's at x = PARK_B_X_MM (470): 17 circles 24.7 mm apart, of which every other one is a slot
// (the widest piece is 27 mm), 9 a side from y 12.35.
#define SQUARE_MM 52.5f
#define A1_X_MM 51.25f
#define A1_Y_MM 26.25f
#define PARK_W_X_MM 0.0f
#define PARK_B_X_MM 470.0f
#define PARK_ROW0_MM 12.35f
#define PARK_PITCH_MM 49.4125f
#define PARK_ROWS 9

// With a piece under the magnet (boot value of $111; its ceiling is MAX_SPEED_MM_S). Slower than a
// travel, for the same torque reason. With an electromagnet (MAGNET_MAX_ON_MS 15 s) the longest
// carries through the lanes (a1-h7 and the like, 620 mm) took longer than that at 40 mm/s and were
// refused (error:10); a permanent magnet has no such limit.
#define CARRY_SPEED_MM_S 40.0f
#define GRAB_MS 300               // after the magnet switches on, before moving
#define RELEASE_MS 200            // after it switches off, before moving away
// Longest sequence one command may queue: $T with the servo is 38 steps, a CARRY is 10 plus its
// via-points. An Uno has 2 KB of RAM in all, so it gets a shorter queue and a shorter line.
#ifdef __AVR__
#define PROGRAM_STEPS 40
#else
#define PROGRAM_STEPS 48
#endif

// ----------------------------------------------------------------------------- safety policies
// Each of these protects a part that can be destroyed by software alone. The firmware enforces
// them whatever the Pi sends; README.md ("Safety policies") lists them in plain words.
//
// The electromagnet coil, when there is one (PIN_MAGNET >= 0). A 12 V electromagnet held on heats up;
// a carry is a few seconds, so anything longer is a fault (a crashed Pi, a stuck program). After
// MAGNET_MAX_ON_MS the firmware switches it off, stops and raises the alarm "magnet on too long"; M3
// within MAGNET_MIN_OFF_MS of the last release is refused (a sequence waits instead), so the coil never
// sits at duty 1. A CARRY whose planned magnet-on time would exceed MAGNET_MAX_ON_MS is refused before
// it starts, so the policy never drops a piece in a lane. A permanent magnet has no coil to protect:
// both are 0 (off), so no carry is cut short or refused for its length. Either way "the magnet" is
// off at boot, on any alarm, on hold (!), on $X, and after a program ends with the host lost, and
// M3 is refused while not homed, while moving, and (with a servo) while the arm is retracted.
#if PIN_MAGNET >= 0
#define MAGNET_MAX_ON_MS 15000
#define MAGNET_MIN_OFF_MS 500
#else
#define MAGNET_MAX_ON_MS 0
#define MAGNET_MIN_OFF_MS 0
#endif

// The servo arm and the pieces. With the arm extended the magnet is at the board top, so a travel
// at full speed would drag whatever piece it passes under and could rip the arm: G0, GOTO, CARRY's
// own travel and any G1 faster than the carry speed ($111) are refused while the arm is up (M8 first).
// The arm is retracted at boot, on any alarm, on hold, on $X, on host lost and after every M5.

// The stepper motors and the DRV8825 drivers. A stepper that is enabled but not moving draws its
// full holding current and the driver dissipates it as heat; the 13 N.cm NEMA 17s and the small
// DRV8825 heat sinks are not meant to sit like that for hours. The drivers are disabled at boot
// and after IDLE_DISABLE_MS without motion; before the first step after enabling, the firmware
// waits ENABLE_SETTLE_MS for the driver's charge pump (the datasheet asks for about 1 ms).
#define IDLE_DISABLE_MS 30000
#define ENABLE_SETTLE_MS 5

// The step pulse rate. The DRV8825 needs at least 1.9 us high and 1.9 us low per step, and the
// ESP32 loop issues one step per pass; MAX_STEP_HZ caps the dominant motor's step rate, which
// caps every feed rate (at 40 steps/mm, 20 kHz is 500 mm/s, far above MAX_SPEED_MM_S: the cap
// matters if someone sets 1/32 microstepping, a tiny pulley or a large $100). It applies on top of
// $110 and $111. $120 can lower the acceleration or raise it back up to MAX_ACCEL_MM_S2, never past it.
// The step pulse ($0, STEP_PULSE_US) lowers the cap further, to 1e6 / (2 x pulse) steps/s.
#define MAX_STEP_HZ 20000.0f

// $100 (steps per mm) is accepted in this range: 4.88 at full steps (pins floating, 41 mm per
// turn), 39 at 1/8, 78 at 1/16, 156 at 1/32. $130 / $131 (travel) are accepted from MIN_TRAVEL_MM
// to MAX_TRAVEL_MM.
#define MIN_STEPS_PER_MM 1.0f
#define MAX_STEPS_PER_MM 400.0f
#define MIN_TRAVEL_MM 10.0f
#define MAX_TRAVEL_MM 1000.0f

// A move that takes far longer than planned means the loop is starved or a program is stuck; the
// motors would sit at full current in the meantime. Every move gets a time budget of
// MOVE_BUDGET_FACTOR times its planned duration plus MOVE_BUDGET_MARGIN_MS; past it the firmware
// stops, drops the magnet, disables the drivers and raises the alarm "move took too long".
// (Open-loop steppers cannot feel a jammed carriage; a stall shows up as a wrong position at the
// next camera check, and homing without switches relies on stalling on purpose, which is why
// HOMING_SPEED_MM_S stays low and HOMING_BUMP_MM short: a switchless bump is the most current
// the motors ever draw, so it must be slow and brief.)
#define MOVE_BUDGET_FACTOR 2.0f
#define MOVE_BUDGET_MARGIN_MS 1000

// The Pi going quiet while a program runs (a crash, a pulled cable). After HOST_TIMEOUT_MS with
// no line from the host the firmware finishes the running CARRY, so no piece is dropped in a
// lane, then releases the magnet, disables the drivers and reports |Host:lost in its status until
// $X or $H, so a person checks the board before the next job.
#define HOST_TIMEOUT_MS 20000

// Power-up. Every output is put in its safe state (drivers disabled, magnet off, servo limp: no pulses until the
// first M7/M8, 0.8.1, so a restart never drives the arm into an end of its travel) before
// anything else, and commands are accepted only after BOOT_SETTLE_MS, so a Pi that is still
// talking when the ESP32 resets cannot start a move on a half-initialised board.
#define BOOT_SETTLE_MS 500

// The Arduino's own test: type $T in the serial monitor, or set SELFTEST_AT_BOOT true to run it
// five seconds after power-up with nothing attached to the USB port (the sketch homes first).
// $T needs the board homed: it visits the four corner squares, switches the magnet on and off on
// e4, carries e4 -> f6 -> e4 through the lanes the way a knight moves, and returns to a1. Needs
// the motors wired and the travel clear.
#define SELFTEST_AT_BOOT false

#define SERIAL_BAUD 115200
#ifdef __AVR__
#define SERIAL_LINE_MAX 128       // longest command line; a CARRY with its via-points can pass 100 characters
#else
#define SERIAL_LINE_MAX 192
#endif
#define FIRMWARE_VERSION "duen-gantry 0.8.6"
