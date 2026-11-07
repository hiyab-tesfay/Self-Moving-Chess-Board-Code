// Checks of motion.h and protocol.h on this computer: step counts, the speed profile, limits,
// homing, hold, CoreXY, the serial replies, the servo sequence and the safety guards. Run with
// `make test` in this folder.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include <string>
#include <vector>

#include "config.h"
#include "motion.h"
#include "protocol.h"
#include "servo_arm.h"

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
  bool enabled = false;
  int enable_changes = 0;
  uint32_t pulse = 0;
  void set_step_pulse_us(uint32_t us) override { pulse = us; }
  void set_dir(int m, bool positive) override { dir[m] = positive ? 1 : -1; }
  void step(uint8_t mask) override {
    for (int m = 0; m < 2; m++) if (mask & (1 << m)) steps[m] += dir[m];
    step_times.push_back(t);
  }
  void enable_motors(bool on) override { if (on != enabled) enable_changes++; enabled = on; }
  bool endstop(int axis) override { return endstops && steps[axis] <= pressed_below[axis]; }
  uint32_t now_us() override { return t; }
};

// The motion checks run at 40 steps/mm (1/8 steps on a 40 mm pulley turn), a fine resolution that
// shows the profile clearly; the bench's own 4.878 (full steps, 41 mm per turn) has its own test,
// test_bench_steps_and_travel. Otherwise the numbers the sketch boots with.
static const float TEST_STEPS_PER_MM = 40.0f;
// Step pulse 0 (no cap from it) unless asked: most checks are about the profile at full speed.
static Settings settings(bool corexy = false, bool endstops = false, float max_step_hz = MAX_STEP_HZ, uint32_t pulse_us = 0,
                         float steps_per_mm = TEST_STEPS_PER_MM) {
  return {steps_per_mm, {MAX_X_MM, MAX_Y_MM}, DEFAULT_SPEED_MM_S, DEFAULT_ACCEL_MM_S2, MAX_SPEED_MM_S, MAX_ACCEL_MM_S2, HOMING_SPEED_MM_S,
          HOMING_BUMP_MM, HOMING_PULL_OFF_MM, {false, false}, corexy, endstops, true,
          max_step_hz, IDLE_DISABLE_MS, ENABLE_SETTLE_MS, MOVE_BUDGET_FACTOR, MOVE_BUDGET_MARGIN_MS, pulse_us};
}

