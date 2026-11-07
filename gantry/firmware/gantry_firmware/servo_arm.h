// The MG90 arm that lifts the magnet, driven gently (firmware 0.7.2). Until 0.7.1 the pulse jumped
// straight to the new angle and was held there for good; on the bench of 2026-11-06 the ESP32's
// serial line garbled the moment M7 was sent and the USB link dropped seconds later, the servo
// starting at full current (and stalling at an end) pulling the 5 V down. Now the pulse sweeps to
// the angle the protocol asks for at `sweep` degrees a second, and once the arm has been down for
// `release_down_ms` the pulses stop: the arm rests on its own weight instead of pushing at an end
// stop. Up, it keeps being held (it presses the magnet to the board). A sweep starts from the last
// angle sent, which is where the arm rests after a release, so it never jumps. No Arduino calls:
// the .ino gives it the function that sets the pulse (0 = no pulses), host/ a recorder.
#pragma once

#include <stdint.h>

namespace gantry {

class ServoArm {
 public:
  typedef void (*PulseFn)(uint32_t pulse_us);

  ServoArm(PulseFn out, uint32_t min_us, uint32_t max_us, float sweep_deg_per_s, uint32_t release_down_ms, int rest_deg)
      : out_(out), min_us_(min_us), max_us_(max_us), sweep_(sweep_deg_per_s), release_us_(release_down_ms * 1000UL),
        at_((float)rest_deg), target_((float)rest_deg) {}

  // The pulse width for an angle, 0-180 deg across min_us..max_us.
  uint32_t pulse_for(float deg) const {
    if (deg < 0) deg = 0;
    if (deg > 180) deg = 180;
    return min_us_ + (uint32_t)(deg * (float)(max_us_ - min_us_) / 180.0f + 0.5f);
  }

  // At power-up nothing knows where the arm is: one step to the angle, then the usual release.
  void start(int angle, uint32_t now_us) {
    at_ = (float)clamp(angle);
    go(angle, now_us);
  }

  // The protocol wants the arm at this angle (M7, M8, a carry's steps, $S).
  void go(int angle, uint32_t now_us) {
    target_ = (float)clamp(angle);
    arrived_ = false;
    last_us_ = now_us;
    pulsing_ = true;
    out_(pulse_for(at_));
  }

  // Call often (every loop). `down`: the protocol has the arm down, so it may be let go once there.
  void tick(uint32_t now_us, bool down) {
    if (!pulsing_) return;
    uint32_t dt = now_us - last_us_;
    if (dt < 5000) return;   // the servo only reads the pulse every 20 ms anyway
    last_us_ = now_us;
    if (at_ != target_) {
      if (dt > 20000) dt = 20000;   // a late tick still moves one small step, never a jump
      float step = sweep_ > 0 ? sweep_ * ((float)dt / 1e6f) : 180.0f;
      if (at_ < target_) at_ = at_ + step > target_ ? target_ : at_ + step;
      else at_ = at_ - step < target_ ? target_ : at_ - step;
      out_(pulse_for(at_));
      return;
    }
    if (!arrived_) {
      arrived_ = true;
      arrived_us_ = now_us;
      return;
    }
    if (down && release_us_ > 0 && now_us - arrived_us_ >= release_us_) {
      out_(0);
      pulsing_ = false;
    }
  }

  float angle() const { return at_; }
  bool pulsing() const { return pulsing_; }

 private:
  static int clamp(int angle) { return angle < 0 ? 0 : angle > 180 ? 180 : angle; }

  PulseFn out_;
  uint32_t min_us_, max_us_;
  float sweep_;
  uint32_t release_us_;
  float at_, target_;
  bool pulsing_ = false, arrived_ = false;
  uint32_t last_us_ = 0, arrived_us_ = 0;
};

}  // namespace gantry
