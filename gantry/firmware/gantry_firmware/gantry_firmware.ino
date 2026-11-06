// DUEN chess board gantry firmware: two NEMA 17s on DRV8825s, an electromagnet and the MG90 arm
// that lifts it to the board, driven over USB serial by the Raspberry Pi (gantry/pi/gantry_driver.py).
// This file is only the glue to the board's pins; the movement itself is in motion.h and the serial
// language in protocol.h.
//
// Board: ESP32 Dev Module (same motor pins as Prototype_Program.ino) or, with GANTRY_CNC_SHIELD
// defined in config.h, an Arduino Uno/Nano on a CNC Shield v3. Open gantry_firmware.ino in
// the Arduino IDE; the other .h files in this folder come along.
#include <Arduino.h>

#include "config.h"
#include "motion.h"
#include "protocol.h"

// ----------------------------------------------------------------------------- the servo pulse
// 50 Hz, 500-2500 us. ESP32: an LEDC channel, no library. AVR: the Servo library that comes with
// the core (it uses Timer1 and a 50 Hz software schedule).
#if PIN_SERVO >= 0
#if defined(ESP32)
static const int SERVO_LEDC_CHANNEL = 0;      // the step pins are driven by hand, so every LEDC channel is free
static const int SERVO_LEDC_BITS = 16;
static void servo_begin() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_SERVO, 50, SERVO_LEDC_BITS);
#else
  ledcSetup(SERVO_LEDC_CHANNEL, 50, SERVO_LEDC_BITS);
  ledcAttachPin(PIN_SERVO, SERVO_LEDC_CHANNEL);
#endif
}
static void servo_write(int angle) {
  if (angle < 0) angle = 0;
  if (angle > 180) angle = 180;
  uint32_t pulse_us = SERVO_MIN_PULSE_US + (uint32_t)angle * (SERVO_MAX_PULSE_US - SERVO_MIN_PULSE_US) / 180UL;
  uint32_t duty = (uint32_t)(((uint64_t)pulse_us << SERVO_LEDC_BITS) / 20000ULL);   // 20 ms period
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PIN_SERVO, duty);
#else
  ledcWrite(SERVO_LEDC_CHANNEL, duty);
#endif
}
#else
#include <Servo.h>
static Servo lift;
static void servo_begin() { lift.attach(PIN_SERVO, SERVO_MIN_PULSE_US, SERVO_MAX_PULSE_US); }
static void servo_write(int angle) { lift.write(angle); }
#endif
#endif

// ----------------------------------------------------------------------------- the pins
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
  }
  bool magnet_on() override { return on; }
  void servo(int angle) override {
#if PIN_SERVO >= 0
    servo_write(angle);
#else
    (void)angle;
#endif
  }
};

struct SerialWriter : gantry::Writer {
  void write(const char* text) override { Serial.print(text); }
};

// The boot values. $100, $110, $111 and $120 change them in RAM until the next reset.
static const gantry::Settings SETTINGS = {
    STEPS_PER_MM,
    {MAX_X_MM, MAX_Y_MM},
    DEFAULT_SPEED_MM_S,
    DEFAULT_ACCEL_MM_S2,
    MAX_SPEED_MM_S,       // ceilings
    MAX_ACCEL_MM_S2,
    HOMING_SPEED_MM_S,
    HOMING_BUMP_MM,
    HOMING_PULL_OFF_MM,
    {INVERT_X_DIR, INVERT_Y_DIR},
    COREXY,
    PIN_X_MIN >= 0 && PIN_Y_MIN >= 0,
    HOME_X_FIRST,
    MAX_STEP_HZ,
    IDLE_DISABLE_MS,
    ENABLE_SETTLE_MS,
    MOVE_BUDGET_FACTOR,
    MOVE_BUDGET_MARGIN_MS,
};

