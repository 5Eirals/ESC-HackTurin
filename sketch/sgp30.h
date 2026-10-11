#pragma once

#include <Arduino.h>
#include <Wire.h>

// Minimal driver for the Sensirion SGP30 gas sensor (I2C address 0x58).
// After begin() the internal algorithm returns fixed values (eCO2 400 ppm, TVOC 0 ppb) for ~15 s,
// and measureIAQ() must be called every ~1 s for the dynamic baseline compensation to work.
class SGP30 {
 public:
  static constexpr uint8_t ADDRESS = 0x58;

  // Reads the serial number (presence check) and starts the IAQ algorithm.
  bool begin(TwoWire &wire = Wire);

  bool measureIAQ(uint16_t &eco2Ppm, uint16_t &tvocPpb);
  // Raw H2 and ethanol signals: they react before eCO2/TVOC, useful for the analysis.
  bool measureRaw(uint16_t &h2Raw, uint16_t &ethanolRaw);

  bool getBaseline(uint16_t &eco2Base, uint16_t &tvocBase);
  bool setBaseline(uint16_t eco2Base, uint16_t tvocBase);

  // Absolute humidity in g/m^3 for the compensation; 0 disables it.
  bool setAbsoluteHumidity(float gramsPerM3);

  // Absolute humidity (g/m^3) from temperature (°C) and relative humidity (%), datasheet formula.
  static float absoluteHumidity(float temperatureC, float humidityPct);

  uint64_t serialNumber() const { return _serial; }

 private:
  TwoWire *_wire = nullptr;
  uint64_t _serial = 0;

  bool command(uint16_t cmd, const uint16_t *args, uint8_t argCount, uint16_t delayMs,
               uint16_t *out, uint8_t outCount);
};
