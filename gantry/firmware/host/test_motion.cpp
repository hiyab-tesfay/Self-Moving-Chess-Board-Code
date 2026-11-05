// Checks of motion.h and protocol.h on this computer: step counts, the speed profile, limits,
// homing, hold, CoreXY and the serial replies. Run with `make test` in this folder.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include <string>
#include <vector>

#include "config.h"
#include "motion.h"
#include "protocol.h"

using namespace gantry;

static int failures = 0, checks = 0;
#define CHECK(cond, ...) do { checks++; if (!(cond)) { failures++; printf("FAIL line %d: ", __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

struct FakeHal : Hal {
  uint32_t t = 0;
  long steps[2] = {0, 0};
  int dir[2] = {1, 1};
  std::vector<uint32_t> step_times;
  bool endstops = false;
  long pressed_below[2] = {-1000000, -1000000};
  void set_dir(int m, bool positive) override { dir[m] = positive ? 1 : -1; }
  void step(uint8_t mask) override {
    for (int m = 0; m < 2; m++) if (mask & (1 << m)) steps[m] += dir[m];
    step_times.push_back(t);
  }
  void enable_motors(bool) override {}
  bool endstop(int axis) override { return endstops && steps[axis] <= pressed_below[axis]; }
  uint32_t now_us() override { return t; }
};

static Settings settings(bool corexy = false, bool endstops = false) {
  return {STEPS_PER_MM, {MAX_X_MM, MAX_Y_MM}, MAX_SPEED_MM_S, MAX_ACCEL_MM_S2, HOMING_SPEED_MM_S,
          HOMING_BUMP_MM, HOMING_PULL_OFF_MM, {false, false}, corexy, endstops, true};
}

// Advance simulated time until the motion is idle. Returns the simulated seconds it took.
static double run_until_idle(FakeHal& hal, Motion& m, Protocol* p = nullptr) {
  uint32_t start = hal.t;
  long guard = 0;
  while (m.moving() || (p && p->busy())) {
    m.run();
    if (p) p->run(hal.t);
    hal.t += 5;
    if (++guard > 200000000) { CHECK(false, "move never finished"); break; }
  }
  return (hal.t - start) / 1e6;
}

// Ideal trapezoid time for a straight move of d mm.
static double ideal_time(double d, double vmax, double a) {
  double ramp = vmax * vmax / (2 * a);
  if (2 * ramp >= d) return 2 * sqrt(d / a);
  return 2 * vmax / a + (d - 2 * ramp) / vmax;
}

struct CaptureWriter : Writer {
  std::string text;
  void write(const char* s) override { text += s; }
};

struct FakePeripherals : Peripherals {
  bool on = false;
  void magnet(bool want) override { on = want; }
  bool magnet_on() override { return on; }
};

static void test_straight_move_counts_and_timing() {
  FakeHal hal;
  Motion m(hal, settings());
  m.unlock();
  CHECK(m.move_to(10, 0, 0) == OK, "move accepted");
  CHECK(m.segment_steps() == 400, "10 mm is 400 steps at 1/8, got %ld", m.segment_steps());
  double took = run_until_idle(hal, m);
  CHECK(hal.steps[0] == 400 && hal.steps[1] == 0, "steps %ld,%ld", hal.steps[0], hal.steps[1]);
  CHECK(fabs(m.position_mm(X) - 10) < 1e-3, "position %.3f", m.position_mm(X));
  double ideal = ideal_time(10, MAX_SPEED_MM_S, MAX_ACCEL_MM_S2);
  CHECK(took > ideal * 0.9 && took < ideal * 1.35, "10 mm took %.3fs, ideal %.3fs", took, ideal);
  // Never faster than the speed limit: the shortest interval between steps.
  uint32_t shortest = 1000000;
  for (size_t i = 1; i < hal.step_times.size(); i++) shortest = std::min(shortest, hal.step_times[i] - hal.step_times[i - 1]);
  CHECK(shortest >= (uint32_t)(1000000.0 / (MAX_SPEED_MM_S * STEPS_PER_MM)) - 1, "peak step interval %u us", shortest);
  // Accelerates then decelerates: intervals shrink in the first half, grow in the second.
  size_t n = hal.step_times.size();
  CHECK(hal.step_times[1] - hal.step_times[0] > hal.step_times[n / 2] - hal.step_times[n / 2 - 1], "ramps up");
  CHECK(hal.step_times[n - 1] - hal.step_times[n - 2] > hal.step_times[n / 2] - hal.step_times[n / 2 - 1], "ramps down");
}

