#include "gas_sensors.h"

#include <Arduino_RouterBridge.h>
#include <Wire.h>

#include "aht20.h"
#include "sgp30.h"

const unsigned long SAMPLE_PERIOD_MS = 1000;     // the SGP30 wants one IAQ measurement every ~1 s
const unsigned long BASELINE_PERIOD_MS = 60000;  // baseline sent to Python
const unsigned long RETRY_PERIOD_MS = 5000;      // new attempt if a sensor is not ready
const uint8_t MAX_CONSECUTIVE_FAILURES = 5;      // after N failed readings the sensor is re-initialised

AHT20 aht;
SGP30 sgp;

bool ahtReady = false;
bool sgpReady = false;
uint8_t ahtFailures = 0;
uint8_t sgpFailures = 0;

unsigned long lastSampleMs = 0;
unsigned long lastBaselineMs = 0;
unsigned long lastRetryMs = 0;

static int uptimeSeconds() { return (int)(millis() / 1000); }

// --- Start-up --------------------------------------------------------------------------------
static void scanI2C() {
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

static void initSensors() {
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

void gasBegin() {
  Wire.begin();
  scanI2C();
  initSensors();
}

// --- Loop steps ------------------------------------------------------------------------------
bool sampleDue(unsigned long now) {
  if (now - lastSampleMs < SAMPLE_PERIOD_MS) return false;
  lastSampleMs = now;
  return true;
}

void retryMissingSensors(unsigned long now) {
  if ((!ahtReady || !sgpReady) && now - lastRetryMs >= RETRY_PERIOD_MS) {
    lastRetryMs = now;
    initSensors();
  }
}

GasSample readGasSensors() {
  GasSample s;
  if (ahtReady) {
    s.ahtOk = aht.read(s.temperatureC, s.humidityPct);
    ahtFailures = s.ahtOk ? 0 : ahtFailures + 1;
    if (ahtFailures >= MAX_CONSECUTIVE_FAILURES) ahtReady = false;
  }
  if (sgpReady) {
    if (s.ahtOk) sgp.setAbsoluteHumidity(SGP30::absoluteHumidity(s.temperatureC, s.humidityPct));
    s.sgpOk = sgp.measureIAQ(s.eco2, s.tvoc) && sgp.measureRaw(s.h2Raw, s.ethanolRaw);
    sgpFailures = s.sgpOk ? 0 : sgpFailures + 1;
    if (sgpFailures >= MAX_CONSECUTIVE_FAILURES) sgpReady = false;
  }
  return s;
}

void sendSensorData(const GasSample &s) {
  Bridge.notify("sensor_data", uptimeSeconds(), s.ahtOk, s.sgpOk, s.temperatureC, s.humidityPct,
                (int)s.tvoc, (int)s.eco2, (int)s.h2Raw, (int)s.ethanolRaw);
}

void sendBaselineIfDue() {
  if (sgpReady && millis() - lastBaselineMs >= BASELINE_PERIOD_MS) {
    lastBaselineMs = millis();
    uint16_t eco2Base, tvocBase;
    if (sgp.getBaseline(eco2Base, tvocBase)) {
      Bridge.notify("sgp30_baseline", uptimeSeconds(), (int)eco2Base, (int)tvocBase);
    }
  }
}

// --- Called by Python ------------------------------------------------------------------------
bool sgp30_set_baseline(int eco2Base, int tvocBase) {
  if (!sgpReady) return false;
  bool ok = sgp.setBaseline((uint16_t)eco2Base, (uint16_t)tvocBase);
  Monitor.print("SGP30 baseline ripristinata: ");
  Monitor.println(ok ? "ok" : "ERRORE");
  return ok;
}
