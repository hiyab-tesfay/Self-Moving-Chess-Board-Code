// Step generation for two belt axes: trapezoidal speed profile, Bresenham for the second
// axis, soft limits, homing. Plain C++ with no Arduino calls, so the same code runs on this
// computer for the tests (host/) and on the board (gantry_firmware.ino supplies the Hal).
//
// Positions are kept in motor steps. "Cartesian" mm are what the Pi talks in; with COREXY the
// two motors move a = x + y and b = x - y instead, and this file does that conversion.
#pragma once

#include <math.h>
#include <stdint.h>

namespace gantry {

enum Axis { X = 0, Y = 1 };

// What the board must provide. One implementation lives in the .ino, one in host/.
struct Hal {
  virtual ~Hal() {}
  virtual void set_dir(int motor, bool positive) = 0;
  virtual void step(uint8_t motor_mask) = 0;      // bit 0 = motor 0, bit 1 = motor 1, pulsed together
  virtual void enable_motors(bool on) = 0;
  virtual bool endstop(int axis) = 0;             // true while the min switch is pressed; always false without switches
  virtual uint32_t now_us() = 0;                  // wraps like Arduino micros(); only differences are used
};

struct Settings {
  float steps_per_mm;
  float max_mm[2];
  float max_speed;        // mm/s
  float accel;            // mm/s^2
  float homing_speed;     // mm/s
  float homing_bump;      // mm past the end, when there are no switches
  float pull_off;         // mm to back away from the end after homing
  bool invert_dir[2];
  bool corexy;
  bool use_endstops;
  bool home_x_first;
};

enum State { ALARM, IDLE, RUN, HOME, HOLD };

// Error codes shared with protocol.h.
enum {
  OK = 0,
  ERR_BUSY = 3,       // a move is already running
  ERR_LIMIT = 4,      // target outside the soft limits
  ERR_ALARM = 5,      // locked: $X to unlock, $H to home
};

class Motion {
 public:
  Motion(Hal& hal, const Settings& s) : hal_(hal), s_(s) {
    motor_[0] = motor_[1] = 0;
    state_ = ALARM;
    homed_ = false;
    alarm_ = "not homed";
    n_ = done_ = 0;
  }

  State state() const { return state_; }
  bool homed() const { return homed_; }
  const char* alarm_reason() const { return alarm_; }
  bool moving() const { return state_ == RUN || state_ == HOME; }

  float position_mm(int axis) const {
    long cx, cy;
    cartesian_steps(cx, cy);
    return (axis == X ? cx : cy) / s_.steps_per_mm;
  }

  // Absolute move in cartesian mm at up to speed mm/s. 0 on success, else an error code.
  int move_to(float x_mm, float y_mm, float speed) {
    if (state_ == ALARM || state_ == HOLD) return ERR_ALARM;
    if (state_ != IDLE) return ERR_BUSY;
    if (x_mm < -0.001f || y_mm < -0.001f || x_mm > s_.max_mm[X] + 0.001f || y_mm > s_.max_mm[Y] + 0.001f) return ERR_LIMIT;
    long cx, cy;
    cartesian_steps(cx, cy);
    long tx = lroundf(x_mm * s_.steps_per_mm), ty = lroundf(y_mm * s_.steps_per_mm);
    if (speed <= 0 || speed > s_.max_speed) speed = s_.max_speed;
    start_segment(tx - cx, ty - cy, speed, false);
    if (n_ == 0) return OK;  // already there
    state_ = RUN;
    return OK;
  }

  int home() {
    if (state_ == RUN || state_ == HOME) return ERR_BUSY;
    hal_.enable_motors(true);
    homed_ = false;
    state_ = HOME;
    phase_ = 0;
    next_home_phase();
    return OK;
  }

  // ALARM/HOLD -> IDLE at the current count. After power-up that count is 0, so you can only
  // move in the positive direction until $H has found the real zero.
  void unlock() {
    if (state_ == ALARM || state_ == HOLD) {
      state_ = IDLE;
      alarm_ = "";
    }
  }

  // G92: call the current spot x_mm, y_mm.
  void set_position(float x_mm, float y_mm) {
    if (moving()) return;
    motors_from_cartesian(lroundf(x_mm * s_.steps_per_mm), lroundf(y_mm * s_.steps_per_mm), motor_[0], motor_[1]);
  }

  // Stop at once. The step count stays valid, so $X can resume from here without homing.
  void hold() {
    if (moving()) {
      state_ = HOLD;
      alarm_ = "hold";
      n_ = done_ = 0;
    }
  }

  void alarm(const char* reason) {
    state_ = ALARM;
    alarm_ = reason;
    homed_ = false;
    n_ = done_ = 0;
  }

  // Call as often as possible. Issues at most one step per call.
  void run() {
    if (!moving() || done_ >= n_) return;
    uint32_t now = hal_.now_us();
    if ((int32_t)(now - next_step_at_) < 0) return;
    if (seek_ && s_.use_endstops && hal_.endstop(seek_axis_)) {
      finish_segment();
      return;
    }
    // Bresenham: the dominant motor steps every tick, the other one when its error overflows.
    uint8_t mask = (uint8_t)(1 << dominant_);
    err_ += delta_abs_[1 - dominant_];
    if (2 * err_ >= n_) {
      err_ -= n_;
      mask |= (uint8_t)(1 << (1 - dominant_));
    }
    hal_.step(mask);
    for (int m = 0; m < 2; m++)
      if (mask & (1 << m)) motor_[m] += dir_[m];
    done_++;
    if (done_ >= n_) {
      finish_segment();
      return;
    }
    // Fall behind gracefully: if the loop was late, the next step is due relative to now.
    uint32_t interval = interval_us(done_);
    uint32_t planned = next_step_at_ + interval;
    next_step_at_ = ((int32_t)(now - planned) > 0) ? now + interval : planned;
  }

