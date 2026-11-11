#include <Arduino.h>

#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "Select ESP32 Dev Module for the classic ESP32-WROOM-32."
#endif

// Motor 1 moves X. Motor 2 moves Y.
constexpr int DIR_X  = 18;
constexpr int STEP_X = 19;
constexpr int EN_X   = 21;

constexpr int DIR_Y  = 22;
constexpr int STEP_Y = 25;
constexpr int EN_Y   = 26;

// Change either value to LOW if that axis moves backward.
constexpr int X_RIGHT = HIGH;
constexpr int Y_UP    = HIGH;

constexpr int STEPS_PER_SQUARE = 400;
constexpr int STEPS_PER_SIDE = 7 * STEPS_PER_SQUARE;

constexpr unsigned int STEP_HALF_PERIOD_US = 1500;
constexpr unsigned long START_DELAY_MS = 3000;
constexpr unsigned long CORNER_PAUSE_MS = 750;

void moveSide(int dirPin, int stepPin, int direction) {
  digitalWrite(dirPin, direction);
  delayMicroseconds(5);

  for (int step = 0; step < STEPS_PER_SIDE; ++step) {
    digitalWrite(stepPin, HIGH);
    delayMicroseconds(STEP_HALF_PERIOD_US);
    digitalWrite(stepPin, LOW);
    delayMicroseconds(STEP_HALF_PERIOD_US);
  }

  delay(CORNER_PAUSE_MS);
}

void setup() {
  pinMode(EN_X, OUTPUT);
  pinMode(EN_Y, OUTPUT);
  digitalWrite(EN_X, HIGH);
  digitalWrite(EN_Y, HIGH);

  pinMode(DIR_X, OUTPUT);
  pinMode(STEP_X, OUTPUT);
  pinMode(DIR_Y, OUTPUT);
  pinMode(STEP_Y, OUTPUT);
  digitalWrite(STEP_X, LOW);
  digitalWrite(STEP_Y, LOW);

  delay(START_DELAY_MS);

  // DRV8825 drivers are enabled when EN is LOW.
  digitalWrite(EN_X, LOW);
  digitalWrite(EN_Y, LOW);
  delay(100);
}

void loop() {
  moveSide(DIR_X, STEP_X, !X_RIGHT);  // Left:  h1 -> a1
  moveSide(DIR_Y, STEP_Y, Y_UP);      // Up:    a1 -> a8
  moveSide(DIR_X, STEP_X, X_RIGHT);   // Right: a8 -> h8
  moveSide(DIR_Y, STEP_Y, !Y_UP);     // Down:  h8 -> h1
}