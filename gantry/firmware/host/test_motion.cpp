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
  void set_dir(int m, bool positive) override { dir[m] = positive ? 1 : -1; }
  void step(uint8_t mask) override {
    for (int m = 0; m < 2; m++) if (mask & (1 << m)) steps[m] += dir[m];
    step_times.push_back(t);
  }
  void enable_motors(bool on) override { if (on != enabled) enable_changes++; enabled = on; }
  bool endstop(int axis) override { return endstops && steps[axis] <= pressed_below[axis]; }
  uint32_t now_us() override { return t; }
};

static Settings settings(bool corexy = false, bool endstops = false, float max_step_hz = MAX_STEP_HZ) {
  return {STEPS_PER_MM, {MAX_X_MM, MAX_Y_MM}, MAX_SPEED_MM_S, MAX_ACCEL_MM_S2, HOMING_SPEED_MM_S,
          HOMING_BUMP_MM, HOMING_PULL_OFF_MM, {false, false}, corexy, endstops, true,
          max_step_hz, IDLE_DISABLE_MS, ENABLE_SETTLE_MS, MOVE_BUDGET_FACTOR, MOVE_BUDGET_MARGIN_MS};
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
// test can read the order of events against the moves.
struct FakePeripherals : Peripherals {
  bool on = false;
  int toggles = 0;
  int angle = SERVO_RETRACT_DEG;
  Motion* motion = nullptr;
  std::vector<std::pair<std::string, double>> events;
  void magnet(bool want) override {
    if (want != on) toggles++;
    on = want;
    events.push_back({want ? "mag on" : "mag off", motion ? motion->position_mm(Y) : 0.0});
  }
  bool magnet_on() override { return on; }
  void servo(int want) override {
    angle = want;
    events.push_back({want == SERVO_EXTEND_DEG ? "arm up" : "arm down", motion ? motion->position_mm(Y) : 0.0});
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
  double planned = ideal_time(100, MAX_SPEED_MM_S, MAX_ACCEL_MM_S2);
  uint32_t budget = m.segment_budget_us();
  CHECK(budget > planned * MOVE_BUDGET_FACTOR * 1e6 * 0.9 && budget < (planned * MOVE_BUDGET_FACTOR + MOVE_BUDGET_MARGIN_MS / 1000.0) * 1e6 * 1.1 + 100000,
        "budget %.2fs for a %.2fs move", budget / 1e6, planned);
  CHECK(fabs(m.move_seconds(100, MAX_SPEED_MM_S) - planned) < 0.01, "move_seconds matches the trapezoid: %.3f vs %.3f", m.move_seconds(100, MAX_SPEED_MM_S), planned);
  for (int i = 0; i < 2000; i++) { m.run(); hal.t += 5; }
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

static BoardGeometry board_geometry() {
  return {SQUARE_MM, A1_X_MM, A1_Y_MM, PARK_W_X_MM, PARK_B_X_MM, PARK_ROW0_MM, PARK_PITCH_MM, PARK_ROWS};
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
    p.set_carry(60, 300, 200);
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
  CHECK(p.program_step(3).type == STEP_MOVE && p.program_step(3).speed == 60, "the carry itself runs at carry speed");
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
  CHECK(send("$$").find("$G=15000,500,20000") != std::string::npos, "settings show the guards: %s", r.out.text.c_str());
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
  CHECK(send("G1 X100 F3600") == "ok\n", "a carry-speed G1 (60 mm/s) is fine with the arm up");
  run_until_idle(hal, m, &p);
  CHECK(send("G1 X0 F7200") == "error:11 servo arm up: M8 first\n", "but not a faster feed: %s", r.out.text.c_str());
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
  CHECK(p.program_step(5).type == STEP_MOVE && p.program_step(5).speed == 60, "the carry move");
  CHECK(p.program_step(6).type == STEP_MAG && !p.program_step(6).on && p.program_step(7).type == STEP_DWELL && p.program_step(7).ms == 200 &&
        p.program_step(8).type == STEP_SERVO && !p.program_step(8).on && p.program_step(9).type == STEP_DWELL && p.program_step(9).ms == SERVO_SETTLE_MS,
        "magnet off, release dwell, arm down, settle");
  double took = run_program(hal, m, p);
  double y_e2 = A1_Y_MM + SQUARE_MM, y_e4 = A1_Y_MM + 3 * SQUARE_MM;
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
  test_magnet_guards();
  test_host_lost_finishes_the_carry_then_makes_the_board_safe();
  printf("%d checks, %d failures\n", checks, failures);
  return failures ? 1 : 0;
}
