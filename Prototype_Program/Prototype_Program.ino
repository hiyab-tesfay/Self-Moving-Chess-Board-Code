// ESP32 Dev Module (classic ESP32-WROOM-32)
// Two NEMA 17 Stepper Motors with DRV8825 Drivers

#include <Arduino.h>

#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "This GPIO mapping requires a classic ESP32. Select ESP32 Dev Module or adapt the pins for your board."
#endif

// Motor 1
#define DIR1  18
#define STEP1 19
#define EN1   21

// Motor 2
#define DIR2  22
#define STEP2 25
#define EN2   26

// Motor settings
const int stepsPerMove = 400;
const int stepDelay = 1500;

void setup() {
  // Motor 1 pins
  pinMode(DIR1, OUTPUT);
  pinMode(STEP1, OUTPUT);
  pinMode(EN1, OUTPUT);

  // Motor 2 pins
  pinMode(DIR2, OUTPUT);
  pinMode(STEP2, OUTPUT);
  pinMode(EN2, OUTPUT);

  // Initialize STEP pins
  digitalWrite(STEP1, LOW);
  digitalWrite(STEP2, LOW);

  // Enable both DRV8825 drivers
  digitalWrite(EN1, LOW);
  digitalWrite(EN2, LOW);

  delay(100);
}

void loop() {

  // Both motors forward
  digitalWrite(DIR1, HIGH);
  digitalWrite(DIR2, HIGH);

  delay(10);

  for (int i = 0; i < stepsPerMove; i++) {

    digitalWrite(STEP1, HIGH);
    digitalWrite(STEP2, HIGH);

    delayMicroseconds(stepDelay);

    digitalWrite(STEP1, LOW);
    digitalWrite(STEP2, LOW);

    delayMicroseconds(stepDelay);
  }

  delay(500);

  // Both motors backward
  digitalWrite(DIR1, LOW);
  digitalWrite(DIR2, LOW);

  delay(10);

  for (int i = 0; i < stepsPerMove; i++) {

    digitalWrite(STEP1, HIGH);
    digitalWrite(STEP2, HIGH);

    delayMicroseconds(stepDelay);

    digitalWrite(STEP1, LOW);
    digitalWrite(STEP2, LOW);

    delayMicroseconds(stepDelay);
  }

  delay(500);
}