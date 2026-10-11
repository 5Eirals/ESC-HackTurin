#include "sgp30.h"

#include <math.h>

#include "sensirion_crc.h"

namespace {
const uint16_t CMD_GET_SERIAL_ID = 0x3682;
const uint16_t CMD_IAQ_INIT = 0x2003;
const uint16_t CMD_MEASURE_IAQ = 0x2008;
const uint16_t CMD_GET_IAQ_BASELINE = 0x2015;
const uint16_t CMD_SET_IAQ_BASELINE = 0x201E;
const uint16_t CMD_SET_ABSOLUTE_HUMIDITY = 0x2061;
const uint16_t CMD_MEASURE_RAW = 0x2050;

const uint8_t MAX_WORDS = 3;
}  // namespace

bool SGP30::begin(TwoWire &wire) {
  _wire = &wire;

  uint16_t serial[3];
  if (!command(CMD_GET_SERIAL_ID, nullptr, 0, 10, serial, 3)) return false;
  _serial = ((uint64_t)serial[0] << 32) | ((uint64_t)serial[1] << 16) | serial[2];

  return command(CMD_IAQ_INIT, nullptr, 0, 10, nullptr, 0);
}

bool SGP30::measureIAQ(uint16_t &eco2Ppm, uint16_t &tvocPpb) {
  uint16_t out[2];
  if (!command(CMD_MEASURE_IAQ, nullptr, 0, 12, out, 2)) return false;
  eco2Ppm = out[0];
  tvocPpb = out[1];
  return true;
}

bool SGP30::measureRaw(uint16_t &h2Raw, uint16_t &ethanolRaw) {
  uint16_t out[2];
  if (!command(CMD_MEASURE_RAW, nullptr, 0, 25, out, 2)) return false;
  h2Raw = out[0];
  ethanolRaw = out[1];
  return true;
}

bool SGP30::getBaseline(uint16_t &eco2Base, uint16_t &tvocBase) {
  uint16_t out[2];
  if (!command(CMD_GET_IAQ_BASELINE, nullptr, 0, 10, out, 2)) return false;
  eco2Base = out[0];
  tvocBase = out[1];
  return true;
}

bool SGP30::setBaseline(uint16_t eco2Base, uint16_t tvocBase) {
  // Careful: when writing, the datasheet wants TVOC first and then eCO2 (reverse of the read order).
  const uint16_t args[2] = {tvocBase, eco2Base};
  return command(CMD_SET_IAQ_BASELINE, args, 2, 10, nullptr, 0);
}

bool SGP30::setAbsoluteHumidity(float gramsPerM3) {
  // 8.8 fixed-point format (g/m^3); the largest value it can hold is 255.996.
  if (gramsPerM3 < 0.0f) gramsPerM3 = 0.0f;
  if (gramsPerM3 > 255.0f) gramsPerM3 = 255.0f;
  const uint16_t arg = (uint16_t)lroundf(gramsPerM3 * 256.0f);
  return command(CMD_SET_ABSOLUTE_HUMIDITY, &arg, 1, 10, nullptr, 0);
}

float SGP30::absoluteHumidity(float temperatureC, float humidityPct) {
  const float saturation = 6.112f * expf(17.62f * temperatureC / (243.12f + temperatureC));
  return 216.7f * (humidityPct / 100.0f) * saturation / (273.15f + temperatureC);
}

bool SGP30::command(uint16_t cmd, const uint16_t *args, uint8_t argCount, uint16_t delayMs,
                    uint16_t *out, uint8_t outCount) {
  if (_wire == nullptr || outCount > MAX_WORDS) return false;

  _wire->beginTransmission(ADDRESS);
  _wire->write((uint8_t)(cmd >> 8));
  _wire->write((uint8_t)(cmd & 0xFF));
  for (uint8_t i = 0; i < argCount; i++) {
    const uint8_t word[2] = {(uint8_t)(args[i] >> 8), (uint8_t)(args[i] & 0xFF)};
    _wire->write(word, 2);
    _wire->write(crc8_31(word, 2));
  }
  if (_wire->endTransmission() != 0) return false;

  delay(delayMs);
  if (outCount == 0) return true;

  const size_t len = (size_t)outCount * 3;  // every word: 2 bytes + CRC
  if (_wire->requestFrom(ADDRESS, len) != len) return false;
  for (uint8_t i = 0; i < outCount; i++) {
    uint8_t word[2];
    word[0] = _wire->read();
    word[1] = _wire->read();
    const uint8_t crc = _wire->read();
    if (crc8_31(word, 2) != crc) return false;
    out[i] = ((uint16_t)word[0] << 8) | word[1];
  }
  return true;
}