  // For tests and the status report.
  long motor_steps(int motor) const { return motor_[motor]; }
  long segment_steps() const { return n_; }

 private:
  void cartesian_steps(long& x, long& y) const {
    if (s_.corexy) {
      x = (motor_[0] + motor_[1]) / 2;
      y = (motor_[0] - motor_[1]) / 2;
    } else {
      x = motor_[0];
      y = motor_[1];
    }
  }

  void motors_from_cartesian(long x, long y, long& a, long& b) const {
    if (s_.corexy) {
      a = x + y;
      b = x - y;
    } else {
      a = x;
      b = y;
    }
  }

  // dx, dy in cartesian steps. speed in mm/s along the path.
  void start_segment(long dx, long dy, float speed, bool seek) {
    long da, db;
    motors_from_cartesian(dx, dy, da, db);
    delta_abs_[0] = da < 0 ? -da : da;
    delta_abs_[1] = db < 0 ? -db : db;
    dir_[0] = da < 0 ? -1 : 1;
    dir_[1] = db < 0 ? -1 : 1;
    dominant_ = delta_abs_[1] > delta_abs_[0] ? 1 : 0;
    n_ = delta_abs_[dominant_];
    done_ = 0;
    err_ = 0;
    seek_ = seek;
    if (n_ == 0) return;
    hal_.enable_motors(true);
    for (int m = 0; m < 2; m++) hal_.set_dir(m, (dir_[m] > 0) != s_.invert_dir[m]);
    // The profile runs along the dominant motor's steps. Scale the path speed so that the
    // carriage really moves at `speed` mm/s along a diagonal, not faster.
    float path_steps = sqrtf((float)dx * dx + (float)dy * dy);
    float scale = path_steps > 0 ? (float)n_ / path_steps : 1.0f;
    v_max_ = speed * s_.steps_per_mm * scale;
    a_ = s_.accel * s_.steps_per_mm * scale;
    v_min_ = v_max_ * 0.05f;
    if (v_min_ < 20.0f) v_min_ = 20.0f;  // steps/s; keeps the first interval under 50 ms
    // Steps needed to reach v_max, or half the move if it is too short to get there.
    float ramp = (v_max_ * v_max_ - v_min_ * v_min_) / (2.0f * a_);
    if (ramp * 2 > n_) ramp = n_ / 2.0f;
    ramp_ = (long)ramp;
    next_step_at_ = hal_.now_us() + interval_us(0);
  }

  // Interval before step i (0-based) of the current segment, in microseconds.
  uint32_t interval_us(long i) const {
    float v;
    if (i < ramp_) v = sqrtf(v_min_ * v_min_ + 2.0f * a_ * i);
    else if (n_ - i <= ramp_) v = sqrtf(v_min_ * v_min_ + 2.0f * a_ * (float)(n_ - i));
    else v = v_max_;
    if (v < v_min_) v = v_min_;
    if (v > v_max_) v = v_max_;
    return (uint32_t)(1000000.0f / v);
  }

  void finish_segment() {
    n_ = done_ = 0;
    if (state_ == HOME) {
      phase_++;
      next_home_phase();
    } else {
      state_ = IDLE;
    }
  }

  // Homing phases: for each axis, seek the minimum end, call it 0, pull off, call that 0.
  void next_home_phase() {
    int first = s_.home_x_first ? X : Y, second = 1 - first;
    int axis = phase_ < 2 ? first : second;
    switch (phase_) {
      case 0:
      case 2: {
        float travel = s_.max_mm[axis] + (s_.use_endstops ? 10.0f : s_.homing_bump);
        long d = lroundf(travel * s_.steps_per_mm);
        seek_axis_ = axis;
        start_segment(axis == X ? -d : 0, axis == Y ? -d : 0, s_.homing_speed, true);
        return;
      }
      case 1:
      case 3: {
        zero_axis(axis);
        long d = lroundf(s_.pull_off * s_.steps_per_mm);
        start_segment(axis == X ? d : 0, axis == Y ? d : 0, s_.homing_speed, false);
        return;
      }
      default:
        zero_axis(first);
        zero_axis(second);
        homed_ = true;
        state_ = IDLE;
        alarm_ = "";
        return;
    }
  }

  void zero_axis(int axis) {
    long cx, cy;
    cartesian_steps(cx, cy);
    if (axis == X) cx = 0; else cy = 0;
    motors_from_cartesian(cx, cy, motor_[0], motor_[1]);
  }

  Hal& hal_;
  Settings s_;
  State state_;
  bool homed_;
  const char* alarm_;
  long motor_[2];
  // Current segment.
  long delta_abs_[2], n_, done_, err_, ramp_;
  int dir_[2], dominant_;
  float v_max_, v_min_, a_;
  uint32_t next_step_at_;
  bool seek_;
  int seek_axis_;
  int phase_;
};

}  // namespace gantry
