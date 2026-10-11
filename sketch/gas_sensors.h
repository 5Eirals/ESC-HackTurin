#pragma once

#include <Arduino.h>

// Gas side on the MCU: AHT20 (temperature/humidity) and SGP30 (TVOC/eCO2 + raw signals) on the
// I2C bus of the Grove Base Shield (Wire). The readings go to Python, where the logic lives.

// One set of readings (values are 0 when the sensor did not answer).
struct GasSample {
  bool ahtOk = false;
  bool sgpOk = false;
  float temperatureC = 0.0f;
  float humidityPct = 0.0f;
  uint16_t eco2 = 0, tvoc = 0, h2Raw = 0, ethanolRaw = 0;
};

// Starts the I2C bus, prints the devices found and initialises the two sensors.
void gasBegin();

// True once every SAMPLE_PERIOD_MS: the SGP30 wants one IAQ measurement every ~1 s.
bool sampleDue(unsigned long now);

// New attempt every RETRY_PERIOD_MS for a sensor that is missing or was reset.
void retryMissingSensors(unsigned long now);

// Reads AHT20 and SGP30 (the SGP30 is compensated with the AHT20 humidity).
GasSample readGasSensors();

// sensor_data message to Python.
void sendSensorData(const GasSample &s);

// sgp30_baseline message to Python, every BASELINE_PERIOD_MS.
void sendBaselineIfDue();

// Called by Python at start-up with the saved baseline. Runs in the loop thread
// (provide_safe), so it does not overlap with the I2C readings.
bool sgp30_set_baseline(int eco2Base, int tvocBase);