// Advance simulated time until the motion is idle. Returns the simulated seconds it took.
static double run_until_idle(FakeHal& hal, Motion& m, Protocol* p = nullptr) {
  uint32_t start = hal.t;
  long guard = 0;
  while (m.moving() || (p && p->busy())) {
    m.run();
    if (p) p->tick(hal.t);
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

// Records what the magnet and the arm did, and where the carriage stood (Y in mm) at the time, so a
// test can read the order of events against the moves; event_us has the simulated time of each.
struct FakePeripherals : Peripherals {
  bool on = false;
  int toggles = 0;
  int angle = SERVO_RETRACT_DEG;
  Motion* motion = nullptr;
  FakeHal* hal = nullptr;
  std::vector<std::pair<std::string, double>> events;
  std::vector<uint32_t> event_us;
  void magnet(bool want) override {
    if (want != on) toggles++;
    on = want;
    events.push_back({want ? "mag on" : "mag off", motion ? motion->position_mm(Y) : 0.0});
    event_us.push_back(hal ? hal->t : 0);
  }
  bool magnet_on() override { return on; }
  void servo(int want) override {
    angle = want;
    events.push_back({want == SERVO_EXTEND_DEG ? "arm up" : "arm down", motion ? motion->position_mm(Y) : 0.0});
    event_us.push_back(hal ? hal->t : 0);
  }
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
  double ideal = ideal_time(10, DEFAULT_SPEED_MM_S, DEFAULT_ACCEL_MM_S2);
  CHECK(took > ideal * 0.9 && took < ideal * 1.35, "10 mm took %.3fs, ideal %.3fs", took, ideal);
  // Never faster than the speed limit: the shortest interval between steps.
  uint32_t shortest = 1000000;
  for (size_t i = 1; i < hal.step_times.size(); i++) shortest = std::min(shortest, hal.step_times[i] - hal.step_times[i - 1]);
  CHECK(shortest >= (uint32_t)(1000000.0 / (DEFAULT_SPEED_MM_S * TEST_STEPS_PER_MM)) - 1, "peak step interval %u us", shortest);
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
  double ideal = ideal_time(300, DEFAULT_SPEED_MM_S, DEFAULT_ACCEL_MM_S2);
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
  CHECK(fabs(m.position_mm(X) - at / TEST_STEPS_PER_MM) < 1e-3, "position still counted");
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
  long expect_x = -(long)((MAX_X_MM + HOMING_BUMP_MM) * TEST_STEPS_PER_MM) + (long)(HOMING_PULL_OFF_MM * TEST_STEPS_PER_MM);
  long expect_y = -(long)((MAX_Y_MM + HOMING_BUMP_MM) * TEST_STEPS_PER_MM) + (long)(HOMING_PULL_OFF_MM * TEST_STEPS_PER_MM);
  CHECK(labs(hal.steps[0] - expect_x) <= 1 && labs(hal.steps[1] - expect_y) <= 1, "bump travel %ld,%ld expected %ld,%ld", hal.steps[0], hal.steps[1], expect_x, expect_y);
}

static void test_switch_homing_stops_at_the_switch() {
  FakeHal hal;
  hal.endstops = true;
  hal.pressed_below[0] = -(long)(30 * TEST_STEPS_PER_MM);
  hal.pressed_below[1] = -(long)(20 * TEST_STEPS_PER_MM);
  Motion m(hal, settings(false, true));
  CHECK(m.home() == OK, "homing from alarm is allowed");
  run_until_idle(hal, m);
  CHECK(m.homed(), "homed with switches");
  long pull = (long)(HOMING_PULL_OFF_MM * TEST_STEPS_PER_MM);
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

// ------------------------------------------------------------------ the safety guards in motion.h

static void test_drivers_rest_when_idle_and_wake_with_a_settle() {
  FakeHal hal;
  Motion m(hal, settings());
  CHECK(!hal.enabled && !m.motors_enabled(), "drivers off at boot");
  m.unlock();
  m.move_to(10, 0, 0);
  CHECK(hal.enabled, "drivers on for a move");
  CHECK(hal.step_times.empty(), "no step yet");
  uint32_t asked = hal.t;
  run_until_idle(hal, m);
  CHECK(hal.step_times[0] - asked >= ENABLE_SETTLE_MS * 1000UL, "first step waits %d ms for the driver: %u us", ENABLE_SETTLE_MS, hal.step_times[0] - asked);
  CHECK(hal.enabled, "still on just after the move");
  hal.t += IDLE_DISABLE_MS * 1000UL - 1000;
  m.run();
  CHECK(hal.enabled, "still on just before the idle limit");
  hal.t += 2000;
  m.run();
  CHECK(!hal.enabled && !m.motors_enabled(), "off after %d ms idle", IDLE_DISABLE_MS);
  hal.step_times.clear();
  asked = hal.t;
  m.move_to(20, 0, 0);
  run_until_idle(hal, m);
  CHECK(hal.enabled && hal.step_times[0] - asked >= ENABLE_SETTLE_MS * 1000UL, "woke with a settle before stepping");
  m.disable_motors();
  CHECK(!hal.enabled, "M18 switches them off at once");
}

static void test_step_rate_cap() {
  FakeHal hal;
  Motion m(hal, settings(false, false, 1000.0f));   // 1 kHz: 25 mm/s at 40 steps/mm
  m.unlock();
  m.move_to(100, 0, 0);
  run_until_idle(hal, m);
  uint32_t shortest = 1000000;
  for (size_t i = 1; i < hal.step_times.size(); i++) shortest = std::min(shortest, hal.step_times[i] - hal.step_times[i - 1]);
  CHECK(shortest >= 999, "no step interval under 1 ms at a 1 kHz cap: %u us", shortest);
  CHECK(hal.steps[0] == 4000, "the move still arrives: %ld steps", hal.steps[0]);
  CHECK(fabs(m.step_capped(120.0f) - 25.0f) < 1e-3, "the feed is capped to %.1f mm/s", m.step_capped(120.0f));
  Motion wide(hal, settings());
  CHECK(fabs(wide.step_capped(120.0f) - 120.0f) < 1e-3, "20 kHz leaves 120 mm/s alone");
}

static void test_move_budget_catches_a_starved_loop() {
  FakeHal hal;
  Motion m(hal, settings());
  m.unlock();
  m.move_to(100, 0, 0);
  double planned = ideal_time(100, DEFAULT_SPEED_MM_S, DEFAULT_ACCEL_MM_S2);
  uint32_t budget = m.segment_budget_us();
  CHECK(budget > planned * MOVE_BUDGET_FACTOR * 1e6 * 0.9 && budget < (planned * MOVE_BUDGET_FACTOR + MOVE_BUDGET_MARGIN_MS / 1000.0) * 1e6 * 1.1 + 100000,
        "budget %.2fs for a %.2fs move", budget / 1e6, planned);
  CHECK(fabs(m.move_seconds(100, DEFAULT_SPEED_MM_S) - planned) < 0.01, "move_seconds matches the trapezoid: %.3f vs %.3f", m.move_seconds(100, DEFAULT_SPEED_MM_S), planned);
  for (int i = 0; i < 4000; i++) { m.run(); hal.t += 5; }   // 20 ms: the settle and the first, slowest steps
  CHECK(m.moving() && hal.steps[0] > 0, "under way");
  hal.t += budget;   // nobody called run() for a very long time
  m.run();
  CHECK(m.state() == ALARM && !strcmp(m.alarm_reason(), "move took too long"), "alarm: %s", m.alarm_reason());
  CHECK(!hal.enabled && !m.homed(), "drivers off and position forgotten");
  // A normal move never trips it.
  m.unlock();
  m.move_to(0, 0, 0);
  run_until_idle(hal, m);
  CHECK(m.state() == IDLE, "a move that runs on time finishes");
}

// ------------------------------------------------------------------ the protocol

// The board as sketch 5 draws it (a1 at 60, lanes at 17.5 and 452.5): the board checks below were
// written against it. config.h's defaults (the board slid right for the magnet's offset) are
// checked in test_bench_steps_and_travel.
static BoardGeometry board_geometry() {
  return {50.0f, 60.0f, 35.0f, 17.5f, 452.5f, 17.5f, 35.0f, PARK_ROWS, 0.0f};
}

// Advance simulated time until the sequence and the motion are done.
static double run_program(FakeHal& hal, Motion& m, Protocol& p) {
  uint32_t start = hal.t;
  long guard = 0;
  while (p.busy()) {
    m.run();
    p.tick(hal.t);
    hal.t += 5;
    if (++guard > 400000000) { CHECK(false, "sequence never finished"); break; }
  }
  return (hal.t - start) / 1e6;
}

struct Rig {
  FakeHal hal;
  FakePeripherals per;
  CaptureWriter out;
  Settings s;
  Motion m;
  Protocol p;
  Rig(bool servo = false) : s(settings()), m(hal, s), p(m, per, out, s, board_geometry(), "test") {
    per.motion = &m;
    per.hal = &hal;
    p.set_carry(CARRY_SPEED_MM_S, GRAB_MS, RELEASE_MS);
    p.set_servo(servo, SERVO_EXTEND_DEG, SERVO_RETRACT_DEG, SERVO_SETTLE_MS);
    p.set_guards(MAGNET_MAX_ON_MS, MAGNET_MIN_OFF_MS, HOST_TIMEOUT_MS);
  }
  std::string send(const char* text) {
    char buf[200];
    strncpy(buf, text, sizeof buf - 1);
    buf[sizeof buf - 1] = 0;
    out.text.clear();
    p.handle_line(buf, hal.t);
    return out.text;
  }
  std::string pos() {
    char buf[40];
    snprintf(buf, sizeof buf, "%.3f,%.3f", m.position_mm(X), m.position_mm(Y));
    return buf;
  }
  void home() { send("$H"); run_program(hal, m, p); }
};

static void test_manual_home_declares_the_corner_without_moving() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  // Straight out of the power-up alarm: the carriage was pushed into the corner by hand.
  CHECK(send("$HM") == "ok\n", "manual home is accepted while locked: %s", r.out.text.c_str());
  CHECK(r.m.homed(), "homed without a seek");
  CHECK(r.hal.steps[0] == 0 && r.hal.steps[1] == 0, "nothing moved: %ld,%ld", r.hal.steps[0], r.hal.steps[1]);
  CHECK(r.pos() == "0.000,0.000", "the spot is 0,0: %s", r.pos().c_str());
  CHECK(send("?").find("|Homed:1") != std::string::npos, "status says homed: %s", r.out.text.c_str());
  CHECK(send("GOTO e4") == "ok\n", "board commands work from a manual home: %s", r.out.text.c_str());
  run_program(r.hal, r.m, r.p);
  CHECK(r.pos() == "260.000,185.000", "and land on e4: %s", r.pos().c_str());
  CHECK(send("$HM") == "ok\n", "declaring a new corner later is allowed when idle");
  CHECK(r.pos() == "0.000,0.000", "and resets the count: %s", r.pos().c_str());
}

static void test_board_commands() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  auto pos = [&]() { return r.pos(); };
  Motion& m = r.m;
  Protocol& p = r.p;
  FakeHal& hal = r.hal;
  FakePeripherals& per = r.per;

  CHECK(send("GOTO E4") == "error:5 locked: $X to unlock, $H to home\n", "locked: %s", r.out.text.c_str());
  send("$X");
  CHECK(send("GOTO e4") == "error:8 not homed: $H first\n", "board commands need a real position: %s", r.out.text.c_str());
  CHECK(send("CARRY e2 e4") == "error:8 not homed: $H first\n", "so does a carry: %s", r.out.text.c_str());
  CHECK(send("$T") == "error:8 not homed: $H first\n", "and the self-test: %s", r.out.text.c_str());
  r.home();
  CHECK(m.homed(), "homed");
  CHECK(send("GOTO e4") == "ok\n", "goto a square: %s", r.out.text.c_str());
  CHECK(send("?").rfind("<Run|", 0) == 0 && send("?").find("|Prog:1/1>") != std::string::npos, "status shows the sequence: %s", r.out.text.c_str());
  run_program(hal, m, p);
  CHECK(pos() == "260.000,185.000", "e4 is a1 (60,35) + (4,3) squares of 50: %s", pos().c_str());
  CHECK(send("GOTO w:2") == "ok\n", "goto a docking slot"); run_program(hal, m, p);
  CHECK(pos() == "17.500,87.500", "White's lane, third slot at 35 mm pitch: %s", pos().c_str());
  CHECK(send("GOTO 5.5,0.5") == "ok\n", "goto a lane crossing in squares"); run_program(hal, m, p);
  CHECK(pos() == "335.000,60.000", "lane crossing: %s", pos().c_str());
  CHECK(send("GOTO -0.5,4") == "ok\n", "goto the lane beside the a file"); run_program(hal, m, p);
  CHECK(pos() == "38.750,235.000", "halfway between the docking lane (17.5) and the a file (60): %s", pos().c_str());
  CHECK(send("GOTO i9") == "error:6 not a square, slot or x,y\n", "bad place: %s", r.out.text.c_str());
  CHECK(send("GOTO w:12") == "error:6 not a square, slot or x,y\n", "slot past the lane: %s", r.out.text.c_str());

  // A straight carry: travel to e2, magnet on, dwell, carry to e4, magnet off, dwell (no servo on this rig).
  int toggles_before = per.toggles;
  CHECK(send("CARRY e2 e4") == "ok\n", "carry: %s", r.out.text.c_str());
  CHECK(p.program_length() == 6, "straight carry is 6 steps, got %d", p.program_length());
  CHECK(p.program_step(0).type == STEP_MOVE && p.program_step(0).speed == r.s.max_speed, "first: travel to the piece");
  CHECK(p.program_step(3).type == STEP_MOVE && p.program_step(3).speed == CARRY_SPEED_MM_S, "the carry itself runs at carry speed");
  double took = run_program(hal, m, p);
  CHECK(pos() == "260.000,185.000" && !per.on && per.toggles == toggles_before + 2, "ends on e4 with the magnet off: %s", pos().c_str());
  CHECK(took > 0.5 + 0.3 + 0.2, "includes the grab and release dwells: %.2fs", took);

  // A knight's carry routes itself: g1 -> corner (5.5,0.5) -> up the lane (5.5,1.5) -> f3.
  CHECK(send("CARRY g1 f3") == "ok\n", "knight carry");
  CHECK(p.program_length() == 8, "knight carry is 8 steps, got %d", p.program_length());
  CHECK(p.program_step(3).x == 335 && p.program_step(3).y == 60, "out to the corner: %.1f,%.1f", p.program_step(3).x, p.program_step(3).y);
  CHECK(p.program_step(4).x == 335 && p.program_step(4).y == 110, "up the lane: %.1f,%.1f", p.program_step(4).x, p.program_step(4).y);
  CHECK(p.program_step(5).x == 310 && p.program_step(5).y == 135, "in to f3: %.1f,%.1f", p.program_step(5).x, p.program_step(5).y);
  run_program(hal, m, p);
  CHECK(pos() == "310.000,135.000", "knight ends on f3: %s", pos().c_str());

  // Via-points from the Pi override the board's own route.
  CHECK(send("CARRY d5 w:0 2.5,4.5 -0.5,4.5") == "ok\n", "carry with via-points");
  CHECK(p.program_length() == 8 && p.program_step(4).x == 38.75f && p.program_step(5).x == 17.5f && p.program_step(5).y == 17.5f, "via the lane beside the a file into slot 0: %.2f / %.2f,%.2f", p.program_step(4).x, p.program_step(5).x, p.program_step(5).y);
  run_program(hal, m, p);
  CHECK(pos() == "17.500,17.500", "a taken piece lands in the docking lane: %s", pos().c_str());
  CHECK(send("CARRY d5 w:0 2.5,4.5 bogus") == "error:6 not a square, slot or x,y\n", "a bad via-point refuses the whole carry: %s", r.out.text.c_str());
  CHECK(p.program_length() == 0 && !p.busy(), "and queues nothing");

  // $B replaces the geometry; $$ shows it.
  CHECK(send("$B=40,50,30") == "ok\n", "set square and a1");
  CHECK(send("GOTO h8") == "ok\n", "goto after $B"); run_program(hal, m, p);
  CHECK(pos() == "330.000,310.000", "h8 with 40 mm squares from (50,30): %s", pos().c_str());
  CHECK(send("$B=0,1,2") == "error:2 bad number\n", "a zero square is refused");
  CHECK(send("$$").find("$B=40.000,50.000,30.000") != std::string::npos, "settings show the board: %s", r.out.text.c_str());
  {
    char want[64];
    snprintf(want, sizeof want, "$G=%d,%d,%d", (int)MAGNET_MAX_ON_MS, (int)MAGNET_MIN_OFF_MS, (int)HOST_TIMEOUT_MS);
    CHECK(send("$$").find(want) != std::string::npos, "settings show the guards (%s): %s", want, r.out.text.c_str());
  }
  send("$B=50,60,35,17.5,452.5,17.5,35");

  // The self-test visits the corners, works the magnet and does a knight's carry there and back.
  std::string selftest = send("$T");
  CHECK(selftest.rfind("# selftest:", 0) == 0 && selftest.find("\nok\n") != std::string::npos, "self-test accepted with a trace: %s", selftest.c_str());
  CHECK(p.program_length() > 20, "self-test is a long sequence: %d", p.program_length());
  run_program(hal, m, p);
  CHECK(m.homed() && pos() == "60.000,35.000", "self-test ends homed, back on a1: %s", pos().c_str());

  // ! stops a sequence in mid-move and drops the rest of it.
  send("GOTO h1"); run_program(hal, m, p);
  CHECK(send("CARRY a1 h8") == "ok\n" && m.moving(), "the travel to a1 is under way");
  CHECK(send("!") == "ok\n" && !p.busy() && m.state() == HOLD, "stop drops the sequence");
  CHECK(send("?").rfind("<Hold|", 0) == 0, "hold: %s", r.out.text.c_str());

  // A sequence that would leave the travel is refused before anything moves.
  send("$X");
  send("$B=50,60,35,17.5,452.5,17.5,35");
  CHECK(send("GOTO 9.5,0") == "error:4 outside the travel limits\n", "beyond the far lane: %s", r.out.text.c_str());
}

static void test_protocol() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  Motion& m = r.m;
  Protocol& p = r.p;
  FakeHal& hal = r.hal;
  FakePeripherals& per = r.per;
  CHECK(send("?").rfind("<Alarm:not homed|MPos:0.000,0.000|Mag:0|Homed:0>", 0) == 0, "alarm status: %s", r.out.text.c_str());
  CHECK(send("G0 X10") == "error:5 locked: $X to unlock, $H to home\n", "move while locked: %s", r.out.text.c_str());
  CHECK(send("$X") == "ok\n", "unlock");
  CHECK(send("g0 x10 y5") == "ok\n", "a jog after $X is allowed before homing (the first power-up checks need it): %s", r.out.text.c_str());
  CHECK(send("?").rfind("<Run|", 0) == 0, "running: %s", r.out.text.c_str());
  run_until_idle(hal, m, &p);
  CHECK(send("?") == "<Idle|MPos:10.000,5.000|Mag:0|Homed:0>\n", "arrived: %s", r.out.text.c_str());
  CHECK(send("G0 X999") == "error:4 outside the travel limits (not homed: $H first)\n", "limit, and it says why the limits are all there is: %s", r.out.text.c_str());
  CHECK(send("M3") == "error:8 not homed: $H first\n", "no magnet before homing: %s", r.out.text.c_str());
  CHECK(send("G1 X20 F600") == "ok\n", "feed move");
  run_until_idle(hal, m, &p);
  CHECK(send("FOO") == "error:1 unknown command\n", "unknown: %s", r.out.text.c_str());
  CHECK(send("G92 X0 Y0") == "ok\n" && fabs(m.position_mm(X)) < 1e-3, "G92");
  CHECK(send("$$").find("$100=40.000 (steps/mm)") != std::string::npos, "settings: %s", r.out.text.c_str());
  CHECK(send("$H") == "ok\n", "home");
  run_until_idle(hal, m, &p);
  CHECK(send("?") == "<Idle|MPos:0.000,0.000|Mag:0|Homed:1>\n", "homed status: %s", r.out.text.c_str());
  CHECK(send("M3") == "ok\n" && per.on, "magnet on once homed");
  CHECK(send("?").find("Mag:1") != std::string::npos, "magnet in status");
  CHECK(send("G4 P0.2") == "ok\n", "dwell");
  CHECK(send("G0 X0") == "error:3 busy\n", "no move during a dwell: %s", r.out.text.c_str());
  CHECK(send("?").rfind("<Run|", 0) == 0, "dwell shows as Run");
  hal.t += 250000;
  p.tick(hal.t);
  CHECK(send("?").rfind("<Idle|", 0) == 0, "dwell over");
  CHECK(send("M5") == "ok\n" && !per.on, "magnet off");
  CHECK(send("G0 X5") == "ok\n", "move");
  CHECK(send("M3") == "error:3 busy\n", "no magnet while moving: %s", r.out.text.c_str());
  CHECK(send("!") == "ok\n" && m.state() == HOLD, "hold");
  CHECK(send("?").rfind("<Hold|", 0) == 0, "hold status: %s", r.out.text.c_str());
  CHECK(send("M18") == "ok\n" && !hal.enabled, "M18 switches the drivers off");
}

