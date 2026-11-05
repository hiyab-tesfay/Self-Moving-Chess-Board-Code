// The serial language between the Pi and the board. Two levels, one line per command:
//
// Board level: the firmware knows the squares (config.h, or $B from the Pi) and works out the
// millimetres and the route itself. The Pi decides WHAT to move, from what the camera sees;
// the board decides HOW to get there.
//
//   GOTO e4             move the empty magnet to the centre of e4 (also w:3, b:0, or 5.5,0.5 in squares)
//   CARRY g1 f3         pick the piece up on g1 and carry it to f3: straight when the squares share a
//                       file, rank or diagonal, otherwise out to a corner, along the lanes, in from a corner
//   CARRY g1 f3 5.5,0.5 5.5,1.5
//                       the same carry through the via-points the Pi chose (when a piece is in the way);
//                       via-points are places too: squares, slots, or x,y in SQUARES (a1 = 0,0; x.5 = lanes)
//   CARRY d5 w:0        a taken piece goes to docking slot 0 of White's lane
//   $B=50,60,35,17.5,452.5,17.5,35
//                       square size, a1 centre, White's and Black's docking lane x, first slot y, slot pitch
//   $T                  the Arduino's own test (homed first): four corners, servo and magnet on e4, a
//                       knight's carry e4 -> f6 -> e4 through the lanes, back to a1
//
// A CARRY runs as one sequence: travel to `from` at full speed, servo arm up (M7), settle, magnet
// on (M3), grab dwell, the carry moves at carry speed, magnet off (M5), release dwell, arm down
// (M8), settle. Without a servo (PIN_SERVO -1) the arm steps are left out.
//
// Millimetre level, the same as GRBL, so the Pi side also works unchanged against real GRBL:
//
//   ?                   status: <Idle|MPos:12.000,34.000|Mag:0|Homed:1>  (Run, Home, Hold, Alarm:reason;
//                       |Servo:up/down with a servo; |Prog:3/12 while a sequence runs; |Host:lost after
//                       the host went quiet during a sequence, until $X or $H)
//   $H                  home both axes; the position is then 0,0 (magnet off and arm down first)
//   $X                  unlock after power-up, a hold or an alarm (magnet off, arm down)
//   G0 X.. Y..          rapid move in mm;  G1 X.. Y.. F..  at F mm/min
//   G4 P..              dwell P seconds;   G92 X.. Y..  call this spot these coordinates
//   M3 / M5             magnet on / off (M3 needs: homed, still, arm up, and MAGNET_MIN_OFF_MS since M5)
//   M7 / M8             servo arm up (magnet to the board) / down (clear of it); ok and nothing without a servo
//   M17 / M18           motors on (they enable themselves on the first move) / off now
//   !                   stop at once, drop the sequence, magnet off, arm down (then $X)
//   $$                  settings;  $I  version
//
// Every command is answered with "ok" (accepted) or "error:<code> <text>". Poll ? until Idle before
// the next motion. Lines starting with # or ( are comments. Trace lines the board prints by itself
// start with # too.
//
// Safety policies (the numbers and what each protects are in config.h): the magnet switches off by
// itself after MAGNET_MAX_ON_MS (alarm "magnet on too long"); a move past its time budget raises
// "move took too long"; a sequence whose host goes quiet for HOST_TIMEOUT_MS finishes, then releases
// the magnet, lowers the arm and switches the drivers off (|Host:lost); rapid motion with the arm up
// is refused; motion needs $H first (jogging with G0/G1 after $X is allowed, in +X/+Y only, so the
// first power-up checks can be done without a working home).
#pragma once

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "motion.h"

#ifndef PROGRAM_STEPS
#define PROGRAM_STEPS 48
#endif

