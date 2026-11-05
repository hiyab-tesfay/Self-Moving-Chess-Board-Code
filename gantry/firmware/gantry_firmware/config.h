// Everything the firmware knows about this particular board: pins, mechanics, limits.
// Change numbers here, not in motion.h or protocol.h.
#pragma once

// ----------------------------------------------------------------------------- pins
// Default: ESP32 Dev Module, the same pins as Prototype_Program.ino (motor 1 = X, motor 2 = Y).
// Build with -DGANTRY_CNC_SHIELD (or uncomment the define) for an Arduino Uno/Nano on a CNC
// Shield v3, which is what the GRBL kit in the Drive folder expects.
// #define GANTRY_CNC_SHIELD

#ifndef GANTRY_CNC_SHIELD
#define PIN_X_DIR 18
#define PIN_X_STEP 19
#define PIN_X_EN 21
#define PIN_Y_DIR 27    // bench wiring of 2026-11-05 (Zain's prototype had it on 22)
#define PIN_Y_STEP 25
#define PIN_Y_EN 26
#define PIN_MAGNET 32   // gate of the MOSFET (or relay input) that switches the 12 V electromagnet (was 27 until the
                        // bench wiring of 2026-11-05 put a direction wire there)
#define PIN_SERVO 12    // signal wire of the MG90 that pushes the magnet up to the board and pulls it back, as wired on
                        // the bench on 2026-11-05. GPIO 12 is a strapping pin (MTDI): the servo's signal input does not
                        // pull it, so boot is unaffected, but never tie it high. -1 = no servo: the magnet stays at the board top.
#define PIN_X_MIN -1    // homing switches at the X=0 and Y=0 ends; -1 when there are none (bump homing). The bench of
#define PIN_Y_MIN -1    // 2026-11-05 has none. With switches, use GPIO 34/35 (input-only: they need external pull-ups).
// DRV8825 microstep inputs M0 M1 M2. Left unconnected they read LOW = full steps, and the
// motion would be 8x too long for MICROSTEPS 8. Either tie them to 3.3 V by hand for 1/8
// (M0 and M1 high, M2 low) or wire them to GPIOs here and the sketch sets them.
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
#define STEP_PULSE_US 3           // the DRV8825 wants at least 1.9 us high and 1.9 us low

// ----------------------------------------------------------------------------- mechanics
// Motor: 1.8 degree NEMA 17 (200 full steps per turn), 13 N.cm / 18.4 oz.in short body.
// Driver: DRV8825. Microstep jumpers M0 M1 M2: 1/8 = on on off, 1/16 = off off on, 1/32 = on on on.
// Belt: GT2, 2 mm pitch, on the 20-tooth pulley from the 2D Plotter V2 design, so one turn
// moves the carriage 40 mm.
//   steps per mm = 200 * MICROSTEPS / (PULLEY_TEETH * 2 mm) = 40 at 1/8, 80 at 1/16.
#define MOTOR_FULL_STEPS_PER_REV 200
#define MICROSTEPS 8
#define PULLEY_TEETH 20
#define BELT_PITCH_MM 2.0f
#define STEPS_PER_MM ((float)MOTOR_FULL_STEPS_PER_REV * MICROSTEPS / (PULLEY_TEETH * BELT_PITCH_MM))

#define INVERT_X_DIR false        // flip if +X moves the wrong way
#define INVERT_Y_DIR false
#define COREXY false              // false: each motor drives its own axis (the 2D Plotter V2 layout)

// ----------------------------------------------------------------------------- travel and speed
// Soft limits in mm from the homed corner: how far the centre of the magnet can travel.
// From sketch 5 of the Fusion design (Chess Board Design), which traces the magnet's centre
// around the edge of where it can move: 470 x 420 mm, origin at its bottom-left corner.
// Confirm by homing and jogging to the far corner once the frame is built.
#define MAX_X_MM 470.0f
#define MAX_Y_MM 420.0f