static void test_long_move_cruises_at_max_speed() {
  FakeHal hal;
  Motion m(hal, settings());
  m.unlock();
  m.move_to(300, 0, 0);
  double took = run_until_idle(hal, m);
  double ideal = ideal_time(300, MAX_SPEED_MM_S, MAX_ACCEL_MM_S2);
  CHECK(fabs(took - ideal) < ideal * 0.1, "300 mm took %.3fs, ideal %.3fs", took, ideal);
}

static void test_diagonal_bresenham() {
  FakeHal hal;
  Motion m(hal, settings());
  m.unlock();
  m.move_to(10, 5, 0);
  run_until_idle(hal, m);
  CHECK(hal.steps[0] == 400 && hal.steps[1] == 200, "diagonal steps %ld,%ld", hal.steps[0], hal.steps[1]);
  m.move_to(0, 0, 0);
  run_until_idle(hal, m);
  CHECK(hal.steps[0] == 0 && hal.steps[1] == 0, "back home %ld,%ld", hal.steps[0], hal.steps[1]);
}

static void test_limits_busy_and_alarm() {
  FakeHal hal;
  Motion m(hal, settings());
  CHECK(m.move_to(10, 10, 0) == ERR_ALARM, "locked at power-up");
  m.unlock();
  CHECK(m.move_to(MAX_X_MM + 1, 0, 0) == ERR_LIMIT, "beyond X");
  CHECK(m.move_to(0, -1, 0) == ERR_LIMIT, "negative Y");
  CHECK(m.move_to(MAX_X_MM, MAX_Y_MM, 0) == OK, "the far corner is allowed");
  CHECK(m.move_to(1, 1, 0) == ERR_BUSY, "second move while running");
  run_until_idle(hal, m);
}

static void test_hold_then_resume() {
  FakeHal hal;
  Motion m(hal, settings());
  m.unlock();
  m.move_to(100, 0, 0);
  for (int i = 0; i < 100000 && hal.steps[0] < 1000; i++) { m.run(); hal.t += 5; }
  m.hold();
  CHECK(m.state() == HOLD, "held");
  long at = hal.steps[0];
  for (int i = 0; i < 1000; i++) { m.run(); hal.t += 5; }
  CHECK(hal.steps[0] == at, "no steps while held");
  CHECK(fabs(m.position_mm(X) - at / STEPS_PER_MM) < 1e-3, "position still counted");
  m.unlock();
  CHECK(m.move_to(0, 0, 0) == OK, "can move again");
  run_until_idle(hal, m);
  CHECK(hal.steps[0] == 0, "back to zero after hold");
}

static void test_bump_homing() {
  FakeHal hal;
  Motion m(hal, settings());
  m.unlock();
  m.move_to(100, 50, 0);
  run_until_idle(hal, m);
  hal.steps[0] = hal.steps[1] = 0;  // count only what homing does
  CHECK(m.home() == OK, "homing starts");
  run_until_idle(hal, m);
  CHECK(m.homed() && m.state() == IDLE, "homed and idle");
  CHECK(fabs(m.position_mm(X)) < 1e-3 && fabs(m.position_mm(Y)) < 1e-3, "at 0,0");
  // Without switches it drives the whole travel plus the bump, then pulls off.
  long expect_x = -(long)((MAX_X_MM + HOMING_BUMP_MM) * STEPS_PER_MM) + (long)(HOMING_PULL_OFF_MM * STEPS_PER_MM);
  long expect_y = -(long)((MAX_Y_MM + HOMING_BUMP_MM) * STEPS_PER_MM) + (long)(HOMING_PULL_OFF_MM * STEPS_PER_MM);
  CHECK(labs(hal.steps[0] - expect_x) <= 1 && labs(hal.steps[1] - expect_y) <= 1, "bump travel %ld,%ld expected %ld,%ld", hal.steps[0], hal.steps[1], expect_x, expect_y);
}

static void test_switch_homing_stops_at_the_switch() {
  FakeHal hal;
  hal.endstops = true;
  hal.pressed_below[0] = -(long)(30 * STEPS_PER_MM);
  hal.pressed_below[1] = -(long)(20 * STEPS_PER_MM);
  Motion m(hal, settings(false, true));
  CHECK(m.home() == OK, "homing from alarm is allowed");
  run_until_idle(hal, m);
  CHECK(m.homed(), "homed with switches");
  long pull = (long)(HOMING_PULL_OFF_MM * STEPS_PER_MM);
  CHECK(labs(hal.steps[0] - (hal.pressed_below[0] + pull)) <= 1, "X stopped at the switch: %ld", hal.steps[0]);
  CHECK(labs(hal.steps[1] - (hal.pressed_below[1] + pull)) <= 1, "Y stopped at the switch: %ld", hal.steps[1]);
  CHECK(fabs(m.position_mm(X)) < 1e-3 && fabs(m.position_mm(Y)) < 1e-3, "zeroed at the pull-off spot");
}

