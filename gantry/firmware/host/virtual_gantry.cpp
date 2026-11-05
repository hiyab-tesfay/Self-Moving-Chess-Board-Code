// A pretend board for testing without hardware: the real motion.h and protocol.h, with pins
// replaced by counters and the clock replaced by a simulated one. It speaks the same serial
// language on stdin/stdout, so gantry_driver.py --port sim drives it exactly like the ESP32.
//
// Time runs `--speed` times faster than real time (default 20; 0 means every move finishes
// before the reply), which keeps tests quick while still letting the Pi side see "Run" when
// it polls. Lines starting with # are traces the Pi side logs and the tests read:
//   # SERVO up X=60.000 Y=40.000 t=1.234        the arm lifts the magnet to the board (down: clear of it)
//   # MAG on X=60.000 Y=40.000 t=1.534
//   # MOVE steps=400,0 to X=70.000 Y=40.000 took=0.512s peak=98.4mm/s
//   # MOTORS off t=31.000                        the drivers were switched off (idle, alarm, host lost)
//
// Options: --speed N, --endstops, --no-servo (a board without the arm, PIN_SERVO -1),
// --host-timeout MS (simulated milliseconds; default HOST_TIMEOUT_MS x speed, so that in wall-clock
// terms the pretend board waits for the host as long as the real one does).
//
//   c++ -std=c++17 -O2 -I../gantry_firmware virtual_gantry.cpp -o virtual_gantry
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <chrono>
#include <string>

#include "config.h"
#include "motion.h"
#include "protocol.h"

struct SimHal : gantry::Hal {
  uint32_t t = 0;
  long steps[2] = {0, 0};
  int dir[2] = {1, 1};
  long seg_steps[2] = {0, 0};
  uint32_t seg_start = 0, last_step = 0;
  float peak_steps_per_s = 0;
  bool endstop_wired = false;
  long endstop_at[2] = {0, 0};   // motor step count at which the switch presses (when wired)
  bool motors_on = false;

  void set_dir(int motor, bool positive) override { dir[motor] = positive ? 1 : -1; }
  void step(uint8_t mask) override {
    if (seg_steps[0] == 0 && seg_steps[1] == 0) seg_start = last_step = t;
    for (int m = 0; m < 2; m++)
      if (mask & (1 << m)) { steps[m] += dir[m]; seg_steps[m]++; }
    uint32_t dt = t - last_step;
    if (dt > 0 && (seg_steps[0] + seg_steps[1]) > 1) {
      float rate = 1000000.0f / dt;
      if (rate > peak_steps_per_s) peak_steps_per_s = rate;
    }
    last_step = t;
  }
  void enable_motors(bool on) override {
    if (on != motors_on) printf("# MOTORS %s t=%.3f\n", on ? "on" : "off", t / 1e6);
    motors_on = on;
  }
  bool endstop(int axis) override {
    if (!endstop_wired) return false;
    return steps[axis] <= endstop_at[axis];
  }
  uint32_t now_us() override { return t; }
};

struct SimPeripherals : gantry::Peripherals {
  bool on = false;
  int angle = SERVO_RETRACT_DEG;
  gantry::Motion* motion = nullptr;
  SimHal* hal = nullptr;
  void magnet(bool want) override {
    on = want;
    printf("# MAG %s X=%.3f Y=%.3f t=%.3f\n", want ? "on" : "off", motion->position_mm(gantry::X), motion->position_mm(gantry::Y), hal->t / 1e6);
  }
  bool magnet_on() override { return on; }
  void servo(int want) override {
    angle = want;
    printf("# SERVO %s X=%.3f Y=%.3f t=%.3f\n", want == SERVO_EXTEND_DEG ? "up" : "down", motion->position_mm(gantry::X), motion->position_mm(gantry::Y), hal->t / 1e6);
  }
};

struct StdoutWriter : gantry::Writer {
  void write(const char* text) override { fputs(text, stdout); }
};

