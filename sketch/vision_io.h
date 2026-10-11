#pragma once

// Vision hardware on the MCU side (see depth_fill.py): LED ring used as flash and
// Modulino Distance (ToF) that gives the true distance at one point of the photo.

// Initialises Modulino, ToF sensor and LED ring (ring off).
void visionBegin();

// Brightness of the LED ring (0-255).
void set_flash(int level);

// ToF distance in mm (median of 5 readings), -1 if there is no valid reading.
int read_tof();

// Python reports the outcome of every photo: useful to check the camera from the serial Monitor.
void camera_status(bool ok);
