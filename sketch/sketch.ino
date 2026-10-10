// MCU side of the UNO Q: drives the LED ring (flash) and reads the Modulino Distance,
// exposing both to the Python script through the App Lab Bridge.
// Library names/APIs to verify in your App Lab / Arduino IDE version:
//  - Arduino_RouterBridge (Bridge)
//  - Modulino
//  - Adafruit_NeoPixel works with WS2813; if it doesn't compile on the UNO Q core,
//    look in the Library Manager for a WS28xx library that supports it.

#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>
#include <Adafruit_NeoPixel.h>

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

void setup() {
  Bridge.begin();
  Modulino.begin();        // on UNO Q the Qwiic port may need Modulino.begin(Wire1)
  distance.begin();
  ring.begin();
  ring.setBrightness(255);
  set_flash(0);
  Bridge.provide("set_flash", set_flash);
  Bridge.provide("read_tof", read_tof);
}


void loop() {
  // put your main code here, to run repeatedly:

}