int main(int argc, char** argv) {
  double speed = 20.0;
  bool endstops = false;
  bool servo = PIN_SERVO >= 0;
  long host_timeout_ms = -1;
  for (int i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--speed") && i + 1 < argc) speed = atof(argv[++i]);
    else if (!strcmp(argv[i], "--endstops")) endstops = true;
    else if (!strcmp(argv[i], "--no-servo")) servo = false;
    else if (!strcmp(argv[i], "--servo")) servo = true;
    else if (!strcmp(argv[i], "--host-timeout") && i + 1 < argc) host_timeout_ms = atol(argv[++i]);
  }
  if (host_timeout_ms < 0) host_timeout_ms = speed > 0 ? (long)(HOST_TIMEOUT_MS * speed) : 0;
  setvbuf(stdout, nullptr, _IOLBF, 0);

  gantry::Settings settings = {
      STEPS_PER_MM, {MAX_X_MM, MAX_Y_MM}, MAX_SPEED_MM_S, MAX_ACCEL_MM_S2, HOMING_SPEED_MM_S,
      HOMING_BUMP_MM, HOMING_PULL_OFF_MM, {INVERT_X_DIR, INVERT_Y_DIR}, COREXY, endstops, HOME_X_FIRST,
      MAX_STEP_HZ, IDLE_DISABLE_MS, ENABLE_SETTLE_MS, MOVE_BUDGET_FACTOR, MOVE_BUDGET_MARGIN_MS};
  SimHal hal;
  hal.endstop_wired = endstops;
  // With switches, pretend the carriage starts 30 mm from each end.
  hal.endstop_at[0] = hal.endstop_at[1] = (long)(-30 * STEPS_PER_MM);
  SimPeripherals peripherals;
  StdoutWriter writer;
  gantry::Motion motion(hal, settings);
  peripherals.motion = &motion;
  peripherals.hal = &hal;
  gantry::BoardGeometry board = {SQUARE_MM, A1_X_MM, A1_Y_MM, PARK_W_X_MM, PARK_B_X_MM, PARK_ROW0_MM, PARK_PITCH_MM, PARK_ROWS};
  gantry::Protocol protocol(motion, peripherals, writer, settings, board, FIRMWARE_VERSION " (virtual)");
  protocol.set_carry(CARRY_SPEED_MM_S, GRAB_MS, RELEASE_MS);
  protocol.set_servo(servo, SERVO_EXTEND_DEG, SERVO_RETRACT_DEG, SERVO_SETTLE_MS);
  protocol.set_guards(MAGNET_MAX_ON_MS, MAGNET_MIN_OFF_MS, (uint32_t)host_timeout_ms);

  printf("[%s ready: $X to unlock, $H to home%s]\n", FIRMWARE_VERSION " (virtual)", servo ? ", servo fitted" : "");

  auto wall = std::chrono::steady_clock::now();
  bool was_moving = false;
  auto advance = [&](uint32_t until) {
    while ((int32_t)(until - hal.t) > 0) {
      motion.run();
      // Report a finished move before the sequence takes its next step, so the traces read in order.
      bool moving = motion.moving();
      if (moving && !was_moving) { hal.seg_steps[0] = hal.seg_steps[1] = 0; hal.peak_steps_per_s = 0; }
      if (!moving && was_moving)
        printf("# MOVE steps=%ld,%ld to X=%.3f Y=%.3f took=%.3fs peak=%.1fmm/s\n", hal.seg_steps[0], hal.seg_steps[1],
               motion.position_mm(gantry::X), motion.position_mm(gantry::Y), (hal.last_step - hal.seg_start) / 1e6,
               hal.peak_steps_per_s / STEPS_PER_MM);
      was_moving = moving;
      protocol.tick(hal.t);
      hal.t += moving || protocol.busy() ? 5 : 1000;
    }
  };

  char line[256];
  while (fgets(line, sizeof line, stdin)) {
    auto now = std::chrono::steady_clock::now();
    double elapsed = std::chrono::duration<double>(now - wall).count();
    wall = now;
    if (speed <= 0) {
      while (motion.moving() || protocol.busy()) advance(hal.t + 100000);
    } else {
      advance(hal.t + (uint32_t)(elapsed * speed * 1e6));
    }
    protocol.handle_line(line, hal.t);
    if (speed <= 0) {
      while (motion.moving() || protocol.busy()) advance(hal.t + 100000);
    }
    fflush(stdout);
  }
  while (motion.moving() || protocol.busy()) advance(hal.t + 100000);
  advance(hal.t + 1);  // print the trace of a move that ended on the last step
  return 0;
}