// ------------------------------------------------------------------ the servo arm

// servo_arm.h (0.7.2): the pulse sweeps instead of jumping, and the arm is let go once down.
static std::vector<uint32_t> g_pulses;
static void record_pulse(uint32_t us) { g_pulses.push_back(us); }

static void test_servo_arm_drive() {
  g_pulses.clear();
  gantry::ServoArm arm(record_pulse, 500, 2500, 360.0f, 500, 0);
  uint32_t t = 1000;
  arm.start(0, t);
  CHECK(g_pulses.size() == 1 && g_pulses[0] == 500, "power-up: the down pulse at once");
  for (int i = 0; i < 400; i++) { t += 1000; arm.tick(t, true); }
  CHECK(arm.pulsing(), "still held 0.4 s after power-up");
  for (int i = 0; i < 300; i++) { t += 1000; arm.tick(t, true); }
  CHECK(!arm.pulsing() && g_pulses.back() == 0, "let go once down for half a second: last pulse %u", g_pulses.back());

  g_pulses.clear();
  uint32_t t0 = t, reached = 0, biggest = 0;
  arm.go(90, t);
  CHECK(g_pulses.size() == 1 && g_pulses[0] == 500, "M7 starts from where the arm rests: %u", g_pulses[0]);
  for (int i = 0; i < 2000; i++) {
    t += 1000;
    arm.tick(t, false);
    if (!reached && arm.angle() == 90.0f) reached = t - t0;
  }
  for (size_t i = 1; i < g_pulses.size(); i++) {
    uint32_t d = g_pulses[i] > g_pulses[i - 1] ? g_pulses[i] - g_pulses[i - 1] : g_pulses[i - 1] - g_pulses[i];
    if (d > biggest) biggest = d;
  }
  CHECK(biggest <= 25, "the pulse never jumps more than ~2 deg at a time: %u us", biggest);
  CHECK(reached >= 240000 && reached <= 260000, "90 deg in about 0.25 s: %u us", reached);
  CHECK(arm.pulsing() && g_pulses.back() == 1500, "held up (1500 us) as long as it is up: %u", g_pulses.back());

  g_pulses.clear();
  arm.go(0, t);
  for (int i = 0; i < 1000; i++) { t += 1000; arm.tick(t, true); }
  CHECK(g_pulses.size() > 40 && g_pulses[g_pulses.size() - 2] == 500 && g_pulses.back() == 0 && !arm.pulsing(),
        "M8 sweeps down, then lets go");
  g_pulses.clear();
  arm.go(10, t);
  for (int i = 0; i < 1000; i++) { t += 1000; arm.tick(t, true); }
  CHECK(g_pulses.front() == 500 && !arm.pulsing() && g_pulses.back() == 0, "a new down angle ($S) sweeps there from rest, then lets go");

  g_pulses.clear();
  gantry::ServoArm held(record_pulse, 500, 2500, 0.0f, 0, 0);
  held.start(0, t);
  held.go(180, t);
  for (int i = 0; i < 2000; i++) { t += 1000; held.tick(t, i > 1000); }
  CHECK(held.angle() == 180.0f && g_pulses.back() == 2500 && held.pulsing(), "sweep 0 jumps, release 0 never lets go");
  CHECK(arm.pulse_for(-5) == 500 && arm.pulse_for(200) == 2500 && arm.pulse_for(90) == 1500, "pulse widths clamp to 0-180 deg");
}

