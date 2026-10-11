// TO-Breath: microcontroller firmware (STM32U585 of the UNO Q).
//
// 1) Vision / fill level (depth_fill.py): drives the LED ring (flash) and reads the Modulino
//    Distance -> vision_io.h / vision_io.cpp
// 2) Gas: reads AHT20 (temperature/humidity) and SGP30 (TVOC/eCO2 + raw signals) on the I2C bus
//    of the Grove Base Shield (Wire) and sends everything to Python via Bridge, where the logic
//    lives -> gas_sensors.h / gas_sensors.cpp (drivers in aht20.* and sgp30.*)
//
// Messages to Python:
//   sensor_data(uptime_s, aht_ok, sgp_ok, temp_c, rh_pct, tvoc_ppb, eco2_ppm, h2_raw, ethanol_raw)  every 1 s
//   sgp30_started(uptime_s)                         every time the SGP30 algorithm restarts from zero
//   sgp30_baseline(uptime_s, eco2_base, tvoc_base)  every 60 s, to be saved
// Functions Python can call:
//   set_flash(level)                          LED ring brightness (0-255)
//   read_tof() -> int                         ToF distance in mm (median of 5), -1 if not valid
//   camera_status(ok)                         outcome of the last phone photo (printed on the Monitor)
//   sgp30_set_baseline(eco2_base, tvoc_base) -> bool
//
// Library names/APIs to verify in your App Lab / Arduino IDE version:
//  - Arduino_RouterBridge (Bridge)
//  - Modulino
//  - Adafruit_NeoPixel works with WS2813; if it doesn't compile on the UNO Q core,
//    look in the Library Manager for a WS28xx library that supports it.

#include <Arduino_RouterBridge.h>

#include "gas_sensors.h"
#include "vision_io.h"

// Functions Python can call through the Bridge.
void provideFunctions() {
  Bridge.provide("set_flash", set_flash);
  // provide_safe: read_tof runs in the loop thread, so it does not use the I2C in parallel with
  // the AHT20/SGP30 readings (if the Modulino ends up on the same Wire bus).
  Bridge.provide_safe("read_tof", read_tof);
  Bridge.provide_safe("camera_status", camera_status); // photo outcome on the Monitor
  Bridge.provide_safe("sgp30_set_baseline", sgp30_set_baseline);
}

void setup() {
  Bridge.begin();
  Monitor.begin(115200);

  visionBegin();        // Modulino, ToF, LED ring off
  provideFunctions();
  gasBegin();           // I2C scan, AHT20 + SGP30
}

void loop() {
  const unsigned long now = millis();
  if (!sampleDue(now)) return;

  retryMissingSensors(now);
  sendSensorData(readGasSensors());
  sendBaselineIfDue();
}
