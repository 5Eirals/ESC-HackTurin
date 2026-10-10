// ESC-HackTurin: firmware del microcontrollore (STM32U585 della UNO Q).
//
// 1) Visione / riempimento (depth_fill.py): pilota l'anello LED (flash) e legge il Modulino Distance.
// 2) Gas: legge AHT20 (temperatura/umidita') e SGP30 (TVOC/eCO2 + segnali grezzi) sul bus I2C
//    del Grove Base Shield (Wire) e invia tutto a Python via Bridge, dove sta la logica.
//
// Messaggi verso Python:
//   sensor_data(uptime_s, aht_ok, sgp_ok, temp_c, rh_pct, tvoc_ppb, eco2_ppm, h2_raw, ethanol_raw)  ogni 1 s
//   sgp30_started(uptime_s)                         ogni volta che l'algoritmo SGP30 riparte da zero
//   sgp30_baseline(uptime_s, eco2_base, tvoc_base)  ogni 60 s, da salvare
// Funzioni chiamabili da Python:
//   set_flash(level)                          luminosita' dell'anello LED (0-255)
//   read_tof() -> int                         distanza ToF in mm (mediana di 5), -1 se non valida
//   sgp30_set_baseline(eco2_base, tvoc_base) -> bool
//
// Library names/APIs to verify in your App Lab / Arduino IDE version:
//  - Arduino_RouterBridge (Bridge)
//  - Modulino
//  - Adafruit_NeoPixel works with WS2813; if it doesn't compile on the UNO Q core,
//    look in the Library Manager for a WS28xx library that supports it.

#include <Arduino_RouterBridge.h>
#include <Wire.h>
#include <Arduino_Modulino.h>
#include <Adafruit_NeoPixel.h>

#include "aht20.h"
#include "sgp30.h"

// ---------------------------------------------------------------- visione: anello LED + ToF
#define RING_PIN 6        // Grove D6 on the Base Shield
#define RING_LEDS 16

Adafruit_NeoPixel ring(RING_LEDS, RING_PIN, NEO_GRB + NEO_KHZ800);
ModulinoDistance distance;

void set_flash(int level) {
  for (int i = 0; i < RING_LEDS; i++) ring.setPixelColor(i, ring.Color(level, level, level));
  ring.show();
}

int read_tof() {
  // median of 5 readings, -1 if no valid reading
  int v[5], n = 0;
  unsigned long t0 = millis();
  while (n < 5 && millis() - t0 < 500) {
    if (distance.available()) v[n++] = distance.get();
    delay(20);
  }
  if (n == 0) return -1;
  for (int i = 0; i < n; i++)            // tiny sort
    for (int j = i + 1; j < n; j++)
      if (v[j] < v[i]) { int t = v[i]; v[i] = v[j]; v[j] = t; }
  return v[n / 2];
}

// ---------------------------------------------------------------- gas: AHT20 + SGP30
const unsigned long SAMPLE_PERIOD_MS = 1000;     // l'SGP30 vuole una misura IAQ ogni ~1 s
const unsigned long BASELINE_PERIOD_MS = 60000;  // invio della baseline a Python
const unsigned long RETRY_PERIOD_MS = 5000;      // nuovo tentativo se un sensore non e' pronto
const uint8_t MAX_CONSECUTIVE_FAILURES = 5;      // dopo N letture fallite il sensore si reinizializza

AHT20 aht;
SGP30 sgp;

bool ahtReady = false;
bool sgpReady = false;
uint8_t ahtFailures = 0;
uint8_t sgpFailures = 0;

unsigned long lastSampleMs = 0;
unsigned long lastBaselineMs = 0;
unsigned long lastRetryMs = 0;

int uptimeSeconds() { return (int)(millis() / 1000); }

// Eseguita nel thread del loop (provide_safe), quindi non si sovrappone alle letture I2C.
bool sgp30_set_baseline(int eco2Base, int tvocBase) {
  if (!sgpReady) return false;
  bool ok = sgp.setBaseline((uint16_t)eco2Base, (uint16_t)tvocBase);
  Monitor.print("SGP30 baseline ripristinata: ");
  Monitor.println(ok ? "ok" : "ERRORE");
  return ok;
}