static const gantry::BoardGeometry BOARD = {SQUARE_MM, A1_X_MM, A1_Y_MM, PARK_W_X_MM, PARK_B_X_MM, PARK_ROW0_MM, PARK_PITCH_MM, PARK_ROWS};

static BoardHal hal;
static BoardPeripherals peripherals;
static SerialWriter writer;
static gantry::Motion motion(hal, SETTINGS);
static gantry::Protocol protocol(motion, peripherals, writer, SETTINGS, BOARD, FIRMWARE_VERSION);

static char line[SERIAL_LINE_MAX];
static size_t line_len = 0;
static bool line_overflow = false;

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

// Run the step timer and the sequence until the board is still (only used at boot for the self-test).
static void run_until_still() {
  while (motion.moving() || protocol.busy()) {
    motion.run();
    protocol.tick(micros());
  }
}

void setup() {
  // 1. Every output safe before anything else: drivers disabled (EN high), magnet off, arm down.
  const int outputs[] = {PIN_X_DIR, PIN_X_STEP, PIN_X_EN, PIN_Y_DIR, PIN_Y_STEP, PIN_Y_EN, PIN_MAGNET};
  for (int pin : outputs) pinMode(pin, OUTPUT);
  digitalWrite(PIN_X_STEP, LOW);
  digitalWrite(PIN_Y_STEP, LOW);
  hal.enable_motors(false);
  peripherals.magnet(false);
#if PIN_SERVO >= 0
  servo_begin();
  servo_write(SERVO_RETRACT_DEG);
#endif
  set_microstep_pins();
  if (PIN_X_MIN >= 0) pinMode(PIN_X_MIN, INPUT_PULLUP);
  if (PIN_Y_MIN >= 0) pinMode(PIN_Y_MIN, INPUT_PULLUP);

  // 2. The numbers the protocol enforces.
  protocol.set_carry(CARRY_SPEED_MM_S, GRAB_MS, RELEASE_MS);
  protocol.set_servo(PIN_SERVO >= 0, SERVO_EXTEND_DEG, SERVO_RETRACT_DEG, SERVO_SETTLE_MS);
  protocol.set_guards(MAGNET_MAX_ON_MS, MAGNET_MIN_OFF_MS, HOST_TIMEOUT_MS);

  // 3. Settle, then listen. Anything the Pi sent while the ESP32 was resetting is dropped.
  delay(BOOT_SETTLE_MS);
#if defined(ESP32)
  // A transmit ring buffer, so that a status reply or $$ is queued and sent by the UART driver
  // while loop() goes on stepping. Without one, Serial.print waits for room in the 128-byte FIFO
  // (about 11 ms for a full one at 115200), and the motors stutter or stall mid-move.
  Serial.setTxBufferSize(512);
#endif
  Serial.begin(SERIAL_BAUD);
  delay(50);
  while (Serial.available()) Serial.read();
  Serial.println();
  Serial.print("[");
  Serial.print(FIRMWARE_VERSION);
  Serial.println(" ready: $X to unlock, $H to home, $T to self-test, CARRY e2 e4 to move a piece]");
#if SELFTEST_AT_BOOT
  delay(5000);
  char home[] = "$H";
  protocol.handle_line(home, micros());
  run_until_still();
  char test[] = "$T";
  protocol.handle_line(test, micros());
#endif
}

void loop() {
  motion.run();
  protocol.tick(micros());
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      line[line_len] = 0;
      if (line_overflow) {
        Serial.println("error:1 line too long");   // never act on a truncated command
      } else if (line_len) {
        protocol.handle_line(line, micros());
      }
      line_len = 0;
      line_overflow = false;
    } else if (c == '?' || c == '!') {
      // Real-time characters, answered even in the middle of a line, like GRBL.
      char one[2] = {c, 0};
      protocol.handle_line(one, micros());
    } else if (line_len < sizeof line - 1) {
      line[line_len++] = c;
    } else {
      line_overflow = true;
    }
    motion.run();  // never let serial parsing starve the step timer
  }
}