// $S=up,down[,settle] (0.7.2): the arm's angles found on the real arm, without a reflash.
static void test_servo_angles() {
  Rig r(true);
  auto send = [&](const char* t) { return r.send(t); };
  Protocol& p = r.p;
  FakePeripherals& per = r.per;
  send("$X");
  r.home();
  CHECK(send("$S=80,10") == "ok\n" && per.angle == 10 && !p.servo_up(), "$S sets the angles and the arm goes to the new down: %d", per.angle);
  send("$$");
  CHECK(r.out.text.find("$S=80,10,400 (servo up deg") != std::string::npos, "$$ shows them: %s", r.out.text.c_str());
  CHECK(send("M7") == "ok\n" && per.angle == 80, "M7 uses the new up angle: %d", per.angle);
  CHECK(send("$S=70,5") == "error:11 servo arm up: M8 first\n", "not while the arm is up: %s", r.out.text.c_str());
  CHECK(send("M8") == "ok\n" && per.angle == 10, "M8 the new down angle: %d", per.angle);
  CHECK(send("$S=85,15,600") == "ok\n", "with a settle time");
  send("$$");
  CHECK(r.out.text.find("$S=85,15,600 (") != std::string::npos, "settle set: %s", r.out.text.c_str());
  CHECK(send("$S=200,0").rfind("error:13", 0) == 0, "angles stop at 180: %s", r.out.text.c_str());
  CHECK(send("$S=90,0,5000").rfind("error:13", 0) == 0, "settle stops at 3000 ms: %s", r.out.text.c_str());
  CHECK(send("$S=80").rfind("error:2", 0) == 0, "both angles needed: %s", r.out.text.c_str());
  CHECK(send("$S=A,B").rfind("error:2", 0) == 0, "numbers only: %s", r.out.text.c_str());
  CHECK(send("$S=1,2,3,4").rfind("error:2", 0) == 0, "three numbers at most: %s", r.out.text.c_str());
  CHECK(send("$S=80,10,").rfind("error:2", 0) == 0, "no trailing comma: %s", r.out.text.c_str());
  send("$$");
  CHECK(r.out.text.find("$S=85,15,600 (") != std::string::npos, "refusals change nothing: %s", r.out.text.c_str());
  Rig plain(false);
  CHECK(plain.send("$S=80,10").rfind("error:1", 0) == 0, "no $S without a servo: %s", plain.out.text.c_str());
}

static void test_servo_sequence() {
  Rig r(true);
  auto send = [&](const char* t) { return r.send(t); };
  Motion& m = r.m;
  Protocol& p = r.p;
  FakeHal& hal = r.hal;
  FakePeripherals& per = r.per;
  send("$X");
  CHECK(send("?").find("|Servo:down>") != std::string::npos, "status shows the arm: %s", r.out.text.c_str());
  r.home();
  CHECK(send("M3") == "error:12 servo arm down: M7 first\n", "magnet needs the arm up: %s", r.out.text.c_str());
  CHECK(send("M7") == "ok\n" && p.servo_up() && per.angle == SERVO_EXTEND_DEG, "M7 lifts the arm");
  CHECK(send("?").find("|Servo:up>") != std::string::npos, "status: %s", r.out.text.c_str());
  CHECK(send("G0 X100") == "error:11 servo arm up: M8 first\n", "no rapid with the arm up: %s", r.out.text.c_str());
  CHECK(send("GOTO e4") == "error:11 servo arm up: M8 first\n", "no GOTO with the arm up: %s", r.out.text.c_str());
  CHECK(send("CARRY e2 e4") == "error:11 servo arm up: M8 first\n", "no CARRY with the arm up either: %s", r.out.text.c_str());
  CHECK(send("G1 X100 F2400") == "ok\n", "a carry-speed G1 (40 mm/s) is fine with the arm up");
  run_until_idle(hal, m, &p);
  CHECK(send("G1 X0 F3600") == "error:11 servo arm up: M8 first\n", "but not a faster feed: %s", r.out.text.c_str());
  CHECK(send("M3") == "ok\n" && per.on, "magnet on with the arm up");
  CHECK(send("M5") == "ok\n" && !per.on && !p.servo_up() && per.angle == SERVO_RETRACT_DEG, "M5 drops the magnet and the arm");
  CHECK(send("G0 X0") == "ok\n", "rapid again once the arm is down");
  run_until_idle(hal, m, &p);
  send("M7");
  send("!");
  CHECK(!p.servo_up(), "! lowers the arm");
  send("$X");
  send("M7");
  CHECK(p.servo_up(), "arm up again");
  send("$X");
  CHECK(!p.servo_up(), "$X lowers the arm");
  hal.t += MAGNET_MIN_OFF_MS * 1000UL + 1000;

  // The full carry: travel, arm up, magnet on, grab dwell, carry, magnet off, release dwell, arm down.
  per.events.clear();
  CHECK(send("CARRY e2 e4") == "ok\n", "carry: %s", r.out.text.c_str());
  CHECK(p.program_length() == 10, "a straight carry with the arm is 10 steps, got %d", p.program_length());
  CHECK(p.program_step(1).type == STEP_SERVO && p.program_step(1).on && p.program_step(2).type == STEP_DWELL && p.program_step(2).ms == SERVO_SETTLE_MS, "arm up, then settle");
  CHECK(p.program_step(3).type == STEP_MAG && p.program_step(3).on, "then magnet on");
  CHECK(p.program_step(5).type == STEP_MOVE && p.program_step(5).speed == CARRY_SPEED_MM_S, "the carry move");
  CHECK(p.program_step(6).type == STEP_MAG && !p.program_step(6).on && p.program_step(7).type == STEP_DWELL && p.program_step(7).ms == 200 &&
        p.program_step(8).type == STEP_SERVO && !p.program_step(8).on && p.program_step(9).type == STEP_DWELL && p.program_step(9).ms == SERVO_SETTLE_MS,
        "magnet off, release dwell, arm down, settle");
  double took = run_program(hal, m, p);
  const gantry::BoardGeometry g = board_geometry();   // the board this test set up, not config.h's
  double y_e2 = g.a1_y + g.square, y_e4 = g.a1_y + 3 * g.square;
  CHECK(per.events.size() == 4, "four events, got %zu", per.events.size());
  if (per.events.size() == 4) {
    CHECK(per.events[0].first == "arm up" && fabs(per.events[0].second - y_e2) < 1e-3, "1. arm up on e2: %s at Y=%.1f", per.events[0].first.c_str(), per.events[0].second);
    CHECK(per.events[1].first == "mag on" && fabs(per.events[1].second - y_e2) < 1e-3, "2. magnet on on e2: %s at Y=%.1f", per.events[1].first.c_str(), per.events[1].second);
    CHECK(per.events[2].first == "mag off" && fabs(per.events[2].second - y_e4) < 1e-3, "3. magnet off on e4 after the move: %s at Y=%.1f", per.events[2].first.c_str(), per.events[2].second);
    CHECK(per.events[3].first == "arm down" && fabs(per.events[3].second - y_e4) < 1e-3, "4. arm down on e4: %s at Y=%.1f", per.events[3].first.c_str(), per.events[3].second);
  }
  CHECK(took > 0.3 + 0.3 + 0.2 + 0.3, "includes both servo settles: %.2fs", took);
  CHECK(!p.servo_up() && !per.on && r.pos() == "260.000,185.000", "ends on e4, arm down, magnet off");

  // The self-test exercises the arm too.
  std::string selftest = send("$T");
  CHECK(selftest.find("arm up") != std::string::npos && selftest.find("\nok\n") != std::string::npos, "self-test with the arm: %s", selftest.c_str());
  int ups = 0;
  for (int i = 0; i < p.program_length(); i++) if (p.program_step(i).type == STEP_SERVO && p.program_step(i).on) ups++;
  CHECK(ups == 3, "the arm lifts three times in the self-test (e4, and each carry): %d", ups);
  run_program(hal, m, p);
  CHECK(!p.servo_up() && m.homed() && r.pos() == "60.000,35.000", "self-test ends on a1 with the arm down");

  // An alarm drops the arm.
  send("M7");
  CHECK(p.servo_up(), "arm up");
  m.alarm("test");
  p.tick(hal.t);
  CHECK(!p.servo_up() && per.angle == SERVO_RETRACT_DEG, "alarm lowers the arm");

  // A board without the arm: M7/M8 are accepted and do nothing, status has no Servo field.
  Rig plain;
  CHECK(plain.send("M7") == "ok\n" && !plain.p.servo_up() && plain.per.events.empty(), "M7 without a servo is a no-op");
  CHECK(plain.send("?").find("Servo") == std::string::npos, "no Servo field without a servo: %s", plain.out.text.c_str());
}

// ------------------------------------------------------------------ the guards in protocol.h

