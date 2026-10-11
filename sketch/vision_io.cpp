#include "vision_io.h"

#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>
#include <Adafruit_NeoPixel.h>

#define RING_PIN 6        // Grove D6 on the Base Shield
#define RING_LEDS 16

Adafruit_NeoPixel ring(RING_LEDS, RING_PIN, NEO_GRB + NEO_KHZ800);
ModulinoDistance distance;

void visionBegin() {
  Modulino.begin();
  distance.begin();
  ring.begin();
  ring.setBrightness(255);
  set_flash(0);
}

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

void camera_status(bool ok) {
  Monitor.println(ok ? "Camera: foto ricevuta" : "Camera: ERRORE, nessuna foto dal telefono");
}