static void test_corexy() {
  FakeHal hal;
  Motion m(hal, settings(true));
  m.unlock();
  m.move_to(10, 0, 0);
  run_until_idle(hal, m);
  CHECK(hal.steps[0] == 400 && hal.steps[1] == 400, "CoreXY +X turns both motors the same way: %ld,%ld", hal.steps[0], hal.steps[1]);
  m.move_to(10, 10, 0);
  run_until_idle(hal, m);
  CHECK(hal.steps[0] == 800 && hal.steps[1] == 0, "CoreXY +Y turns them opposite: %ld,%ld", hal.steps[0], hal.steps[1]);
  CHECK(fabs(m.position_mm(X) - 10) < 1e-3 && fabs(m.position_mm(Y) - 10) < 1e-3, "cartesian position reads back");
}

static void test_protocol() {
  FakeHal hal;
  FakePeripherals per;
  CaptureWriter out;
  Settings s = settings();
  Motion m(hal, s);
  Protocol p(m, per, out, s, "test");
  auto send = [&](const char* text) { char buf[96]; strncpy(buf, text, sizeof buf - 1); buf[sizeof buf - 1] = 0; out.text.clear(); p.handle_line(buf, hal.t); return out.text; };
  CHECK(send("?").rfind("<Alarm:not homed|MPos:0.000,0.000|Mag:0|Homed:0>", 0) == 0, "alarm status: %s", out.text.c_str());
  CHECK(send("G0 X10") == "error:5 locked: $X to unlock, $H to home\n", "move while locked: %s", out.text.c_str());
  CHECK(send("$X") == "ok\n", "unlock");
  CHECK(send("g0 x10 y5") == "ok\n", "lower case move: %s", out.text.c_str());
  CHECK(send("?").rfind("<Run|", 0) == 0, "running: %s", out.text.c_str());
  run_until_idle(hal, m, &p);
  CHECK(send("?") == "<Idle|MPos:10.000,5.000|Mag:0|Homed:0>\n", "arrived: %s", out.text.c_str());
  CHECK(send("G0 X999") == "error:4 outside the travel limits\n", "limit: %s", out.text.c_str());
  CHECK(send("M3") == "ok\n" && per.on, "magnet on");
  CHECK(send("?").find("Mag:1") != std::string::npos, "magnet in status");
  CHECK(send("G4 P0.2") == "ok\n", "dwell");
  CHECK(send("G0 X0") == "error:3 busy\n", "no move during a dwell: %s", out.text.c_str());
  CHECK(send("?").rfind("<Run|", 0) == 0, "dwell shows as Run");
  hal.t += 250000;
  p.run(hal.t);
  CHECK(send("?").rfind("<Idle|", 0) == 0, "dwell over");
  CHECK(send("M5") == "ok\n" && !per.on, "magnet off");
  CHECK(send("G1 X20 F600") == "ok\n", "feed move");
  run_until_idle(hal, m, &p);
  CHECK(send("FOO") == "error:1 unknown command\n", "unknown: %s", out.text.c_str());
  CHECK(send("G92 X0 Y0") == "ok\n" && fabs(m.position_mm(X)) < 1e-3, "G92");
  CHECK(send("$$").find("$100=40.000 (steps/mm)") != std::string::npos, "settings: %s", out.text.c_str());
  CHECK(send("$H") == "ok\n", "home");
  run_until_idle(hal, m, &p);
  CHECK(send("?") == "<Idle|MPos:0.000,0.000|Mag:0|Homed:1>\n", "homed status: %s", out.text.c_str());
  CHECK(send("G0 X5") == "ok\n", "move");
  CHECK(send("!") == "ok\n" && m.state() == HOLD, "hold");
  CHECK(send("?").rfind("<Hold|", 0) == 0, "hold status: %s", out.text.c_str());
}

int main() {
  test_straight_move_counts_and_timing();
  test_long_move_cruises_at_max_speed();
  test_diagonal_bresenham();
  test_limits_busy_and_alarm();
  test_hold_then_resume();
  test_bump_homing();
  test_switch_homing_stops_at_the_switch();
  test_corexy();
  test_protocol();
  printf("%d checks, %d failures\n", checks, failures);
  return failures ? 1 : 0;
}