namespace gantry {

// "12.345" with three decimals, without printf's %f (which AVR boards do not have).
inline void fixed3(float value, char* out, size_t size) {
  bool negative = value < 0;
  if (negative) value = -value;
  long whole = (long)value;
  long frac = (long)((value - whole) * 1000.0f + 0.5f);
  if (frac >= 1000) { whole++; frac -= 1000; }
  snprintf(out, size, "%s%ld.%03ld", negative ? "-" : "", whole, frac);
}

// The magnet and the servo arm: the two things on the carriage besides the motors. One
// implementation lives in the .ino (MOSFET gate, LEDC/Servo pulse), one in host/ (traces).
struct Peripherals {
  virtual ~Peripherals() {}
  virtual void magnet(bool on) = 0;
  virtual bool magnet_on() = 0;
  virtual void servo(int angle) { (void)angle; }   // boards without a servo leave this alone
};

struct Writer {
  virtual ~Writer() {}
  virtual void write(const char* text) = 0;
};

// Where the squares are. Set from config.h, replaced by $B from the Pi.
struct BoardGeometry {
  float square, a1_x, a1_y, park_w_x, park_b_x, park_row0, park_pitch;
  int park_rows;
};

// A place in board terms: coordinates in squares (a1 = 0,0; x.5 = lanes) or a docking slot.
struct Place {
  float x, y;      // in squares, for board points; for slots: x = -1 (White's lane) or 8 (Black's)
  bool slot;
  int row;         // slot index down the lane
};

enum {
  ERR_UNKNOWN = 1,
  ERR_NUMBER = 2,
  // 3 busy, 4 limit, 5 alarm: motion.h
  ERR_PLACE = 6,          // not a square, slot or x,y
  ERR_PROGRAM = 7,        // sequence too long
  ERR_NOT_HOMED = 8,      // GOTO, CARRY, $T and M3 need $H first
  ERR_MAGNET_COOL = 9,    // M3 within MAGNET_MIN_OFF_MS of the last M5
  ERR_MAGNET_LONG = 10,   // a CARRY that would keep the magnet on longer than MAGNET_MAX_ON_MS
  ERR_SERVO_UP = 11,      // rapid motion with the arm extended
  ERR_SERVO_DOWN = 12,    // M3 with the arm retracted
  ERR_COUNT = 13
};

enum StepType { STEP_MOVE, STEP_MAG, STEP_DWELL, STEP_SERVO };

struct Step {
  uint8_t type;
  float x, y, speed;   // mm, mm/s (MOVE)
  bool on;             // MAG: magnet on; SERVO: arm up
  uint32_t ms;         // DWELL
};

class Protocol {
 public:
  static const int PROGRAM_CAPACITY = PROGRAM_STEPS;

  Protocol(Motion& motion, Peripherals& p, Writer& out, const Settings& s, const BoardGeometry& board, const char* version)
      : m_(motion), p_(p), out_(out), s_(s), board_(board), version_(version), feed_mm_s_(s.max_speed),
        carry_speed_(s.max_speed / 2), grab_ms_(300), release_ms_(200),
        servo_present_(false), servo_extend_(90), servo_retract_(0), servo_settle_ms_(300), servo_up_(false),
        magnet_max_on_us_(15000UL * 1000UL), magnet_min_off_us_(500UL * 1000UL), host_timeout_us_(20000UL * 1000UL),
        magnet_on_at_(0), magnet_off_at_(0), magnet_was_on_(false), host_last_us_(0), host_lost_(false),
        dwell_until_(0), dwelling_(false), prog_len_(0), prog_at_(0), prog_active_(false) {}

  void set_carry(float speed_mm_s, uint32_t grab_ms, uint32_t release_ms) {
    carry_speed_ = speed_mm_s;
    grab_ms_ = grab_ms;
    release_ms_ = release_ms;
  }

  // The servo arm that lifts the magnet to the board. present = false means there is none.
  void set_servo(bool present, int extend_deg, int retract_deg, uint32_t settle_ms) {
    servo_present_ = present;
    servo_extend_ = extend_deg;
    servo_retract_ = retract_deg;
    servo_settle_ms_ = settle_ms;
  }

  // The safety numbers from config.h. 0 switches a guard off.
  void set_guards(uint32_t magnet_max_on_ms, uint32_t magnet_min_off_ms, uint32_t host_timeout_ms) {
    magnet_max_on_us_ = magnet_max_on_ms * 1000UL;
    magnet_min_off_us_ = magnet_min_off_ms * 1000UL;
    host_timeout_us_ = host_timeout_ms * 1000UL;
  }

