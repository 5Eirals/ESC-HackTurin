"""TO-Breath configuration: bin identity, vision mode, phone camera, thresholds and demo flags.

Everything that is meant to be edited by hand lives here; the other modules import it.
"""

import os
from pathlib import Path

# --- Bin -------------------------------------------------------------------------------------
BIN_ID = "TO-0001"
BIN_ADDRESS = "Via Vincenzo Vela 17, Torino"

# --- Vision ----------------------------------------------------------------------------------
# App Lab starts main.py without arguments, so the vision mode is chosen here
# (or with an argument, the MODE environment variable or a mode.txt file next to main.py):
#   "snapshot"  -> saves calib_frame.jpg (only needed to draw the masks by hand, with
#                  depth_fill.CFG["auto_calibration"] = False)
#   "calibrate" -> with the bin EMPTY: the AI finds the floor and the reference band, the ToF
#                  gives the distance to the floor; creates masks, distances and the reference map
#                  (check python/calib_overlay.jpg), then switches to "run" by itself
#   "run"       -> normal operation: photo + depth analysis every CAPTURE_INTERVAL_S
#   "off"/None  -> vision disabled (gas sensors only)
VISION_MODE = "run"         # "calibrate" only with the bin empty, to redo the calibration
VISION_MODES = ("snapshot", "calibrate", "run")

CAPTURE_INTERVAL_S = 20      # testing: one photo every 20 s (300 = 5 minutes in normal use)
FLASH_SETTLE_S = 1.5         # wait for the phone's exposure to adapt to the torch
SNAPSHOT_TIMEOUT_S = 20      # longest wait for one photo (the app can take up to 14 s)

# --- Phone used as camera (IP Webcam app) ----------------------------------------------------
PHONE_IP = "192.168.22.74"
PORT = 4444
USE_HTTPS = True
USER, PASSWORD = "ESCam67", "ESCamotage420"   # IP camera app login
# Lens: "auto" = the widest back camera (the walls must be in view for the calibration),
# or an id from /info.json such as "0" or "0:2" (the list is printed at the first photo)
CAMERA = "auto"

SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_frame.jpg")

# --- Fill level ------------------------------------------------------------------------------
FILL_WARN_PCT = 80           # "almost full" event
FILL_FULL_PCT = 95           # "full" event
# With no measurement yet, generate a month of INVENTED data (simulate_fill.py) to show the
# forecast and the chart. Set to False on the real bin.
SIMULATED_HISTORY = True

# --- Gas and odour ---------------------------------------------------------------------------
# Perceived odour, demo mode: 60 s windows instead of 30 min (same thresholds, 2x / 5x) to show
# it live in a few minutes. Set to False on the real bin.
ODOR_DEMO_MODE = True
# The fast gas alarm (gas_alarm.py) is meant for the sensor INSIDE the bin (decay, start of a
# fire): in the demo, with the sensor outside and alcohol as the source, it would give
# misleading alarms, so it stays off.
GAS_ALARM_ENABLED = not ODOR_DEMO_MODE

HISTORY_SIZE = 600           # samples kept for the charts (1/s -> 10 minutes)
MAX_EVENTS = 100              # events kept in the control centre
SGP30_WARMUP_S = 15           # after start-up the SGP30 returns fixed values for ~15 s
BASELINE_MIN_RUNTIME_S = 600  # save the baseline only after 10 min of continuous operation
SENSOR_FAULT_AFTER = 3        # consecutive failed readings before reporting a fault

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
