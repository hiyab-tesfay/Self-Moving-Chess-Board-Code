// The serial language between the Pi and the board: a small GRBL-style dialect, so the Pi
// side also works unchanged against real GRBL on a CNC shield. One command per line.
//
//   ?                 status:  <Idle|MPos:12.000,34.000|Mag:0|Homed:1>   (also Run, Home, Hold, Alarm:reason)
//   $H                home both axes, then the position is 0,0
//   $X                unlock after power-up, a hold or an alarm (position is whatever the count says)
//   G0 X.. Y..        rapid move, absolute mm
//   G1 X.. Y.. F..    move at F mm/min (the usual G-code unit), remembered for later G1s
//   G4 P..            dwell P seconds (lets the magnet grab before moving)
//   G92 X.. Y..       call the current spot these coordinates
//   M3 / M5           magnet on / off (electromagnet pin, and the lift servo if there is one)
//   M17 / M18         motors on / off
//   !                 stop immediately (then $X)
//   $$                print the settings, so the Pi can check steps per mm and the limits
//   $I                firmware version
//
// Every command is answered with "ok" or "error:<code> <text>". A move answers ok as soon as
// it is accepted; poll ? until Idle before sending the next one. Lines starting with # or (
// are comments and are ignored.
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

enum {
  ERR_UNKNOWN = 1,
  ERR_NUMBER = 2,
};

class Protocol {
 public:
  Protocol(Motion& motion, Peripherals& p, Writer& out, const Settings& s, const char* version)
      : m_(motion), p_(p), out_(out), s_(s), version_(version), feed_mm_s_(s.max_speed), dwell_until_(0), dwelling_(false) {}

  // Keep timers going. Call next to Motion::run().
  void run(uint32_t now_us) {
    if (dwelling_ && (int32_t)(now_us - dwell_until_) >= 0) dwelling_ = false;
  }

  bool busy() const { return dwelling_ || m_.moving(); }

  void handle_line(char* line, uint32_t now_us) {
    trim(line);
    if (!*line || *line == '#' || *line == '(') return;
    upper(line);
    if (!strcmp(line, "?")) { status(); return; }
    if (!strcmp(line, "!")) { m_.hold(); dwelling_ = false; reply(OK); return; }
    if (!strcmp(line, "$H")) { reply(m_.home()); return; }
    if (!strcmp(line, "$X")) { m_.unlock(); reply(OK); return; }
    if (!strcmp(line, "$$")) { settings(); reply(OK); return; }
    if (!strcmp(line, "$I")) { out_.write("["); out_.write(version_); out_.write("]\n"); reply(OK); return; }
    if (!strcmp(line, "M3")) { p_.magnet(true); reply(OK); return; }
    if (!strcmp(line, "M5")) { p_.magnet(false); reply(OK); return; }
    if (!strcmp(line, "M17")) { reply(OK); return; }   // motors enable themselves on the first move
    if (!strcmp(line, "M18")) { reply(m_.moving() ? ERR_BUSY : OK); if (!m_.moving()) disable_motors_ = true; return; }
    if (line[0] == 'G') {
      char* rest;
      long g = strtol(line + 1, &rest, 10);
      float x, y, f, p;
      bool has_x = word(rest, 'X', x), has_y = word(rest, 'Y', y), has_f = word(rest, 'F', f), has_p = word(rest, 'P', p);
      if (g == 0 || g == 1) {
        if (dwelling_) { reply(ERR_BUSY); return; }
        if (has_f) feed_mm_s_ = f / 60.0f;
        float tx = has_x ? x : m_.position_mm(X), ty = has_y ? y : m_.position_mm(Y);
        reply(m_.move_to(tx, ty, g == 0 ? s_.max_speed : feed_mm_s_));
        return;
      }
      if (g == 4) {
        if (!has_p || p < 0) { reply(ERR_NUMBER); return; }
        if (busy()) { reply(ERR_BUSY); return; }
        dwell_until_ = now_us + (uint32_t)(p * 1000000.0f);
        dwelling_ = true;
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

  bool take_disable_request() { bool d = disable_motors_; disable_motors_ = false; return d; }

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

  void reply(int code) {
    if (code == OK) { out_.write("ok\n"); return; }
    static const char* texts[] = {"", "unknown command", "bad number", "busy", "outside the travel limits", "locked: $X to unlock, $H to home"};
    char buf[80];
    snprintf(buf, sizeof buf, "error:%d %s\n", code, code < 6 ? texts[code] : "");
    out_.write(buf);
  }

  void status() {
    const char* name = "Idle";
    switch (m_.state()) {
      case RUN: name = "Run"; break;
      case HOME: name = "Home"; break;
      case HOLD: name = "Hold"; break;
      case ALARM: name = "Alarm"; break;
      default: name = dwelling_ ? "Run" : "Idle";
    }
    char x[16], y[16], buf[120];
    fixed3(m_.position_mm(X), x, sizeof x);
    fixed3(m_.position_mm(Y), y, sizeof y);
    if (m_.state() == ALARM)
      snprintf(buf, sizeof buf, "<Alarm:%s|MPos:%s,%s|Mag:%d|Homed:%d>\n", m_.alarm_reason(), x, y, p_.magnet_on() ? 1 : 0, m_.homed() ? 1 : 0);
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
  }

  Motion& m_;
  Peripherals& p_;
  Writer& out_;
  Settings s_;
  const char* version_;
  float feed_mm_s_;
  uint32_t dwell_until_;
  bool dwelling_;
  bool disable_motors_ = false;
};

}  // namespace gantry