  // Keep timers, guards and the sequence going. Call next to Motion::run().
  void run(uint32_t now_us) {
    if (dwelling_ && (int32_t)(now_us - dwell_until_) >= 0) dwelling_ = false;

    // Guards that hold whatever is going on.
    bool stopped = m_.state() == ALARM || m_.state() == HOLD;
    if (stopped) {
      set_magnet(false, now_us);
      set_servo_up(false);
    }
    if (p_.magnet_on() && magnet_max_on_us_ > 0 && (uint32_t)(now_us - magnet_on_at_) > magnet_max_on_us_) {
      // A carry is a few seconds; this long means the host is stuck or gone. Protect the coil.
      set_magnet(false, now_us);
      set_servo_up(false);
      m_.alarm("magnet on too long");
      prog_active_ = false;
      prog_len_ = 0;
      dwelling_ = false;
    }
    if (prog_active_ && host_timeout_us_ > 0 && (uint32_t)(now_us - host_last_us_) > host_timeout_us_) host_lost_ = true;

    if (!prog_active_ || dwelling_ || m_.moving()) return;
    if (stopped || prog_at_ >= prog_len_) {
      end_program(now_us);
      return;
    }
    const Step& step = program_[prog_at_];
    switch (step.type) {
      case STEP_MOVE: {
        prog_at_++;
        int code = m_.move_to(step.x, step.y, step.speed);
        if (code != OK) {
          m_.alarm(code == ERR_LIMIT ? "a step of the sequence is outside the travel" : "the sequence could not continue");
          end_program(now_us);
        }
        break;
      }
      case STEP_MAG:
        if (step.on && cooling(now_us)) return;   // wait for the coil; the sequence picks up here next call
        prog_at_++;
        set_magnet(step.on, now_us);
        break;
      case STEP_DWELL:
        prog_at_++;
        dwell_until_ = now_us + step.ms * 1000UL;
        dwelling_ = true;
        break;
      case STEP_SERVO:
        prog_at_++;
        set_servo_up(step.on);
        break;
    }
  }

  bool busy() const { return dwelling_ || m_.moving() || prog_active_; }
  bool servo_up() const { return servo_up_; }
  bool host_lost() const { return host_lost_; }
  const BoardGeometry& board() const { return board_; }

  // The sequence queued by the last GOTO/CARRY/$T, for tests and traces.
  int program_length() const { return prog_len_; }
  const Step& program_step(int i) const { return program_[i]; }

  void handle_line(char* line, uint32_t now_us) {
    trim(line);
    if (!*line || *line == '#' || *line == '(') return;
    upper(line);
    last_now_ = now_us;
    host_last_us_ = now_us;
    if (!strcmp(line, "?")) { status(); return; }   // asking does not count as taking the board back
    if (!strcmp(line, "!")) {
      m_.hold();
      dwelling_ = false;
      prog_active_ = false;
      prog_len_ = 0;
      set_magnet(false, now_us);
      set_servo_up(false);
      reply(OK);
      return;
    }
    if (!strcmp(line, "$H")) {
      // Homing with a piece held, or the arm up, would drag it the length of the board.
      set_magnet(false, now_us);
      set_servo_up(false);
      host_lost_ = false;
      reply(m_.home());
      return;
    }
    if (!strcmp(line, "$X")) {
      m_.unlock();
      set_magnet(false, now_us);
      set_servo_up(false);
      host_lost_ = false;
      reply(OK);
      return;
    }
    if (!strcmp(line, "$$")) { settings(); reply(OK); return; }
    if (!strcmp(line, "$I")) { out_.write("["); out_.write(version_); out_.write("]\n"); reply(OK); return; }
    if (!strcmp(line, "$T")) { reply(selftest()); return; }
    if (!strncmp(line, "$B=", 3)) { reply(set_board(line + 3)); return; }
    if (!strcmp(line, "M3")) { reply(magnet_command(true, now_us)); return; }
    if (!strcmp(line, "M5")) { reply(magnet_command(false, now_us)); return; }
    if (!strcmp(line, "M7")) { reply(servo_command(true)); return; }
    if (!strcmp(line, "M8")) { reply(servo_command(false)); return; }
    if (!strcmp(line, "M17")) { reply(OK); return; }   // motors enable themselves on the first move
    if (!strcmp(line, "M18")) {
      if (m_.moving()) { reply(ERR_BUSY); return; }
      m_.disable_motors();
      reply(OK);
      return;
    }
    if (!strncmp(line, "GOTO ", 5)) { reply(go_to(line + 5)); return; }
    if (!strncmp(line, "CARRY ", 6)) { reply(carry(line + 6)); return; }
    if (line[0] == 'G') {
      char* rest;
      long g = strtol(line + 1, &rest, 10);
      float x, y, f, p;
      bool has_x = word(rest, 'X', x), has_y = word(rest, 'Y', y), has_f = word(rest, 'F', f), has_p = word(rest, 'P', p);
      if (g == 0 || g == 1) {
        if (busy()) { reply(ERR_BUSY); return; }
        if (m_.state() == ALARM || m_.state() == HOLD) { reply(ERR_ALARM); return; }
        if (has_f) {
          feed_mm_s_ = f / 60.0f;
          if (feed_mm_s_ <= 0 || feed_mm_s_ > s_.max_speed) feed_mm_s_ = s_.max_speed;
        }
        float speed = g == 0 ? s_.max_speed : feed_mm_s_;
        // With the arm up the magnet is at the board: anything faster than a carry drags pieces.
        if (servo_up_ && speed > carry_speed_ + 0.001f) { reply(ERR_SERVO_UP); return; }
        float tx = has_x ? x : m_.position_mm(X), ty = has_y ? y : m_.position_mm(Y);
        if (!within(tx, ty)) { reply(ERR_LIMIT); return; }
        begin_program();
        push_move(tx, ty, speed);
        start_program(now_us);
        reply(OK);
        return;
      }
      if (g == 4) {
        if (!has_p || p < 0) { reply(ERR_NUMBER); return; }
        if (busy()) { reply(ERR_BUSY); return; }
        begin_program();
        push_dwell((uint32_t)(p * 1000.0f));
        start_program(now_us);
        reply(OK);
        return;
      }
      if (g == 92) {
        if (m_.moving()) { reply(ERR_BUSY); return; }
        m_.set_position(has_x ? x : m_.position_mm(X), has_y ? y : m_.position_mm(Y));
        reply(OK);
        return;
      }
    }
    reply(ERR_UNKNOWN);
  }

