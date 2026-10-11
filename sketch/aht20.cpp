#include "aht20.h"

#include "sensirion_crc.h"

namespace {
const uint8_t CMD_STATUS = 0x71;
const uint8_t CMD_INIT[] = {0xBE, 0x08, 0x00};
const uint8_t CMD_MEASURE[] = {0xAC, 0x33, 0x00};

const uint8_t STATUS_BUSY = 0x80;
const uint8_t STATUS_CALIBRATED = 0x08;

const uint8_t FRAME_LEN = 7;          // status + 5 data bytes + CRC
const float RAW_FULL_SCALE = 1048576.0f;  // 2^20
}  // namespace

bool AHT20::begin(TwoWire &wire) {
  _wire = &wire;
  delay(40);  // start-up time after power-on (datasheet)

  uint8_t status;
  if (!readStatus(status)) return false;
  if (status & STATUS_CALIBRATED) return true;

  _wire->beginTransmission(ADDRESS);
  _wire->write(CMD_INIT, sizeof(CMD_INIT));
  if (_wire->endTransmission() != 0) return false;
  delay(10);

  return readStatus(status) && (status & STATUS_CALIBRATED);
}

bool AHT20::readStatus(uint8_t &status) {
  _wire->beginTransmission(ADDRESS);
  _wire->write(CMD_STATUS);
  if (_wire->endTransmission() != 0) return false;
  if (_wire->requestFrom(ADDRESS, (size_t)1) != 1) return false;
  status = _wire->read();
  return true;
}

bool AHT20::read(float &temperatureC, float &humidityPct) {
  if (_wire == nullptr) return false;

  _wire->beginTransmission(ADDRESS);
  _wire->write(CMD_MEASURE, sizeof(CMD_MEASURE));
  if (_wire->endTransmission() != 0) return false;
  delay(80);  // measurement time (datasheet)

  uint8_t buf[FRAME_LEN];
  for (uint8_t attempt = 0;; attempt++) {
    if (_wire->requestFrom(ADDRESS, (size_t)FRAME_LEN) != FRAME_LEN) return false;
    for (uint8_t i = 0; i < FRAME_LEN; i++) buf[i] = _wire->read();
    if (!(buf[0] & STATUS_BUSY)) break;
    if (attempt >= 5) return false;
    delay(10);
  }
  if (crc8_31(buf, FRAME_LEN - 1) != buf[FRAME_LEN - 1]) return false;

  uint32_t rawHumidity = ((uint32_t)buf[1] << 12) | ((uint32_t)buf[2] << 4) | (buf[3] >> 4);
  uint32_t rawTemperature = (((uint32_t)buf[3] & 0x0F) << 16) | ((uint32_t)buf[4] << 8) | buf[5];
  humidityPct = rawHumidity * 100.0f / RAW_FULL_SCALE;
  temperatureC = rawTemperature * 200.0f / RAW_FULL_SCALE - 50.0f;
  return true;
}