static void test_magnet_guards() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  Motion& m = r.m;
  Protocol& p = r.p;
  FakeHal& hal = r.hal;
  FakePeripherals& per = r.per;
  r.p.set_guards(1000, 500, 0);   // a 1 s magnet limit for the test, no host timeout
  send("$X");
  r.home();
  CHECK(send("M3") == "ok\n" && per.on, "magnet on");
  hal.t += 900000;
  p.tick(hal.t);
  CHECK(per.on && m.state() == IDLE, "still on at 0.9 s");
  hal.t += 200000;
  p.tick(hal.t);
  CHECK(!per.on, "off by itself after the limit");
  CHECK(send("?").rfind("<Alarm:magnet on too long|", 0) == 0, "alarm: %s", r.out.text.c_str());
  CHECK(!hal.enabled, "drivers off with the alarm");
  send("$X");
  r.home();
  CHECK(send("M3") == "ok\n", "magnet on again once homed");
  CHECK(send("M5") == "ok\n" && !per.on, "off");
  CHECK(send("M3") == "error:9 magnet cooling: wait\n", "no M3 within the cooling time: %s", r.out.text.c_str());
  hal.t += 600000;
  CHECK(send("M3") == "ok\n", "fine after 0.6 s");
  send("M5");
  hal.t += 600000;

  // A sequence waits for the coil instead of failing: magnet off at the end of one carry, the
  // next carry's M3 comes after the travel, which here is far shorter than the cooling time.
  r.p.set_guards(15000, 5000, 0);   // 5 s cooling, to make the wait visible
  send("CARRY e2 e4"); run_program(hal, m, p);
  uint32_t released = hal.t;
  send("CARRY e4 e2");
  run_program(hal, m, p);
  CHECK(per.on == false && r.pos() == "260.000,85.000", "the second carry still happened: %s", r.pos().c_str());
  CHECK(hal.t - released > 5000000, "and waited for the coil: %.2fs between the carries", (hal.t - released) / 1e6);

  // A carry that would hold the magnet longer than the limit is refused before it starts.
  r.p.set_guards(1000, 500, 0);
  CHECK(send("CARRY a1 h8") == "error:10 magnet would be on too long\n", "too long for a 1 s limit: %s", r.out.text.c_str());
  CHECK(p.program_length() == 0 && !p.busy() && !per.on, "nothing queued, nothing on");

  // A hold in the middle of the carry move drops the magnet at once.
  r.p.set_guards(15000, 500, 0);
  hal.t += 600000;
  send("CARRY e2 e4");
  for (int i = 0; i < 4000000 && !(per.on && m.moving()); i++) { m.run(); p.tick(hal.t); hal.t += 5; }
  CHECK(per.on && m.moving(), "magnet on and the piece under way");
  send("!");
  CHECK(!per.on && m.state() == HOLD && !p.busy(), "! stops, drops the sequence and switches the magnet off");
}

static void test_host_lost_finishes_the_carry_then_makes_the_board_safe() {
  Rig r(true);
  auto send = [&](const char* t) { return r.send(t); };
  Motion& m = r.m;
  Protocol& p = r.p;
  FakeHal& hal = r.hal;
  FakePeripherals& per = r.per;
  r.p.set_guards(MAGNET_MAX_ON_MS, MAGNET_MIN_OFF_MS, 1000);   // a 1 s host timeout for the test
  send("$X");
  r.home();
  CHECK(send("CARRY a1 h8") == "ok\n", "a long carry starts");
  // Nobody talks to the board while it runs (no ? polls): the carry still completes.
  double took = run_program(hal, m, p);
  CHECK(took > 1.0, "the carry outlasted the host timeout: %.2fs", took);
  CHECK(r.pos() == "410.000,385.000", "the piece arrived on h8: %s", r.pos().c_str());
  CHECK(!per.on && !p.servo_up(), "magnet off and arm down at the end");
  CHECK(!hal.enabled && !m.motors_enabled(), "drivers off because the host is gone");
  CHECK(p.host_lost(), "host lost is remembered");
  CHECK(send("?").find("|Host:lost>") != std::string::npos, "status says so: %s", r.out.text.c_str());
  CHECK(send("?").find("|Host:lost>") != std::string::npos, "and keeps saying so: asking is not taking the board back");
  CHECK(send("$X") == "ok\n", "a person unlocks");
  CHECK(send("?").find("Host:lost") == std::string::npos, "cleared by $X: %s", r.out.text.c_str());
  CHECK(m.homed(), "the position is still good (the carry finished cleanly)");
  // A sequence that is polled stays alive.
  send("CARRY h8 a1");
  while (p.busy()) {
    for (int i = 0; i < 20000 && p.busy(); i++) { m.run(); p.tick(hal.t); hal.t += 5; }   // 0.1 s between polls
    send("?");
  }
  CHECK(!p.host_lost() && hal.enabled, "polled every 0.1 s: never lost, drivers still on");
}

// ------------------------------------------------------------------ $110, $111, $120, $100

static uint32_t shortest_interval(const std::vector<uint32_t>& times) {
  uint32_t shortest = 1000000;
  for (size_t i = 1; i < times.size(); i++) shortest = std::min(shortest, times[i] - times[i - 1]);
  return shortest;
}

// Steps from the start of a move until the step interval first comes within 2% of the cruise interval.
static long ramp_steps(const std::vector<uint32_t>& times, double cruise_us) {
  for (size_t i = 1; i < times.size(); i++)
    if (times[i] - times[i - 1] <= cruise_us * 1.02) return (long)i;
  return -1;
}

// The bench: 3 us pulses never reached the drivers; Zain's 1500 us ones move both motors. The step
// rate must leave the low time at least as long as the high time.
static void test_step_pulse_caps_the_rate() {
  CHECK(STEP_PULSE_US == 1500, "boot step pulse 1500 us: %d", (int)STEP_PULSE_US);
  FakeHal hal;
  Motion m(hal, settings(false, false, MAX_STEP_HZ, STEP_PULSE_US));
  CHECK(hal.pulse == 1500, "the Hal is told the pulse: %u", hal.pulse);
  CHECK(fabs(m.settings().max_step_hz - 1000000.0f / 3000.0f) < 0.01f, "333 steps/s: %.2f", m.settings().max_step_hz);
  CHECK(fabs(m.step_capped(120.0f) - 1000000.0f / 3000.0f / 40.0f) < 1e-3, "8.33 mm/s at 40 steps/mm: %.3f", m.step_capped(120.0f));
  m.unlock();
  m.move_to(20, 0, 0);
  run_until_idle(hal, m);
  uint32_t shortest = 1000000;
  for (size_t i = 1; i < hal.step_times.size(); i++) shortest = std::min(shortest, hal.step_times[i] - hal.step_times[i - 1]);
  CHECK(shortest >= 2995, "every step at least 3 ms after the last: %u us", shortest);
  CHECK(hal.steps[0] == 800, "and the move arrives: %ld", hal.steps[0]);
  CHECK(m.set_step_pulse(100) == OK && hal.pulse == 100 && fabs(m.settings().max_step_hz - 5000.0f) < 0.01f, "100 us: 5 kHz");
  CHECK(m.set_step_pulse(0) == ERR_RANGE, "0 refused");
  CHECK(m.set_step_pulse(2) == OK && fabs(m.settings().max_step_hz - MAX_STEP_HZ) < 0.01f, "2 us: MAX_STEP_HZ still caps");
}

static void test_step_pulse_setting() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  CHECK(send("$0=1500") == "ok\n", "$0: %s", r.out.text.c_str());
  CHECK(r.hal.pulse == 1500 && fabs(r.m.step_capped(120.0f) - 8.3333f) < 1e-3, "the Hal and the cap follow: %u", r.hal.pulse);
  std::string s = send("$$");
  CHECK(s.find("$0=1500.000 (step pulse us)\n") != std::string::npos, "$$ shows it: %s", s.c_str());
  CHECK(send("$0=1") == "error:13 $0 must be 2..5000\n", "too short: %s", r.out.text.c_str());
  CHECK(send("$0=5001") == "error:13 $0 must be 2..5000\n", "too long: %s", r.out.text.c_str());
  CHECK(send("$0=") == "error:2 bad number\n", "nothing: %s", r.out.text.c_str());
  CHECK(r.hal.pulse == 1500, "refusals changed nothing");
  send("$X");
  CHECK(send("G0 X20") == "ok\n" && r.m.moving(), "a move under way");
  CHECK(send("$0=200") == "error:3 busy\n", "no $0 while moving: %s", r.out.text.c_str());
  run_until_idle(r.hal, r.m, &r.p);
  CHECK(send("$0=200") == "ok\n" && r.hal.pulse == 200, "after it: %s", r.out.text.c_str());
}