  // ------------------------------------------------------------------ places and routes

  // "e4", "w:3", "b:0" or "5.5,0.5". False when it is none of those.
  bool parse_place(const char* text, Place& place) const {
    place.slot = false;
    place.row = 0;
    if (text[0] >= 'A' && text[0] <= 'H' && text[1] >= '1' && text[1] <= '8' && (text[2] == 0 || text[2] == ' ')) {
      place.x = (float)(text[0] - 'A');
      place.y = (float)(text[1] - '1');
      return true;
    }
    if ((text[0] == 'W' || text[0] == 'B') && text[1] == ':') {
      char* end;
      long row = strtol(text + 2, &end, 10);
      if (end == text + 2 || row < 0 || row >= board_.park_rows) return false;
      place.slot = true;
      place.row = (int)row;
      place.x = text[0] == 'W' ? -1.0f : 8.0f;
      // Where the slot sits in square units, for routing: beside the board at the lane's height.
      place.y = (board_.park_row0 + row * board_.park_pitch - board_.a1_y) / board_.square;
      place.y = (float)((long)(place.y * 2 + (place.y >= 0 ? 0.5f : -0.5f))) / 2.0f;
      if (place.y < -0.5f) place.y = -0.5f;
      if (place.y > 7.5f) place.y = 7.5f;
      return true;
    }
    char* end;
    float x = (float)strtod(text, &end);
    if (end == text || *end != ',') return false;
    const char* second = end + 1;
    float y = (float)strtod(second, &end);
    if (end == second) return false;
    place.x = x;
    place.y = y;
    return true;
  }

  // Millimetres of a place. Lanes beside the board (x between -1 and 0, or 7 and 8) run between
  // the edge file and the docking lane, the same rule the Pi uses.
  void place_mm(const Place& place, float& x_mm, float& y_mm) const {
    if (place.slot) {
      x_mm = place.x < 0 ? board_.park_w_x : board_.park_b_x;
      y_mm = board_.park_row0 + place.row * board_.park_pitch;
      return;
    }
    float h_x = board_.a1_x + 7 * board_.square;
    if (place.x < 0) x_mm = board_.park_w_x + (place.x + 1.0f) * (board_.a1_x - board_.park_w_x);
    else if (place.x > 7) x_mm = h_x + (place.x - 7.0f) * (board_.park_b_x - h_x);
    else x_mm = board_.a1_x + place.x * board_.square;
    y_mm = board_.a1_y + place.y * board_.square;
  }

