#pragma once

#include <Arduino.h>
#include <Wire.h>

// Driver minimale per il sensore di gas Sensirion SGP30 (indirizzo I2C 0x58).
// Dopo begin() l'algoritmo interno restituisce valori fissi (eCO2 400 ppm, TVOC 0 ppb) per ~15 s,
// e measureIAQ() va chiamata ogni ~1 s perche' la compensazione dinamica della baseline funzioni.
class SGP30 {
 public:
  static constexpr uint8_t ADDRESS = 0x58;

  // Legge il numero di serie (verifica di presenza) e avvia l'algoritmo IAQ.
  bool begin(TwoWire &wire = Wire);

  bool measureIAQ(uint16_t &eco2Ppm, uint16_t &tvocPpb);
  // Segnali grezzi H2 ed etanolo: rispondono prima di eCO2/TVOC, utili per l'analisi.
  bool measureRaw(uint16_t &h2Raw, uint16_t &ethanolRaw);

  bool getBaseline(uint16_t &eco2Base, uint16_t &tvocBase);
  bool setBaseline(uint16_t eco2Base, uint16_t tvocBase);

  // Umidita' assoluta in g/m^3 per la compensazione; 0 la disattiva.
  bool setAbsoluteHumidity(float gramsPerM3);

  // Umidita' assoluta (g/m^3) da temperatura (°C) e umidita' relativa (%), formula del datasheet.
  static float absoluteHumidity(float temperatureC, float humidityPct);

  uint64_t serialNumber() const { return _serial; }

 private:
  TwoWire *_wire = nullptr;
  uint64_t _serial = 0;

  bool command(uint16_t cmd, const uint16_t *args, uint8_t argCount, uint16_t delayMs,
               uint16_t *out, uint8_t outCount);
};
