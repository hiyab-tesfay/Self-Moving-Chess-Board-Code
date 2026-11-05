// DUEN chess board gantry firmware: two NEMA 17s on DRV8825s and a magnet, driven over USB
// serial by the Raspberry Pi (gantry/pi/gantry_driver.py). This file is only the glue to the
// board's pins; the movement itself is in motion.h and the serial language in protocol.h.
//
// Board: ESP32 Dev Module (same pins as Prototype_Program.ino) or, with GANTRY_CNC_SHIELD
// defined in config.h, an Arduino Uno/Nano on a CNC Shield v3. Open gantry_firmware.ino in
// the Arduino IDE; the other .h files in this folder come along.
#include <Arduino.h>

#include "config.h"
#include "motion.h"
#include "protocol.h"

#if PIN_SERVO >= 0
#if defined(ESP32)
#include <ESP32Servo.h>   // Library Manager: "ESP32Servo"
#else
#include <Servo.h>
#endif
static Servo lift;
#endif

struct BoardHal : gantry::Hal {
  void set_dir(int motor, bool positive) override {
    digitalWrite(motor == 0 ? PIN_X_DIR : PIN_Y_DIR, positive ? HIGH : LOW);
  }
  void step(uint8_t mask) override {
    if (mask & 1) digitalWrite(PIN_X_STEP, HIGH);
    if (mask & 2) digitalWrite(PIN_Y_STEP, HIGH);
    delayMicroseconds(STEP_PULSE_US);
    if (mask & 1) digitalWrite(PIN_X_STEP, LOW);
    if (mask & 2) digitalWrite(PIN_Y_STEP, LOW);
  }
  void enable_motors(bool on) override {
    digitalWrite(PIN_X_EN, on ? LOW : HIGH);   // DRV8825 enable is active low
    digitalWrite(PIN_Y_EN, on ? LOW : HIGH);
  }
  bool endstop(int axis) override {
    int pin = axis == gantry::X ? PIN_X_MIN : PIN_Y_MIN;
    if (pin < 0) return false;
    return digitalRead(pin) == ENDSTOP_PRESSED_LEVEL;
  }
  uint32_t now_us() override { return micros(); }
};

struct BoardPeripherals : gantry::Peripherals {
  bool on = false;
  void magnet(bool want) override {
    on = want;
    digitalWrite(PIN_MAGNET, (want == MAGNET_ACTIVE_HIGH) ? HIGH : LOW);
#if PIN_SERVO >= 0
    lift.write(want ? SERVO_DOWN_DEG : SERVO_UP_DEG);
#endif
  }
  bool magnet_on() override { return on; }
};

struct SerialWriter : gantry::Writer {
  void write(const char* text) override { Serial.print(text); }
};

static const gantry::Settings SETTINGS = {
    STEPS_PER_MM,
    {MAX_X_MM, MAX_Y_MM},
    MAX_SPEED_MM_S,
    MAX_ACCEL_MM_S2,
    HOMING_SPEED_MM_S,
    HOMING_BUMP_MM,
    HOMING_PULL_OFF_MM,
    {INVERT_X_DIR, INVERT_Y_DIR},
    COREXY,
    PIN_X_MIN >= 0 && PIN_Y_MIN >= 0,
    HOME_X_FIRST,
};

static const gantry::BoardGeometry BOARD = {SQUARE_MM, A1_X_MM, A1_Y_MM, PARK_W_X_MM, PARK_B_X_MM, PARK_ROW0_MM, PARK_PITCH_MM, PARK_ROWS};

static BoardHal hal;
static BoardPeripherals peripherals;
static SerialWriter writer;
static gantry::Motion motion(hal, SETTINGS);
static gantry::Protocol protocol(motion, peripherals, writer, SETTINGS, BOARD, FIRMWARE_VERSION);

static char line[96];
static size_t line_len = 0;

// DRV8825 M0 M1 M2 for each microstep setting, when those inputs are wired to GPIOs.
static void set_microstep_pins() {
  int m0 = 0, m1 = 0, m2 = 0;
  switch (MICROSTEPS) {
    case 2: m0 = 1; break;
    case 4: m1 = 1; break;
    case 8: m0 = 1; m1 = 1; break;
    case 16: m2 = 1; break;
    case 32: m0 = 1; m1 = 1; m2 = 1; break;
    default: break;  // 1 = full steps
  }
  const int pins[3] = {PIN_M0, PIN_M1, PIN_M2};
  const int levels[3] = {m0, m1, m2};
  for (int i = 0; i < 3; i++) {
    if (pins[i] < 0) continue;
    pinMode(pins[i], OUTPUT);
    digitalWrite(pins[i], levels[i] ? HIGH : LOW);
  }
}

void setup() {
  const int outputs[] = {PIN_X_DIR, PIN_X_STEP, PIN_X_EN, PIN_Y_DIR, PIN_Y_STEP, PIN_Y_EN, PIN_MAGNET};
  for (int pin : outputs) pinMode(pin, OUTPUT);
  set_microstep_pins();
  if (PIN_X_MIN >= 0) pinMode(PIN_X_MIN, INPUT_PULLUP);
  if (PIN_Y_MIN >= 0) pinMode(PIN_Y_MIN, INPUT_PULLUP);
  digitalWrite(PIN_X_STEP, LOW);
  digitalWrite(PIN_Y_STEP, LOW);
  hal.enable_motors(false);
  peripherals.magnet(false);
#if PIN_SERVO >= 0
  lift.attach(PIN_SERVO);
  lift.write(SERVO_UP_DEG);
#endif
  protocol.set_carry(CARRY_SPEED_MM_S, GRAB_MS, RELEASE_MS);
  Serial.begin(SERIAL_BAUD);
  delay(50);
  Serial.println();
  Serial.print("[");
  Serial.print(FIRMWARE_VERSION);
  Serial.println(" ready: $X to unlock, $H to home, $T to self-test, CARRY e2 e4 to move a piece]");
#if SELFTEST_AT_BOOT
  delay(5000);
  char test[] = "$T";
  protocol.handle_line(test, micros());
#endif
}

void loop() {
  motion.run();
  protocol.tick(micros());
  if (protocol.take_disable_request()) hal.enable_motors(false);
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      line[line_len] = 0;
      if (line_len) protocol.handle_line(line, micros());
      line_len = 0;
    } else if (c == '?' || c == '!') {
      // Real-time characters, answered even in the middle of a line, like GRBL.
      char one[2] = {c, 0};
      protocol.handle_line(one, micros());
    } else if (line_len < sizeof line - 1) {
      line[line_len++] = c;
    }
    motion.run();  // never let serial parsing starve the step timer
  }
}