 private:
  static void trim(char* s) {
    char* start = s;
    while (*start == ' ' || *start == '\t' || *start == '\r' || *start == '\n') start++;
    size_t n = strlen(start);
    while (n && (start[n - 1] == ' ' || start[n - 1] == '\t' || start[n - 1] == '\r' || start[n - 1] == '\n')) n--;
    memmove(s, start, n);
    s[n] = 0;
  }

  static void upper(char* s) {
    for (; *s; s++) if (*s >= 'a' && *s <= 'z') *s -= 32;
  }

  // Finds "<letter><number>" anywhere in the rest of the line.
  static bool word(const char* s, char letter, float& value) {
    for (const char* c = s; *c; c++) {
      if (*c == letter) {
        char* end;
        value = (float)strtod(c + 1, &end);  // strtod, not strtof: AVR's C library has no strtof
        return end != c + 1;
      }
    }
    return false;
  }

  bool within(float x, float y) const {
    return x >= -0.001f && y >= -0.001f && x <= s_.max_mm[X] + 0.001f && y <= s_.max_mm[Y] + 0.001f;
  }

  // ------------------------------------------------------------------ the magnet and the arm

  bool cooling(uint32_t now_us) const {
    return magnet_was_on_ && !p_.magnet_on() && magnet_min_off_us_ > 0 && (uint32_t)(now_us - magnet_off_at_) < magnet_min_off_us_;
  }

  void set_magnet(bool on, uint32_t now_us) {
    if (on == p_.magnet_on()) return;
    p_.magnet(on);
    if (on) magnet_on_at_ = now_us;
    else {
      magnet_off_at_ = now_us;
      magnet_was_on_ = true;
    }
  }

  void set_servo_up(bool up) {
    if (!servo_present_ || up == servo_up_) return;
    servo_up_ = up;
    p_.servo(up ? servo_extend_ : servo_retract_);
  }

  // M3 / M5 from the host.
  int magnet_command(bool on, uint32_t now_us) {
    if (!on) {
      set_magnet(false, now_us);
      set_servo_up(false);   // after any M5 the arm comes down: a lowered magnet drags nothing
      return OK;
    }
    if (m_.state() == ALARM || m_.state() == HOLD) return ERR_ALARM;
    if (busy()) return ERR_BUSY;
    if (!m_.homed()) return ERR_NOT_HOMED;
    if (servo_present_ && !servo_up_) return ERR_SERVO_DOWN;
    if (cooling(now_us)) return ERR_MAGNET_COOL;
    set_magnet(true, now_us);
    return OK;
  }

  // M7 / M8 from the host. Without a servo both are accepted and do nothing, so a driver can
  // send M8 "just in case" to any board.
  int servo_command(bool up) {
    if (!servo_present_) return OK;
    if (busy()) return ERR_BUSY;
    if (up && (m_.state() == ALARM || m_.state() == HOLD)) return ERR_ALARM;
    set_servo_up(up);
    return OK;
  }

  // ------------------------------------------------------------------ the sequence

  void begin_program() { prog_len_ = 0; prog_at_ = 0; prog_active_ = false; }
  bool push(const Step& step) {
    if (prog_len_ >= PROGRAM_CAPACITY) return false;
    program_[prog_len_++] = step;
    return true;
  }
  bool push_move(float x, float y, float speed) { Step s = {STEP_MOVE, x, y, speed, false, 0}; return push(s); }
  bool push_mag(bool on) { Step s = {STEP_MAG, 0, 0, 0, on, 0}; return push(s); }
  bool push_dwell(uint32_t ms) { Step s = {STEP_DWELL, 0, 0, 0, false, ms}; return push(s); }
  bool push_servo(bool up) {
    if (!servo_present_) return true;
    Step s = {STEP_SERVO, 0, 0, 0, up, 0};
    return push(s) && push_dwell(servo_settle_ms_);
  }
  void start_program(uint32_t now_us) { prog_active_ = prog_len_ > 0; run(now_us); }

  void end_program(uint32_t now_us) {
    prog_active_ = false;
    if (host_lost_) {
      // The Pi went quiet while this ran. The piece is where the sequence put it; now make the
      // board safe and wait for a person ($X or $H clears Host:lost).
      set_magnet(false, now_us);
      set_servo_up(false);
      m_.disable_motors();
    }
  }