static void test_settings_at_boot() {
  CHECK(DEFAULT_SPEED_MM_S == 60 && DEFAULT_ACCEL_MM_S2 == 400 && CARRY_SPEED_MM_S == 40,
        "boot defaults 60 mm/s, 400 mm/s^2, carry 40 mm/s: %.0f, %.0f, %.0f", DEFAULT_SPEED_MM_S, DEFAULT_ACCEL_MM_S2, CARRY_SPEED_MM_S);
  CHECK(MAX_SPEED_MM_S == 120 && MAX_ACCEL_MM_S2 == 800, "ceilings 120 mm/s and 800 mm/s^2");
  Rig r;   // built the way the sketch builds its Motion and Protocol
  std::string s = r.send("$$");
  CHECK(s.find("$100=40.000 (steps/mm)\n") != std::string::npos, "$100 at boot: %s", s.c_str());
  CHECK(s.find("$110=60.000 (travel mm/s)\n") != std::string::npos, "$110 at boot: %s", s.c_str());
  CHECK(s.find("$111=40.000 (carry mm/s)\n") != std::string::npos, "$111 at boot: %s", s.c_str());
  CHECK(s.find("$120=400.000 (mm/s^2)\n") != std::string::npos, "$120 at boot: %s", s.c_str());
  CHECK(r.m.settings().max_speed == 60 && r.m.settings().accel == 400 && r.p.carry_speed() == 40, "and the motion runs on them");
}

static void test_settings_accepted_refused_and_busy() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  // Accepted before $X and $H: the numbers can be set on a locked board.
  CHECK(send("$110=30") == "ok\n", "$110: %s", r.out.text.c_str());
  CHECK(send("$111=25.5") == "ok\n", "$111: %s", r.out.text.c_str());
  CHECK(send("$120=200") == "ok\n", "$120: %s", r.out.text.c_str());
  std::string s = send("$$");
  CHECK(s.find("$110=30.000 (travel mm/s)\n") != std::string::npos && s.find("$111=25.500 (carry mm/s)\n") != std::string::npos &&
        s.find("$120=200.000 (mm/s^2)\n") != std::string::npos, "$$ shows the new values: %s", s.c_str());
  CHECK(r.m.settings().max_speed == 30 && r.m.settings().accel == 200 && r.p.carry_speed() == 25.5f, "the motion's copy changed with the protocol's");
  // Out of range: the reply names the range, and nothing changes.
  CHECK(send("$110=0") == "error:13 $110 must be 1..120\n", "zero speed: %s", r.out.text.c_str());
  CHECK(send("$110=121") == "error:13 $110 must be 1..120\n", "past the ceiling: %s", r.out.text.c_str());
  CHECK(send("$111=500") == "error:13 $111 must be 1..120\n", "carry past the ceiling: %s", r.out.text.c_str());
  CHECK(send("$120=801") == "error:13 $120 must be 1..800\n", "acceleration past the ceiling: %s", r.out.text.c_str());
  CHECK(send("$120=-5") == "error:13 $120 must be 1..800\n", "negative acceleration: %s", r.out.text.c_str());
  CHECK(send("$100=0") == "error:13 $100 must be 1..400\n", "zero steps/mm: %s", r.out.text.c_str());
  CHECK(send("$100=401") == "error:13 $100 must be 1..400\n", "too many steps/mm: %s", r.out.text.c_str());
  CHECK(send("$110=nan") == "error:13 $110 must be 1..120\n", "NaN is out of range too: %s", r.out.text.c_str());
  CHECK(send("$110=fast") == "error:2 bad number\n", "a word: %s", r.out.text.c_str());
  CHECK(send("$110=") == "error:2 bad number\n", "nothing: %s", r.out.text.c_str());
  CHECK(send("$110=30mm") == "error:2 bad number\n", "trailing letters: %s", r.out.text.c_str());
  CHECK(send("$105=3") == "error:1 unknown command\n", "no such setting: %s", r.out.text.c_str());
  CHECK(send("$110") == "error:1 unknown command\n", "no value: %s", r.out.text.c_str());
  s = send("$$");
  CHECK(s.find("$110=30.000") != std::string::npos && s.find("$111=25.500") != std::string::npos && s.find("$120=200.000") != std::string::npos &&
        s.find("$100=40.000") != std::string::npos, "refusals changed nothing: %s", s.c_str());
  CHECK(send("$110=120") == "ok\n" && send("$111=120") == "ok\n" && send("$120=800") == "ok\n" && send("$100=40") == "ok\n", "the ceilings themselves are allowed");
  send("$110=30");
  send("$111=25.5");
  send("$120=200");

  // Busy while a move runs.
  send("$X");
  CHECK(send("G0 X100") == "ok\n" && r.m.moving(), "a move under way");
  CHECK(send("$110=50") == "error:3 busy\n", "no $110 while moving: %s", r.out.text.c_str());
  CHECK(send("$120=400") == "error:3 busy\n", "no $120 while moving: %s", r.out.text.c_str());
  CHECK(send("$111=50") == "error:3 busy\n", "no $111 while moving: %s", r.out.text.c_str());
  CHECK(send("$100=80") == "error:3 busy\n", "no $100 while moving: %s", r.out.text.c_str());
  run_until_idle(r.hal, r.m, &r.p);
  CHECK(r.m.settings().max_speed == 30 && r.m.settings().accel == 200 && r.m.settings().steps_per_mm == 40, "still the old numbers");

  // Busy while a CARRY runs, also between its moves (here: holding the piece in the grab dwell).
  r.home();
  CHECK(send("CARRY e2 e4") == "ok\n", "carry: %s", r.out.text.c_str());
  for (int i = 0; i < 40000000 && !(r.per.on && !r.m.moving()); i++) { r.m.run(); r.p.tick(r.hal.t); r.hal.t += 5; }
  CHECK(r.per.on && !r.m.moving() && r.p.busy(), "the magnet holds the piece, nothing moves, the sequence runs");
  CHECK(send("$111=10") == "error:3 busy\n", "no $111 during a carry: %s", r.out.text.c_str());
  CHECK(send("$110=10") == "error:3 busy\n", "no $110 during a carry: %s", r.out.text.c_str());
  CHECK(send("$100=80") == "error:3 busy\n", "no $100 during a carry: %s", r.out.text.c_str());
  run_program(r.hal, r.m, r.p);
  CHECK(r.pos() == "260.000,185.000" && r.p.carry_speed() == 25.5f && r.m.homed(), "the carry finished as planned: %s", r.pos().c_str());
  CHECK(send("G4 P0.5") == "ok\n" && send("$110=40") == "error:3 busy\n", "a G4 dwell is a sequence too: %s", r.out.text.c_str());
}

static void test_settings_change_the_motion() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  FakeHal& hal = r.hal;
  send("$X");
  // $110=30: a G0 takes the time a 30 mm/s trapezoid predicts, and never steps faster.
  CHECK(send("$110=30") == "ok\n", "$110=30");
  hal.step_times.clear();
  CHECK(send("G0 X100") == "ok\n", "G0");
  double took = run_until_idle(hal, r.m, &r.p);
  double ideal = ideal_time(100, 30, DEFAULT_ACCEL_MM_S2);
  CHECK(fabs(took - ideal) < ideal * 0.03 + 0.01, "100 mm at 30 mm/s took %.3fs, the trapezoid says %.3fs", took, ideal);
  CHECK(shortest_interval(hal.step_times) >= (uint32_t)(1e6 / (30 * TEST_STEPS_PER_MM)) - 5, "no step faster than 30 mm/s: %u us", shortest_interval(hal.step_times));
  // ...and it is the top of G1 too: a faster feed runs at 30.
  hal.step_times.clear();
  CHECK(send("G1 X150 F6000") == "ok\n", "G1 at 100 mm/s asked");
  took = run_until_idle(hal, r.m, &r.p);
  CHECK(fabs(took - ideal_time(50, 30, DEFAULT_ACCEL_MM_S2)) < 0.05, "G1 capped at $110: %.3fs", took);

  // $120=200 halves the acceleration: the ramp to the same speed is twice as long.
  send("$110=60");
  hal.step_times.clear();
  send("G0 X350");
  took = run_until_idle(hal, r.m, &r.p);
  long ramp_400 = ramp_steps(hal.step_times, 1e6 / (60 * TEST_STEPS_PER_MM));
  CHECK(fabs(took - ideal_time(200, 60, 400)) < 0.05, "200 mm at 400 mm/s^2: %.3fs vs %.3fs", took, ideal_time(200, 60, 400));
  CHECK(send("$120=200") == "ok\n", "$120=200");
  hal.step_times.clear();
  send("G0 X150");
  took = run_until_idle(hal, r.m, &r.p);
  long ramp_200 = ramp_steps(hal.step_times, 1e6 / (60 * TEST_STEPS_PER_MM));
  CHECK(fabs(took - ideal_time(200, 60, 200)) < 0.05, "200 mm at 200 mm/s^2: %.3fs vs %.3fs", took, ideal_time(200, 60, 200));
  CHECK(ramp_400 > 150 && ramp_200 > 1.8 * ramp_400 && ramp_200 < 2.2 * ramp_400, "the ramp doubles: %ld steps at 400, %ld at 200", ramp_400, ramp_200);
  CHECK(fabs(r.m.move_seconds(100, 60) - ideal_time(100, 60, 200)) < 0.01, "the planner uses the new acceleration too");

  // The step-rate cap still applies on top: 400 steps/mm at 20 kHz is 50 mm/s, whatever $110 says.
  send("$110=120");
  send("$100=400");
  CHECK(fabs(r.m.step_capped(120) - 50) < 1e-3 && fabs(r.m.move_seconds(100, 120) - ideal_time(100, 50, 200)) < 0.01,
        "MAX_STEP_HZ caps the feed at %.1f mm/s", r.m.step_capped(120));
  send("$100=40");

  // Homing keeps its slow speed, and goes slower still when the travel speed is set below it.
  send("$120=400");
  send("$110=60");
  hal.step_times.clear();
  r.home();
  uint32_t at_60 = shortest_interval(hal.step_times);
  CHECK(at_60 >= (uint32_t)(1e6 / (HOMING_SPEED_MM_S * TEST_STEPS_PER_MM)) - 5 && at_60 < (uint32_t)(1e6 / (HOMING_SPEED_MM_S * TEST_STEPS_PER_MM)) + 50,
        "homing at %.0f mm/s with $110=60: %u us per step", HOMING_SPEED_MM_S, at_60);
  send("$110=20");
  hal.step_times.clear();
  r.home();
  CHECK(r.m.homed() && shortest_interval(hal.step_times) >= (uint32_t)(1e6 / (20 * TEST_STEPS_PER_MM)) - 5,
        "homing at 20 mm/s with $110=20: %u us per step", shortest_interval(hal.step_times));
}

