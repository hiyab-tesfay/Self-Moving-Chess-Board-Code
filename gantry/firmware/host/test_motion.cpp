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

struct FakePeripherals : Peripherals {
  bool on = false;
  int toggles = 0;
  void magnet(bool want) override { if (want != on) toggles++; on = want; }
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

static void test_board_commands() {
  FakeHal hal;
  FakePeripherals per;
  CaptureWriter out;
  Settings s = settings();
  Motion m(hal, s);
  Protocol p(m, per, out, s, board_geometry(), "test");
  p.set_carry(60, 300, 200);
  auto send = [&](const char* text) { char buf[160]; strncpy(buf, text, sizeof buf - 1); buf[sizeof buf - 1] = 0; out.text.clear(); p.handle_line(buf, hal.t); return out.text; };
  auto pos = [&]() { char buf[40]; snprintf(buf, sizeof buf, "%.3f,%.3f", m.position_mm(X), m.position_mm(Y)); return std::string(buf); };

  CHECK(send("GOTO E4") == "error:5 locked: $X to unlock, $H to home\n", "locked: %s", out.text.c_str());
  send("$X");
  CHECK(send("GOTO e4") == "ok\n", "goto a square: %s", out.text.c_str());
  CHECK(send("?").rfind("<Run|", 0) == 0 && send("?").find("|Prog:1/1>") != std::string::npos, "status shows the sequence: %s", out.text.c_str());
  run_program(hal, m, p);
  CHECK(pos() == "260.000,185.000", "e4 is a1 (60,35) + (4,3) squares of 50: %s", pos().c_str());
  CHECK(send("GOTO w:2") == "ok\n", "goto a docking slot"); run_program(hal, m, p);
  CHECK(pos() == "17.500,87.500", "White's lane, third slot at 35 mm pitch: %s", pos().c_str());
  CHECK(send("GOTO 5.5,0.5") == "ok\n", "goto a lane crossing in squares"); run_program(hal, m, p);
  CHECK(pos() == "335.000,60.000", "lane crossing: %s", pos().c_str());
  CHECK(send("GOTO -0.5,4") == "ok\n", "goto the lane beside the a file"); run_program(hal, m, p);
  CHECK(pos() == "38.750,235.000", "halfway between the docking lane (17.5) and the a file (60): %s", pos().c_str());
  CHECK(send("GOTO i9") == "error:6 not a square, slot or x,y\n", "bad place: %s", out.text.c_str());
  CHECK(send("GOTO w:12") == "error:6 not a square, slot or x,y\n", "slot past the lane: %s", out.text.c_str());

  // A straight carry: travel to e2, magnet on, dwell, carry to e4, magnet off, dwell.
  int toggles_before = per.toggles;
  CHECK(send("CARRY e2 e4") == "ok\n", "carry: %s", out.text.c_str());
  CHECK(p.program_length() == 6, "straight carry is 6 steps, got %d", p.program_length());
  CHECK(p.program_step(0).type == STEP_MOVE && p.program_step(0).speed == s.max_speed, "first: travel to the piece");
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

  // $B replaces the geometry; $$ shows it.
  CHECK(send("$B=40,50,30") == "ok\n", "set square and a1");
  CHECK(send("GOTO h8") == "ok\n", "goto after $B"); run_program(hal, m, p);
  CHECK(pos() == "330.000,310.000", "h8 with 40 mm squares from (50,30): %s", pos().c_str());
  CHECK(send("$B=0,1,2") == "error:2 bad number\n", "a zero square is refused");
  CHECK(send("$$").find("$B=40.000,50.000,30.000") != std::string::npos, "settings show the board: %s", out.text.c_str());
  send("$B=50,60,35,17.5,452.5,17.5,35");

  // The self-test homes, visits the corners, works the magnet and does a knight's carry.
  std::string selftest = send("$T");
  CHECK(selftest.rfind("# selftest:", 0) == 0 && selftest.find("\nok\n") != std::string::npos, "self-test accepted with a trace: %s", selftest.c_str());
  CHECK(p.program_length() > 20, "self-test is a long sequence: %d", p.program_length());
  run_program(hal, m, p);
  CHECK(m.homed() && pos() == "60.000,35.000", "self-test ends homed, back on a1: %s", pos().c_str());

  // ! stops a sequence in mid-move and drops the rest of it.
  send("GOTO h1"); run_program(hal, m, p);
  CHECK(send("CARRY a1 h8") == "ok\n" && m.moving(), "the travel to a1 is under way");
  CHECK(send("!") == "ok\n" && !p.busy() && m.state() == HOLD, "stop drops the sequence");
  CHECK(send("?").rfind("<Hold|", 0) == 0, "hold: %s", out.text.c_str());

  // A sequence that would leave the travel is refused before anything moves.
  send("$X");
  send("$B=50,60,35,17.5,452.5,17.5,35");
  CHECK(send("GOTO 9.5,0") == "error:4 outside the travel limits\n", "beyond the far lane: %s", out.text.c_str());
}

static void test_protocol() {
  FakeHal hal;
  FakePeripherals per;
  CaptureWriter out;
  Settings s = settings();
  Motion m(hal, s);
  Protocol p(m, per, out, s, board_geometry(), "test");
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
  p.tick(hal.t);
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
  test_board_commands();
  printf("%d checks, %d failures\n", checks, failures);
  return failures ? 1 : 0;
}