  // Every move inside the travel, and the magnet never planned to stay on past its limit.
  int check_program() const {
    float x = m_.position_mm(X), y = m_.position_mm(Y);
    float on_ms = 0;
    bool on = false;
    for (int i = 0; i < prog_len_; i++) {
      const Step& s = program_[i];
      switch (s.type) {
        case STEP_MOVE: {
          if (!within(s.x, s.y)) return ERR_LIMIT;
          float dx = s.x - x, dy = s.y - y;
          if (on) on_ms += m_.move_seconds(sqrtf(dx * dx + dy * dy), s.speed) * 1000.0f;
          x = s.x;
          y = s.y;
          break;
        }
        case STEP_MAG: on = s.on; break;
        case STEP_DWELL: if (on) on_ms += (float)s.ms; break;
        default: break;
      }
    }
    if (magnet_max_on_us_ > 0 && on_ms * 1000.0f > (float)magnet_max_on_us_) return ERR_MAGNET_LONG;
    return OK;
  }

  // What has to be true before a sequence that moves pieces may start.
  int ready_for_board_command() const {
    if (busy()) return ERR_BUSY;
    if (m_.state() == ALARM || m_.state() == HOLD) return ERR_ALARM;
    if (!m_.homed()) return ERR_NOT_HOMED;
    if (servo_up_) return ERR_SERVO_UP;
    return OK;
  }

  int go_to(const char* text) {
    int code = ready_for_board_command();
    if (code != OK) return code;
    Place place;
    if (!parse_place(text, place)) return ERR_PLACE;
    float x, y;
    place_mm(place, x, y);
    begin_program();
    push_move(x, y, s_.max_speed);
    code = check_program();
    if (code != OK) { begin_program(); return code; }
    start_program(last_now_);
    return OK;
  }

  // The first part of a carry: travel to the piece, arm up, magnet on, hold still.
  bool push_pickup(const Place& from) {
    float x, y;
    place_mm(from, x, y);
    return push_move(x, y, s_.max_speed) && push_servo(true) && push_mag(true) && push_dwell(grab_ms_);
  }

  // The last part: let go, hold still, arm down.
  bool push_release() { return push_mag(false) && push_dwell(release_ms_) && push_servo(false); }

  // CARRY from to [via ...]: the tokens are read one at a time, so no array of places is needed.
  int carry(const char* text) {
    int code = ready_for_board_command();
    if (code != OK) return code;
    char buf[160];
    strncpy(buf, text, sizeof buf - 1);
    buf[sizeof buf - 1] = 0;
    char* token = strtok(buf, " ");
    Place from, to;
    if (!token || !parse_place(token, from)) return ERR_PLACE;
    token = strtok(nullptr, " ");
    if (!token || !parse_place(token, to)) return ERR_PLACE;
    begin_program();
    if (!push_pickup(from)) return ERR_PROGRAM;
    int vias = 0;
    float x, y;
    for (token = strtok(nullptr, " "); token; token = strtok(nullptr, " ")) {
      Place via;
      if (!parse_place(token, via)) { begin_program(); return ERR_PLACE; }
      place_mm(via, x, y);
      if (!push_move(x, y, carry_speed_)) { begin_program(); return ERR_PROGRAM; }
      vias++;
    }
    if (vias == 0) {
      if (!route(from, to)) { begin_program(); return ERR_PROGRAM; }
    } else {
      place_mm(to, x, y);
      if (!push_move(x, y, carry_speed_)) { begin_program(); return ERR_PROGRAM; }
    }
    if (!push_release()) { begin_program(); return ERR_PROGRAM; }
    code = check_program();
    if (code != OK) { begin_program(); return code; }
    start_program(last_now_);
    return OK;
  }

  // The board's own route: straight when the two places share a file, rank or diagonal; otherwise
  // out of the square to the corner nearest the target, along the lanes, and in from the corner.
  // Only the Pi knows whether something is in the way: it sends via-points when it is.
  bool route(const Place& from, const Place& to) {
    float ax = from.x, ay = from.y, bx = to.x, by = to.y;
    float dx = bx - ax, dy = by - ay;
    bool straight = !from.slot && !to.slot && (dx == 0 || dy == 0 || (dx < 0 ? -dx : dx) == (dy < 0 ? -dy : dy));
    float x, y;
    if (straight) {
      place_mm(to, x, y);
      return push_move(x, y, carry_speed_);
    }
    Place near_a = corner(from, bx, by), near_b = corner(to, ax, ay);
    Place middle = {near_b.x, near_a.y, false, 0};
    const Place* points[4] = {&near_a, &middle, &near_b, &to};
    const Place* last = &from;
    for (int i = 0; i < 4; i++) {
      if (points[i]->slot == last->slot && points[i]->x == last->x && points[i]->y == last->y && (!points[i]->slot || points[i]->row == last->row)) continue;
      place_mm(*points[i], x, y);
      if (!push_move(x, y, carry_speed_)) return false;
      last = points[i];
    }
    return true;
  }

