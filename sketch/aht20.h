#pragma once

#include <Arduino.h>
#include <Wire.h>

// Driver minimale per il sensore di temperatura e umidita' AHT20 (indirizzo I2C 0x38).
class AHT20 {
 public:
  static constexpr uint8_t ADDRESS = 0x38;

  // Verifica la presenza del sensore e, se serve, lo inizializza (calibrazione).
  bool begin(TwoWire &wire = Wire);

  // Misura bloccante (~80 ms). Ritorna false se il sensore non risponde o il CRC e' errato.
  bool read(float &temperatureC, float &humidityPct);

 private:
  TwoWire *_wire = nullptr;

  bool readStatus(uint8_t &status);
};