// Chosen for the 13 N.cm motor and a light carriage; see README.md for the numbers behind them.
#define MAX_SPEED_MM_S 120.0f     // G0 and the top of G1
#define MAX_ACCEL_MM_S2 800.0f
#define HOMING_SPEED_MM_S 25.0f
#define HOMING_BUMP_MM 8.0f       // without switches, drive this far past the expected end so the stall is certain
#define HOMING_PULL_OFF_MM 2.0f   // then back off and call that 0

#define HOME_X_FIRST true

// ----------------------------------------------------------------------------- magnet and servo
#define MAGNET_ACTIVE_HIGH true
// The MG90 arm that carries the electromagnet: extended = magnet touching the underside of the
// board top, retracted = dropped clear of it so the carriage can travel without dragging pieces.
// 90 / 0 are placeholders: set them on the real arm with M7 (extend) and M8 (retract) while
// watching it, so that extended just touches the board and retracted clears the thickest piece's
// pull. The servo takes 5 V from the supply's 5 V rail (or a BEC), never from the ESP32's 3.3 V pin.
#define SERVO_EXTEND_DEG 90
#define SERVO_RETRACT_DEG 0
#define SERVO_SETTLE_MS 300       // time for the arm to arrive before the magnet switches or the carriage moves
#define SERVO_MIN_PULSE_US 500    // MG90: 500 us = 0 deg, 2500 us = 180 deg at 50 Hz
#define SERVO_MAX_PULSE_US 2500

// ----------------------------------------------------------------------------- the board, as the firmware knows it
// So that the Pi can say "CARRY g1 f3" and the board works out the millimetres itself. These are
// the defaults; the Pi sends its calibrated values with $B=square,a1x,a1y,parkWx,parkBx,parkRow0,parkPitch.
// Squares: a1's centre is at (A1_X_MM, A1_Y_MM) and each square is SQUARE_MM; x.5 are the lanes
// between squares. Captured pieces dock in a lane each side of the board (sketch 5: 12 circles
// at a 35 mm pitch), White's at x = PARK_W_X_MM, Black's at x = PARK_B_X_MM.
#define SQUARE_MM 50.0f
#define A1_X_MM 60.0f
#define A1_Y_MM 35.0f
#define PARK_W_X_MM 17.5f
#define PARK_B_X_MM 452.5f
#define PARK_ROW0_MM 17.5f
#define PARK_PITCH_MM 35.0f
#define PARK_ROWS 12

#define CARRY_SPEED_MM_S 60.0f    // with a piece under the magnet (G0/GOTO use MAX_SPEED_MM_S)
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
// The electromagnet coil. A 12 V electromagnet held on heats up; a carry is a few seconds, so
// anything longer is a fault (a crashed Pi, a stuck program). After MAGNET_MAX_ON_MS the firmware
// switches it off, stops and raises the alarm "magnet on too long"; M3 within MAGNET_MIN_OFF_MS of
// the last release is refused (a sequence waits instead), so the coil never sits at duty 1.
// A CARRY whose planned magnet-on time would exceed MAGNET_MAX_ON_MS is refused before it starts,
// so the policy never drops a piece in a lane. The magnet is off at boot, on any alarm, on hold
// (!), on $X, and after a program ends with the host lost. M3 is also refused while not homed,
// while moving, and (with a servo) while the arm is retracted: a magnet that is not at the board
// pulls nothing and only heats.
#define MAGNET_MAX_ON_MS 15000
#define MAGNET_MIN_OFF_MS 500

// The servo arm and the pieces. With the arm extended the magnet is at the board top, so a travel
// at full speed would drag whatever piece it passes under and could rip the arm: G0, GOTO, CARRY's
// own travel and any G1 faster than CARRY_SPEED_MM_S are refused while the arm is up (M8 first).
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
// matters if someone sets 1/32 microstepping or a tiny pulley). Acceleration is capped at
// MAX_ACCEL_MM_S2 by construction: no command can raise it.
#define MAX_STEP_HZ 20000.0f

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

// Power-up. Every output is put in its safe state (drivers disabled, magnet off, servo up) before
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
#define FIRMWARE_VERSION "duen-gantry 0.2"