  // The lane crossing beside a place that is nearest to (toward_x, toward_y), staying on the board if it can.
  static Place corner(const Place& p, float toward_x, float toward_y) {
    float dx = (toward_x > p.x || (toward_x == p.x && p.x < 7)) ? 0.5f : -0.5f;
    float dy = (toward_y > p.y || (toward_y == p.y && p.y < 7)) ? 0.5f : -0.5f;
    Place c = {p.x + dx, p.y + dy, false, 0};
    return c;
  }

  // ------------------------------------------------------------------ $B and $T

  int set_board(const char* text) {
    float v[7];
    int n = 0;
    const char* c = text;
    while (n < 7) {
      char* end;
      v[n] = (float)strtod(c, &end);
      if (end == c) break;
      n++;
      if (*end != ',') break;
      c = end + 1;
    }
    if (n != 3 && n != 7) return ERR_NUMBER;
    if (v[0] <= 0) return ERR_NUMBER;
    board_.square = v[0];
    board_.a1_x = v[1];
    board_.a1_y = v[2];
    if (n == 7) {
      board_.park_w_x = v[3];
      board_.park_b_x = v[4];
      board_.park_row0 = v[5];
      board_.park_pitch = v[6];
    }
    return OK;
  }

  // Homed first: the test runs at full speed to the corner squares, so the position must be real.
  int selftest() {
    int code = ready_for_board_command();
    if (code != OK) return code;
    begin_program();
    const char* corners[] = {"A1", "H1", "H8", "A8", "E4"};
    for (int i = 0; i < 5; i++) {
      Place p;
      parse_place(corners[i], p);
      float x, y;
      place_mm(p, x, y);
      push_move(x, y, s_.max_speed);
    }
    // On e4: arm up, magnet on and off, arm down, with nothing to carry.
    push_servo(true);
    push_mag(true);
    push_dwell(500);
    push_mag(false);
    push_dwell(release_ms_);
    push_servo(false);
    // A knight's carry there and back, the full sequence each way.
    Place e4, f6;
    parse_place("E4", e4);
    parse_place("F6", f6);
    push_pickup(e4);
    route(e4, f6);
    push_release();
    push_pickup(f6);
    route(f6, e4);
    push_release();
    Place a1;
    parse_place("A1", a1);
    float x, y;
    place_mm(a1, x, y);
    if (!push_move(x, y, s_.max_speed)) { begin_program(); return ERR_PROGRAM; }
    code = check_program();
    if (code != OK) { begin_program(); return code; }
    out_.write(servo_present_
                   ? "# selftest: a1, h1, h8, a8, e4, arm up, magnet on/off, arm down, carry e4->f6->e4 through the lanes, back to a1\n"
                   : "# selftest: a1, h1, h8, a8, e4, magnet on/off, carry e4->f6->e4 through the lanes, back to a1\n");
    start_program(last_now_);
    return OK;
  }

  // ------------------------------------------------------------------ replies

  void reply(int code) {
    if (code == OK) { out_.write("ok\n"); return; }
    static const char* texts[ERR_COUNT] = {
        "", "unknown command", "bad number", "busy", "outside the travel limits",
        "locked: $X to unlock, $H to home", "not a square, slot or x,y", "sequence too long",
        "not homed: $H first", "magnet cooling: wait", "magnet would be on too long",
        "servo arm up: M8 first", "servo arm down: M7 first"};
    char buf[96];
    const char* note = (code == ERR_LIMIT && !m_.homed()) ? " (not homed: $H first)" : "";
    snprintf(buf, sizeof buf, "error:%d %s%s\n", code, code < ERR_COUNT ? texts[code] : "", note);
    out_.write(buf);
  }