void scanI2C() {
  Monitor.print("Scansione I2C:");
  uint8_t found = 0;
  for (uint8_t address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      Monitor.print(" 0x");
      Monitor.print(address, HEX);
      found++;
    }
  }
  Monitor.println(found ? "" : " nessun dispositivo (controlla cavi e switch 3,3 V dello shield)");
}

void initSensors() {
  if (!ahtReady) {
    ahtReady = aht.begin(Wire);
    ahtFailures = 0;
    Monitor.println(ahtReady ? "AHT20 pronto" : "AHT20 non trovato (atteso 0x38)");
  }
  if (!sgpReady) {
    sgpReady = sgp.begin(Wire);
    sgpFailures = 0;
    Monitor.println(sgpReady ? "SGP30 pronto (riscaldamento ~15 s)" : "SGP30 non trovato (atteso 0x58)");
    if (sgpReady) {
      lastBaselineMs = millis();
      Bridge.notify("sgp30_started", uptimeSeconds());
    }
  }
}

void setup() {
  Bridge.begin();
  Monitor.begin(115200);

  Modulino.begin();
  distance.begin();
  ring.begin();
  ring.setBrightness(255);
  set_flash(0);
  Bridge.provide("set_flash", set_flash);
  // provide_safe: read_tof gira nel thread del loop, cosi' non usa l'I2C in parallelo alle letture
  // di AHT20/SGP30 (se il Modulino finisce sullo stesso bus Wire).
  Bridge.provide_safe("read_tof", read_tof);
  Bridge.provide("camera_status", camera_status); // per controllare la camera
  Bridge.provide_safe("sgp30_set_baseline", sgp30_set_baseline);

  Wire.begin();
  scanI2C();
  initSensors();
>>>>>>> 487c4588a5105a68c34fe67c97c94030d975acc8
}

void loop() {
  const unsigned long now = millis();
  if (now - lastSampleMs < SAMPLE_PERIOD_MS) return;
  lastSampleMs = now;

  if ((!ahtReady || !sgpReady) && now - lastRetryMs >= RETRY_PERIOD_MS) {
    lastRetryMs = now;
    initSensors();
  }

  float temperatureC = 0.0f;
  float humidityPct = 0.0f;
  bool ahtOk = false;
  if (ahtReady) {
    ahtOk = aht.read(temperatureC, humidityPct);
    ahtFailures = ahtOk ? 0 : ahtFailures + 1;
    if (ahtFailures >= MAX_CONSECUTIVE_FAILURES) ahtReady = false;
  }

  uint16_t eco2 = 0, tvoc = 0, h2Raw = 0, ethanolRaw = 0;
  bool sgpOk = false;
  if (sgpReady) {

    if (ahtOk) sgp.setAbsoluteHumidity(SGP30::absoluteHumidity(temperatureC, humidityPct));
    sgpOk = sgp.measureIAQ(eco2, tvoc) && sgp.measureRaw(h2Raw, ethanolRaw);
    sgpFailures = sgpOk ? 0 : sgpFailures + 1;
    if (sgpFailures >= MAX_CONSECUTIVE_FAILURES) sgpReady = false;
  }

  Bridge.notify("sensor_data", uptimeSeconds(), ahtOk, sgpOk, temperatureC, humidityPct,
                (int)tvoc, (int)eco2, (int)h2Raw, (int)ethanolRaw);

  if (sgpReady && millis() - lastBaselineMs >= BASELINE_PERIOD_MS) {
    lastBaselineMs = millis();
    uint16_t eco2Base, tvocBase;
    if (sgp.getBaseline(eco2Base, tvocBase)) {
      Bridge.notify("sgp30_baseline", uptimeSeconds(), (int)eco2Base, (int)tvocBase);
    }
  }

}
