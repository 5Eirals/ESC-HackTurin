#pragma once

#include <Arduino.h>
#include <Wire.h>

// Minimal driver for the AHT20 temperature and humidity sensor (I2C address 0x38).
class AHT20 {
 public:
  static constexpr uint8_t ADDRESS = 0x38;

  // Checks that the sensor is there and, if needed, initialises it (calibration).
  bool begin(TwoWire &wire = Wire);

  // Blocking measurement (~80 ms). Returns false if the sensor does not answer or the CRC is wrong.
  bool read(float &temperatureC, float &humidityPct);

 private:
  TwoWire *_wire = nullptr;

  bool readStatus(uint8_t &status);
};