  void status() {
    const char* name = "Idle";
    switch (m_.state()) {
      case RUN: name = "Run"; break;
      case HOME: name = "Home"; break;
      case HOLD: name = "Hold"; break;
      case ALARM: name = "Alarm"; break;
      default: name = (dwelling_ || prog_active_) ? "Run" : "Idle";
    }
    char x[16], y[16], buf[160];
    fixed3(m_.position_mm(X), x, sizeof x);
    fixed3(m_.position_mm(Y), y, sizeof y);
    int n;
    if (m_.state() == ALARM)
      n = snprintf(buf, sizeof buf, "<Alarm:%s|MPos:%s,%s|Mag:%d|Homed:%d", m_.alarm_reason(), x, y, p_.magnet_on() ? 1 : 0, m_.homed() ? 1 : 0);
    else
      n = snprintf(buf, sizeof buf, "<%s|MPos:%s,%s|Mag:%d|Homed:%d", name, x, y, p_.magnet_on() ? 1 : 0, m_.homed() ? 1 : 0);
    if (n < 0) n = 0;
    if (servo_present_) n += snprintf(buf + n, sizeof buf - n, "|Servo:%s", servo_up_ ? "up" : "down");
    if (prog_active_) n += snprintf(buf + n, sizeof buf - n, "|Prog:%d/%d", prog_at_, prog_len_);
    if (host_lost_) n += snprintf(buf + n, sizeof buf - n, "|Host:lost");
    snprintf(buf + n, sizeof buf - n, ">\n");
    out_.write(buf);
  }

  void settings() {
    struct { const char* key; float value; const char* note; } rows[] = {
        {"$100", s_.steps_per_mm, "steps/mm"}, {"$110", s_.max_speed, "max mm/s"}, {"$120", s_.accel, "mm/s^2"},
        {"$130", s_.max_mm[X], "max X mm"}, {"$131", s_.max_mm[Y], "max Y mm"}, {"$22", s_.use_endstops ? 1.0f : 0.0f, "homing switches"}};
    for (auto& row : rows) {
      char value[16], buf[64];
      fixed3(row.value, value, sizeof value);
      snprintf(buf, sizeof buf, "%s=%s (%s)\n", row.key, value, row.note);
      out_.write(buf);
    }
    char a[16], b[16], c[16], d[16], e[16], f[16], g[16], buf[160];
    fixed3(board_.square, a, sizeof a); fixed3(board_.a1_x, b, sizeof b); fixed3(board_.a1_y, c, sizeof c);
    fixed3(board_.park_w_x, d, sizeof d); fixed3(board_.park_b_x, e, sizeof e); fixed3(board_.park_row0, f, sizeof f); fixed3(board_.park_pitch, g, sizeof g);
    snprintf(buf, sizeof buf, "$B=%s,%s,%s,%s,%s,%s,%s (square, a1, docking lanes)\n", a, b, c, d, e, f, g);
    out_.write(buf);
    snprintf(buf, sizeof buf, "$G=%lu,%lu,%lu (magnet max on ms, min off ms, host timeout ms)\n",
             (unsigned long)(magnet_max_on_us_ / 1000UL), (unsigned long)(magnet_min_off_us_ / 1000UL), (unsigned long)(host_timeout_us_ / 1000UL));
    out_.write(buf);
    if (servo_present_) {
      snprintf(buf, sizeof buf, "$S=%d,%d,%lu (servo up deg, down deg, settle ms)\n", servo_extend_, servo_retract_, (unsigned long)servo_settle_ms_);
      out_.write(buf);
    }
  }

 public:
  void tick(uint32_t now_us) { last_now_ = now_us; run(now_us); }

 private:
  Motion& m_;
  Peripherals& p_;
  Writer& out_;
  Settings s_;
  BoardGeometry board_;
  const char* version_;
  float feed_mm_s_, carry_speed_;
  uint32_t grab_ms_, release_ms_;
  bool servo_present_;
  int servo_extend_, servo_retract_;
  uint32_t servo_settle_ms_;
  bool servo_up_;
  uint32_t magnet_max_on_us_, magnet_min_off_us_, host_timeout_us_;
  uint32_t magnet_on_at_, magnet_off_at_;
  bool magnet_was_on_;
  uint32_t host_last_us_;
  bool host_lost_;
  uint32_t dwell_until_;
  bool dwelling_;
  Step program_[PROGRAM_CAPACITY];
  int prog_len_, prog_at_;
  bool prog_active_;
  uint32_t last_now_ = 0;
};

}  // namespace gantry