static void test_carry_uses_the_carry_speed() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  FakePeripherals& per = r.per;
  send("$X");
  r.home();
  CHECK(send("$111=20") == "ok\n", "$111=20");
  per.events.clear();
  per.event_us.clear();
  uint32_t start = r.hal.t;
  CHECK(send("CARRY e2 e4") == "ok\n", "carry: %s", r.out.text.c_str());
  CHECK(r.p.program_step(0).speed == DEFAULT_SPEED_MM_S && r.p.program_step(3).speed == 20, "travel at $110, the carry at $111: %.0f, %.0f",
        r.p.program_step(0).speed, r.p.program_step(3).speed);
  run_program(r.hal, r.m, r.p);
  CHECK(per.events.size() == 2 && per.events[0].first == "mag on" && per.events[1].first == "mag off", "magnet on, then off: %zu events", per.events.size());
  if (per.event_us.size() == 2) {
    // From the magnet switching on to it switching off: the grab dwell, then the 100 mm carry e2 -> e4.
    double carry_s = (per.event_us[1] - per.event_us[0]) / 1e6 - GRAB_MS / 1000.0;
    double ideal = ideal_time(board_geometry().square * 2, 20, DEFAULT_ACCEL_MM_S2);
    CHECK(fabs(carry_s - ideal) < ideal * 0.03 + 0.01, "the carry leg took %.3fs, a 20 mm/s trapezoid %.3fs", carry_s, ideal);
    // From the command to the magnet: the travel from 0,0 to e2 at the travel speed.
    double travel_s = (per.event_us[0] - start) / 1e6;
    const BoardGeometry g = board_geometry();
    double d = sqrt((g.a1_x + 4 * g.square) * (g.a1_x + 4 * g.square) + (g.a1_y + g.square) * (g.a1_y + g.square));
    double travel_ideal = ideal_time(d, DEFAULT_SPEED_MM_S, DEFAULT_ACCEL_MM_S2);
    CHECK(fabs(travel_s - travel_ideal) < travel_ideal * 0.03 + 0.01, "the travel leg took %.3fs, a %.0f mm/s trapezoid %.3fs", travel_s, DEFAULT_SPEED_MM_S, travel_ideal);
  }

  // The magnet precheck plans with the carry speed and the acceleration as they are now. It only
  // exists for an electromagnet's coil: these are that build's limits (15 s on, 0.5 s off).
  r.p.set_guards(15000, 500, HOST_TIMEOUT_MS);
  r.hal.t += 500 * 1000UL + 1000;
  send("GOTO a1");
  run_program(r.hal, r.m, r.p);
  CHECK(send("$111=20") == "ok\n" && send("CARRY a1 a8") == "error:10 magnet would be on too long\n",
        "350 mm at 20 mm/s is 17.5 s under the magnet: %s", r.out.text.c_str());
  CHECK(r.p.program_length() == 0 && !r.p.busy(), "refused before anything moves");
  CHECK(send("$111=25") == "ok\n" && send("CARRY a1 a8") == "ok\n", "at 25 mm/s it is 14.4 s: %s", r.out.text.c_str());
  run_program(r.hal, r.m, r.p);
  CHECK(r.pos() == "60.000,385.000", "and it runs: %s", r.pos().c_str());
  r.hal.t += 500 * 1000UL + 1000;
  CHECK(send("$120=10") == "ok\n" && send("CARRY a8 a1") == "error:10 magnet would be on too long\n",
        "the same carry with a slow ramp is too long: %s", r.out.text.c_str());
  // With the boot carry speed, the longest straight carry fits.
  send("$120=400");
  send("$111=40");
  CHECK(send("CARRY a8 h1") == "ok\n", "the a8-h1 diagonal at 40 mm/s: %s", r.out.text.c_str());
  run_program(r.hal, r.m, r.p);
  CHECK(r.pos() == "410.000,35.000" && r.m.state() == IDLE, "and it arrives without the magnet guard tripping: %s", r.pos().c_str());

  // This build's permanent magnet (config.h: PIN_MAGNET -1, both limits 0): no carry is refused or cut
  // short for its length, and a grab right after a release does not wait for a coil to cool.
  Rig perm;
  perm.send("$X");
  perm.home();
  perm.send("GOTO a1");
  run_program(perm.hal, perm.m, perm.p);
  CHECK(perm.send("$111=20") == "ok\n" && perm.send("CARRY a1 a8") == "ok\n",
        "permanent magnet: 17.5 s under the magnet is fine: %s", perm.out.text.c_str());
  run_program(perm.hal, perm.m, perm.p);
  CHECK(perm.pos() == "60.000,385.000" && perm.m.state() == IDLE, "and it arrives, no alarm: %s", perm.pos().c_str());
  CHECK(perm.send("CARRY a8 a1") == "ok\n", "straight back, no cooling wait: %s", perm.out.text.c_str());
  run_program(perm.hal, perm.m, perm.p);
  CHECK(perm.pos() == "60.000,35.000", "back on a1: %s", perm.pos().c_str());
  perm.send("$$");
  CHECK(perm.out.text.find("$G=0,0,") != std::string::npos, "$$ says there are no coil limits: %s", perm.out.text.c_str());
}

static void test_carry_speed_caps_g1_with_the_arm_up() {
  Rig r(true);
  auto send = [&](const char* t) { return r.send(t); };
  send("$X");
  r.home();
  CHECK(send("M7") == "ok\n" && r.p.servo_up(), "arm up");
  CHECK(send("$111=20") == "ok\n", "$111 with the arm up and nothing moving");
  CHECK(send("G1 X50 F1200") == "ok\n", "G1 at 20 mm/s with the arm up: %s", r.out.text.c_str());
  run_until_idle(r.hal, r.m, &r.p);
  CHECK(send("G1 X0 F1500") == "error:11 servo arm up: M8 first\n", "25 mm/s is past $111=20: %s", r.out.text.c_str());
  CHECK(send("$111=30") == "ok\n" && send("G1 X0 F1500") == "ok\n", "and fine at $111=30: %s", r.out.text.c_str());
  run_until_idle(r.hal, r.m, &r.p);
}

static void test_steps_per_mm_clears_homed() {
  Rig r;
  auto send = [&](const char* t) { return r.send(t); };
  send("$X");
  r.home();
  CHECK(send("$100=40") == "ok\n" && r.m.homed(), "the same steps/mm changes nothing");
  CHECK(send("M3") == "ok\n" && r.per.on, "magnet on");
  CHECK(send("$100=5") == "ok\n", "$100=5 for a driver on full steps: %s", r.out.text.c_str());
  CHECK(!r.m.homed() && r.m.state() == ALARM, "the position no longer means anything");
  CHECK(!r.per.on, "and the magnet let go with it");
  CHECK(send("?").rfind("<Alarm:not homed|", 0) == 0, "status: %s", r.out.text.c_str());
  CHECK(send("$$").find("$100=5.000 (steps/mm)\n") != std::string::npos, "$$: %s", r.out.text.c_str());
  CHECK(send("GOTO e4") == "error:5 locked: $X to unlock, $H to home\n", "board commands wait for a home: %s", r.out.text.c_str());
  CHECK(send("$HM") == "ok\n" && r.m.homed(), "manual home");
  long before = r.hal.steps[0];
  CHECK(send("G0 X10") == "ok\n", "10 mm");
  run_until_idle(r.hal, r.m, &r.p);
  CHECK(r.hal.steps[0] - before == 50, "10 mm at 5 steps/mm is 50 steps: %ld", r.hal.steps[0] - before);
  CHECK(r.pos() == "10.000,0.000", "and reads back as 10 mm: %s", r.pos().c_str());
  // A very slow feed at few steps/mm (1 mm/s at 5 steps/mm is 5 steps/s, under the profile's
  // 20 steps/s starting rate) runs evenly and inside its time budget.
  CHECK(send("$110=1") == "ok\n" && send("G0 X14") == "ok\n", "4 mm at 1 mm/s");
  double took = run_until_idle(r.hal, r.m, &r.p);
  CHECK(r.m.state() == IDLE && r.pos() == "14.000,0.000" && took > 3.9 && took < 4.3, "4 mm at 1 mm/s in %.2fs, %s: %s", took, r.m.alarm_reason(), r.pos().c_str());
}

