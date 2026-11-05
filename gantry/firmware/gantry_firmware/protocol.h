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
//                       the same carry through the via-points the Pi chose (when a piece is in the way)
//   CARRY d5 w:0        a taken piece goes to docking slot 0 of White's lane
//   $B=50,60,35,17.5,452.5,17.5,35
//                       square size, a1 centre, White's and Black's docking lane x, first slot y, slot pitch
//   $T                  the Arduino's own test: home, four corners, magnet on/off on e4, a knight's carry
//
// Millimetre level, the same as GRBL, so the Pi side also works unchanged against real GRBL:
//
//   ?                   status: <Idle|MPos:12.000,34.000|Mag:0|Homed:1>  (Run, Home, Hold, Alarm:reason;
//                       |Prog:3/12 while a sequence runs)
//   $H                  home both axes; the position is then 0,0
//   $X                  unlock after power-up, a hold or an alarm
//   G0 X.. Y..          rapid move in mm;  G1 X.. Y.. F..  at F mm/min
//   G4 P..              dwell P seconds;   G92 X.. Y..  call this spot these coordinates
//   M3 / M5             magnet on / off;   M17 / M18  motors on / off
//   !                   stop at once, drop the sequence (then $X)
//   $$                  settings;  $I  version
//
// Every command is answered with "ok" (accepted) or "error:<code> <text>". Poll ? until Idle before
// the next motion. Lines starting with # or ( are comments. Trace lines the board prints by itself
// start with # too.
#pragma once

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "motion.h"

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

struct Peripherals {
  virtual ~Peripherals() {}
  virtual void magnet(bool on) = 0;
  virtual bool magnet_on() = 0;
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
  ERR_PLACE = 6,       // not a square, slot or x,y
  ERR_PROGRAM = 7,     // sequence too long
};

enum StepType { STEP_MOVE, STEP_MAG, STEP_DWELL, STEP_HOME };

struct Step {
  uint8_t type;
  float x, y, speed;   // mm, mm/s (MOVE)
  bool on;             // MAG
  uint32_t ms;         // DWELL
};

class Protocol {
 public:
  Protocol(Motion& motion, Peripherals& p, Writer& out, const Settings& s, const BoardGeometry& board, const char* version)
      : m_(motion), p_(p), out_(out), s_(s), board_(board), version_(version), feed_mm_s_(s.max_speed),
        carry_speed_(s.max_speed / 2), grab_ms_(300), release_ms_(200),
        dwell_until_(0), dwelling_(false), prog_len_(0), prog_at_(0), prog_active_(false), disable_motors_(false) {}

  void set_carry(float speed_mm_s, uint32_t grab_ms, uint32_t release_ms) {
    carry_speed_ = speed_mm_s;
    grab_ms_ = grab_ms;
    release_ms_ = release_ms;
  }

  // Keep timers and the sequence going. Call next to Motion::run().
  void run(uint32_t now_us) {
    if (dwelling_ && (int32_t)(now_us - dwell_until_) >= 0) dwelling_ = false;
    if (!prog_active_ || dwelling_ || m_.moving()) return;
    if (m_.state() == ALARM || m_.state() == HOLD) { prog_active_ = false; return; }
    if (prog_at_ >= prog_len_) {
      prog_active_ = false;
      return;
    }
    const Step& step = program_[prog_at_++];
    switch (step.type) {
      case STEP_MOVE: {
        int code = m_.move_to(step.x, step.y, step.speed);
        if (code != OK) {
          m_.alarm(code == ERR_LIMIT ? "a step of the sequence is outside the travel" : "the sequence could not continue");
          prog_active_ = false;
        }
        break;
      }
      case STEP_MAG:
        p_.magnet(step.on);
        break;
      case STEP_DWELL:
        dwell_until_ = now_us + step.ms * 1000UL;
        dwelling_ = true;
        break;
      case STEP_HOME:
        m_.home();
        break;
    }
  }

  bool busy() const { return dwelling_ || m_.moving() || prog_active_; }
  bool take_disable_request() { bool d = disable_motors_; disable_motors_ = false; return d; }
  const BoardGeometry& board() const { return board_; }

  // The sequence queued by the last GOTO/CARRY/$T, for tests and traces.
  int program_length() const { return prog_len_; }
  const Step& program_step(int i) const { return program_[i]; }

