#pragma once

#include <stddef.h>
#include <stdint.h>

// CRC-8 used by both SGP30 and AHT20: polynomial 0x31 (x^8 + x^5 + x^4 + 1), initial value 0xFF.
// Example from the SGP30 datasheet: crc8_31({0xBE, 0xEF}) == 0x92.
inline uint8_t crc8_31(const uint8_t *data, size_t len) {
  uint8_t crc = 0xFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}
