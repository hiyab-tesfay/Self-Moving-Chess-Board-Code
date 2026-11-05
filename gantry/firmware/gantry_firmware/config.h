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
#define PIN_Y_DIR 22
#define PIN_Y_STEP 25
#define PIN_Y_EN 26
#define PIN_MAGNET 27   // gate of the MOSFET (or relay input) that switches the 12 V electromagnet
#define PIN_SERVO -1    // MG90 that lowers/raises the magnet: set to its GPIO (e.g. 32) and install "ESP32Servo"; -1 = none
#define PIN_X_MIN 34    // homing switches at the X=0 and Y=0 ends; -1 when there are none (bump homing)
#define PIN_Y_MIN 35    // GPIO 34/35 are input-only on the ESP32, so they need external pull-ups
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
#define PIN_SERVO -1    // "Spindle direction" is pin 13 if a servo is added
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

// ----------------------------------------------------------------------------- magnet
#define MAGNET_ACTIVE_HIGH true
#define SERVO_DOWN_DEG 10         // magnet touching the underside of the board
#define SERVO_UP_DEG 70           // magnet dropped away from the board

#define SERIAL_BAUD 115200
#define FIRMWARE_VERSION "duen-gantry 0.1"
