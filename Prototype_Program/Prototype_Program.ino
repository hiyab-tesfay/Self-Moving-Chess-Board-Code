#include <Arduino.h>

#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "Select ESP32 Dev Module for the classic ESP32-WROOM-32."
#endif

// Motor 2 moves horizontally.
constexpr int DIR_X  = 22;
constexpr int STEP_X = 25;
constexpr int EN_X   = 26;

// Motor 1 moves vertically.
constexpr int DIR_Y  = 18;
constexpr int STEP_Y = 19;
constexpr int EN_Y   = 21;

constexpr int X_LEFT  = HIGH;
constexpr int X_RIGHT = LOW;
constexpr int Y_UP    = HIGH;
constexpr int Y_DOWN  = LOW;

// Tune each axis after measuring its travel.
constexpr int X_STEPS_PER_SQUARE = 50;
constexpr int Y_STEPS_PER_SQUARE = 50;
constexpr int SQUARES_PER_SIDE = 7;

constexpr unsigned int STEP_HALF_PERIOD_US = 1500;
constexpr unsigned long START_DELAY_MS = 3000;
constexpr unsigned long CORNER_PAUSE_MS = 750;

void moveSide(int dirPin, int stepPin, int direction, int pulseCount) {
  digitalWrite(dirPin, direction);
  delayMicroseconds(5);

  for (int pulse = 0; pulse < pulseCount; ++pulse) {
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
  const int horizontalPulses = SQUARES_PER_SIDE * X_STEPS_PER_SQUARE;
  const int verticalPulses = SQUARES_PER_SIDE * Y_STEPS_PER_SQUARE;

  moveSide(DIR_X, STEP_X, X_LEFT,  horizontalPulses);
  moveSide(DIR_Y, STEP_Y, Y_UP,    verticalPulses);
  moveSide(DIR_X, STEP_X, X_RIGHT, horizontalPulses);
  moveSide(DIR_Y, STEP_Y, Y_DOWN,  verticalPulses);
}