  void handle_line(char* line, uint32_t now_us) {
    trim(line);
    if (!*line || *line == '#' || *line == '(') return;
    upper(line);
    if (!strcmp(line, "?")) { status(); return; }
    if (!strcmp(line, "!")) { m_.hold(); dwelling_ = false; prog_active_ = false; prog_len_ = 0; reply(OK); return; }
    if (!strcmp(line, "$H")) { reply(m_.home()); return; }
    if (!strcmp(line, "$X")) { m_.unlock(); reply(OK); return; }
    if (!strcmp(line, "$$")) { settings(); reply(OK); return; }
    if (!strcmp(line, "$I")) { out_.write("["); out_.write(version_); out_.write("]\n"); reply(OK); return; }
    if (!strcmp(line, "$T")) { reply(selftest()); return; }
    if (!strncmp(line, "$B=", 3)) { reply(set_board(line + 3)); return; }
    if (!strcmp(line, "M3")) { p_.magnet(true); reply(OK); return; }
    if (!strcmp(line, "M5")) { p_.magnet(false); reply(OK); return; }
    if (!strcmp(line, "M17")) { reply(OK); return; }   // motors enable themselves on the first move
    if (!strcmp(line, "M18")) { reply(m_.moving() ? ERR_BUSY : OK); if (!m_.moving()) disable_motors_ = true; return; }
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
        if (has_f) feed_mm_s_ = f / 60.0f;
        float tx = has_x ? x : m_.position_mm(X), ty = has_y ? y : m_.position_mm(Y);
        if (!within(tx, ty)) { reply(ERR_LIMIT); return; }
        begin_program();
        push_move(tx, ty, g == 0 ? s_.max_speed : feed_mm_s_);
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
  bool push_home() { Step s = {STEP_HOME, 0, 0, 0, false, 0}; return push(s); }
  void start_program(uint32_t now_us) { prog_active_ = prog_len_ > 0; run(now_us); }

  int check_program() const {
    for (int i = 0; i < prog_len_; i++)
      if (program_[i].type == STEP_MOVE && !within(program_[i].x, program_[i].y)) return ERR_LIMIT;
    return OK;
  }

  int go_to(const char* text) {
    if (busy()) return ERR_BUSY;
    if (m_.state() == ALARM || m_.state() == HOLD) return ERR_ALARM;
    Place place;
    if (!parse_place(text, place)) return ERR_PLACE;
    float x, y;
    place_mm(place, x, y);
    begin_program();
    push_move(x, y, s_.max_speed);
    int code = check_program();
    if (code != OK) return code;
    start_program(last_now_);
    return OK;
  }

  // CARRY from to [via ...]
  int carry(const char* text) {
    if (busy()) return ERR_BUSY;
    if (m_.state() == ALARM || m_.state() == HOLD) return ERR_ALARM;
    Place points[PROGRAM_CAPACITY];
    int count = 0;
    char buf[160];
    strncpy(buf, text, sizeof buf - 1);
    buf[sizeof buf - 1] = 0;
    for (char* token = strtok(buf, " "); token; token = strtok(nullptr, " ")) {
      if (count >= PROGRAM_CAPACITY) return ERR_PROGRAM;
      if (!parse_place(token, points[count])) return ERR_PLACE;
      count++;
    }
    if (count < 2) return ERR_PLACE;
    Place from = points[0], to = points[1];
    begin_program();
    float x, y;
    place_mm(from, x, y);
    push_move(x, y, s_.max_speed);
    push_mag(true);
    push_dwell(grab_ms_);
    if (count == 2) {
      if (!route(from, to)) return ERR_PROGRAM;
    } else {
      for (int i = 2; i < count; i++) {
        place_mm(points[i], x, y);
        if (!push_move(x, y, carry_speed_)) return ERR_PROGRAM;
      }
      place_mm(to, x, y);
      if (!push_move(x, y, carry_speed_)) return ERR_PROGRAM;
    }
    push_mag(false);
    if (!push_dwell(release_ms_)) return ERR_PROGRAM;
    int code = check_program();
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

  int selftest() {
    if (busy()) return ERR_BUSY;
    begin_program();
    push_home();
    const char* corners[] = {"A1", "H1", "H8", "A8", "E4"};
    for (int i = 0; i < 5; i++) {
      Place p;
      parse_place(corners[i], p);
      float x, y;
      place_mm(p, x, y);
      push_move(x, y, s_.max_speed);
    }
    push_mag(true);
    push_dwell(500);
    push_mag(false);
    push_dwell(release_ms_);
    Place e4, f6;
    parse_place("E4", e4);
    parse_place("F6", f6);
    push_mag(true);
    push_dwell(grab_ms_);
    route(e4, f6);
    push_mag(false);
    push_dwell(release_ms_);
    push_mag(true);
    push_dwell(grab_ms_);
    route(f6, e4);
    push_mag(false);
    push_dwell(release_ms_);
    Place a1;
    parse_place("A1", a1);
    float x, y;
    place_mm(a1, x, y);
    push_move(x, y, s_.max_speed);
    int code = check_program();
    if (code != OK) { begin_program(); return code; }
    out_.write("# selftest: home, a1, h1, h8, a8, e4, magnet on/off, carry e4->f6->e4 through the lanes, back to a1\n");
    m_.unlock();
    start_program(last_now_);
    return OK;
  }

  // ------------------------------------------------------------------ replies

  void reply(int code) {
    if (code == OK) { out_.write("ok\n"); return; }
    static const char* texts[] = {"", "unknown command", "bad number", "busy", "outside the travel limits",
                                  "locked: $X to unlock, $H to home", "not a square, slot or x,y", "sequence too long"};
    char buf[80];
    snprintf(buf, sizeof buf, "error:%d %s\n", code, code < 8 ? texts[code] : "");
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
    char x[16], y[16], buf[140];
    fixed3(m_.position_mm(X), x, sizeof x);
    fixed3(m_.position_mm(Y), y, sizeof y);
    if (m_.state() == ALARM)
      snprintf(buf, sizeof buf, "<Alarm:%s|MPos:%s,%s|Mag:%d|Homed:%d>\n", m_.alarm_reason(), x, y, p_.magnet_on() ? 1 : 0, m_.homed() ? 1 : 0);
    else if (prog_active_)
      snprintf(buf, sizeof buf, "<%s|MPos:%s,%s|Mag:%d|Homed:%d|Prog:%d/%d>\n", name, x, y, p_.magnet_on() ? 1 : 0, m_.homed() ? 1 : 0, prog_at_, prog_len_);
    else
      snprintf(buf, sizeof buf, "<%s|MPos:%s,%s|Mag:%d|Homed:%d>\n", name, x, y, p_.magnet_on() ? 1 : 0, m_.homed() ? 1 : 0);
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
  }

 public:
  static const int PROGRAM_CAPACITY = 32;
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
  uint32_t dwell_until_;
  bool dwelling_;
  Step program_[PROGRAM_CAPACITY];
  int prog_len_, prog_at_;
  bool prog_active_;
  bool disable_motors_;
  uint32_t last_now_ = 0;
};

}  // namespace gantry