// Firmware 0.7 on the bench: full steps, 41 mm per motor turn (measured 2026-11-06), the travel and
// the board as $130 / $131 / $B set them, square size along Y apart from X.
static void test_bench_steps_and_travel() {
  CHECK(fabsf(STEPS_PER_MM - 200.0f / 41.0f) < 1e-4f, "4.878 steps/mm: %.4f", STEPS_PER_MM);
  CHECK(fabsf(200.0f / STEPS_PER_MM - 41.0f) < 1e-3f, "one turn (200 full steps) is 41 mm: %.3f", 200.0f / STEPS_PER_MM);
  CHECK(MAX_X_MM == 470.0f && MAX_Y_MM == 420.0f, "Sketch 5: the magnet's 470 x 420 travel");
  CHECK(A1_X_MM == 51.25f && A1_Y_MM == 26.25f && SQUARE_MM == 52.5f, "a1 at 51.25, 26.25; 52.5 mm squares");
  CHECK(A1_Y_MM + 7.5f * SQUARE_MM == MAX_Y_MM && 2 * A1_X_MM + 7 * SQUARE_MM == MAX_X_MM, "the board fills the height, centred across");
  CHECK(PARK_W_X_MM == 0.0f && PARK_B_X_MM == MAX_X_MM, "the docking columns on the travel's side edges: %.1f, %.1f", PARK_W_X_MM, PARK_B_X_MM);
  CHECK(fabsf(PARK_ROW0_MM + (PARK_ROWS - 1) * PARK_PITCH_MM - 407.65f) < 0.01f, "9 slots up to y 407.65");

  FakeHal hal;
  FakePeripherals per;
  CaptureWriter out;
  Settings s = settings(false, false, MAX_STEP_HZ, STEP_PULSE_US, STEPS_PER_MM);
  Motion m(hal, s);
  BoardGeometry board = {SQUARE_MM, A1_X_MM, A1_Y_MM, PARK_W_X_MM, PARK_B_X_MM, PARK_ROW0_MM, PARK_PITCH_MM, PARK_ROWS, 0.0f};
  Protocol p(m, per, out, s, board, "test");
  per.motion = &m;
  per.hal = &hal;
  p.set_carry(CARRY_SPEED_MM_S, GRAB_MS, RELEASE_MS);
  p.set_servo(false, SERVO_EXTEND_DEG, SERVO_RETRACT_DEG, SERVO_SETTLE_MS);
  p.set_guards(MAGNET_MAX_ON_MS, MAGNET_MIN_OFF_MS, HOST_TIMEOUT_MS);
  auto send = [&](const char* text) {
    char buf[200];
    strncpy(buf, text, sizeof buf - 1);
    buf[sizeof buf - 1] = 0;
    out.text.clear();
    p.handle_line(buf, hal.t);
    return out.text;
  };
  auto at = [&]() {
    char buf[40];
    snprintf(buf, sizeof buf, "%.1f,%.1f", m.position_mm(X), m.position_mm(Y));
    return std::string(buf);
  };
  CHECK(send("$$").find("$100=4.878 (steps/mm)\n") != std::string::npos, "$100 at boot: %s", out.text.c_str());
  CHECK(send("$100=4.87805") == "ok\n" && !m.homed() && m.state() == ALARM, "the Pi's value is the same: nothing changes");
  CHECK(send("$HM") == "ok\n" && m.homed(), "home by hand");
  CHECK(send("$100=4.87805") == "ok\n" && m.homed(), "sent again after the home: still homed");
  long before = hal.steps[0];
  CHECK(send("G0 X41") == "ok\n", "one turn's worth");
  run_until_idle(hal, m, &p);
  CHECK(hal.steps[0] - before == 200, "41 mm is 200 full steps, one motor turn: %ld", hal.steps[0] - before);

  // $130 / $131: the measured travel, kept through a move; the position stays valid.
  CHECK(send("$130=400") == "ok\n" && send("$131=402.5") == "ok\n", "travel set: %s", out.text.c_str());
  CHECK(m.homed() && m.settings().max_mm[X] == 400.0f && m.settings().max_mm[Y] == 402.5f, "kept, still homed");
  CHECK(send("$$").find("$130=400.000 (max X mm)\n") != std::string::npos, "$$ shows it: %s", out.text.c_str());
  CHECK(send("G0 X401") == "error:4 outside the travel limits\n", "past it refused: %s", out.text.c_str());
  CHECK(send("G0 X400 Y402.5") == "ok\n", "up to it allowed");
  run_until_idle(hal, m, &p);
  CHECK(send("$130=5") == "error:13 $130 must be 10..1000\n", "too short: %s", out.text.c_str());
  CHECK(send("$131=1001") == "error:13 $131 must be 10..1000\n", "too long: %s", out.text.c_str());
  send("G0 X0 Y0");
  CHECK(send("$130=470") == "error:3 busy\n", "not while moving: %s", out.text.c_str());
  run_until_idle(hal, m, &p);
  CHECK(send("$130=470") == "ok\n" && send("$131=420") == "ok\n", "back to the full travel");

  // The boot board, Sketch 5 (one full step is 0.205 mm, so places land within 0.11 mm).
  CHECK(send("GOTO a1") == "ok\n", "a1"); run_program(hal, m, p);
  CHECK(fabsf(m.position_mm(X) - 51.25f) < 0.11f && fabsf(m.position_mm(Y) - 26.25f) < 0.11f, "a1 at 51.25,26.25: %s", at().c_str());
  CHECK(send("GOTO w:0") == "ok\n", "left column"); run_program(hal, m, p);
  CHECK(fabsf(m.position_mm(X) - 0.0f) < 0.11f && fabsf(m.position_mm(Y) - 12.35f) < 0.11f, "left slot 0 at 0,12.35: %s", at().c_str());
  CHECK(send("GOTO b:8") == "ok\n", "right column"); run_program(hal, m, p);
  CHECK(fabsf(m.position_mm(X) - 470.0f) < 0.11f && fabsf(m.position_mm(Y) - 407.65f) < 0.11f, "right slot 8 at 470,407.65: %s", at().c_str());

  // $B's eighth number: the square size along Y measured apart from X.
  CHECK(send("$B=50.2,1,36,-41.5,394,17.5,35,49.6") == "ok\n", "per-axis squares: %s", out.text.c_str());
  CHECK(send("GOTO h8") == "ok\n", "h8"); run_program(hal, m, p);
  CHECK(fabsf(m.position_mm(X) - (1 + 7 * 50.2f)) < 0.11f && fabsf(m.position_mm(Y) - (36 + 7 * 49.6f)) < 0.11f, "h8 from both sizes: %s", at().c_str());
  CHECK(send("$$").find("$B=50.200,1.000,36.000,-41.500,394.000,17.500,35.000,49.600") != std::string::npos, "$$ shows both: %s", out.text.c_str());
  CHECK(send("$B=50,0,35,-42.5,392.5,17.5,35,0") == "error:2 bad number\n", "a zero Y size refused: %s", out.text.c_str());
  CHECK(send("$B=50,0,35,-42.5,392.5,17.5,35") == "ok\n", "seven numbers: Y follows X again");
  CHECK(send("$$").find("$B=50.000,0.000,35.000,-42.500,392.500,17.500,35.000,50.000") != std::string::npos, "$$: %s", out.text.c_str());
}

int main() {
  test_straight_move_counts_and_timing();
  test_long_move_cruises_at_max_speed();
  test_diagonal_bresenham();
  test_limits_busy_and_alarm();
  test_hold_then_resume();
  test_bump_homing();
  test_manual_home_declares_the_corner_without_moving();
  test_switch_homing_stops_at_the_switch();
  test_corexy();
  test_drivers_rest_when_idle_and_wake_with_a_settle();
  test_step_rate_cap();
  test_move_budget_catches_a_starved_loop();
  test_protocol();
  test_board_commands();
  test_servo_sequence();
  test_servo_angles();
  test_servo_arm_drive();
  test_magnet_guards();
  test_host_lost_finishes_the_carry_then_makes_the_board_safe();
  test_step_pulse_caps_the_rate();
  test_step_pulse_setting();
  test_settings_at_boot();
  test_settings_accepted_refused_and_busy();
  test_settings_change_the_motion();
  test_carry_uses_the_carry_speed();
  test_carry_speed_caps_g1_with_the_arm_up();
  test_steps_per_mm_clears_homed();
  test_bench_steps_and_travel();
  printf("%d checks, %d failures\n", checks, failures);
  return failures ? 1 : 0;
}
