"""TO-Breath: Linux app of the UNO Q (entry point, started by App Lab).

- Gas: receives the AHT20 + SGP30 readings from the microcontroller, decides (gas alarm relative to
  the resting value), records the events for the simulated "control centre" of the waste company
  and feeds the web dashboard (WebUI brick, port 7000).
- Vision: in the loop it takes a photo with the phone (IP camera) and the LED ring flash, reads
  the ToF and estimates the depth with Depth Anything V2 (depth_fill.py) to get the fill level.
  Every measurement goes to data/fill_log.csv (fill_history.py), from which the time to full is
  forecast; level and forecast go to the dashboard.
- Cloud: when configured (data/cloud.json), cloud_sync.py sends readings, measurements, events and
  state to the API on Vercel, which stores them on Supabase for the control panel. The local files
  remain the board's dataset and backup copy.

Modules:
  config.py        settings to edit by hand (bin, vision mode, camera, thresholds, demo flags)
  state.py         runtime state shared by the modules (WebUI, readings, events, odour, cloud)
  common.py        small helpers (time stamp for the logs, safe call to the sketch)
  gas_sensors.py   Bridge callbacks, sensor health, gas alarm, odour events, SGP30 baseline
  events.py        events for the control centre
  phone_camera.py  phone used as camera (requests, lens, photo, torch)
  vision.py        photo + ToF, depth analysis, fill history, forecast and fill events
  panels.py        local dashboard and cloud control panel
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))  # model, masks and calibration live here

from arduino.app_utils import App

import gas_sensors
import panels
import state
import vision
from common import now_str
from config import CAPTURE_INTERVAL_S, GAS_ALARM_ENABLED, PHONE_IP, PORT, USE_HTTPS


def loop():
    """Main loop. Calls to the sketch are made here, never inside the Bridge callbacks."""
    gas_sensors.restore_baseline_if_needed()
    vision.report_startup_fill()
    vision.capture_if_due()
    time.sleep(1)


def print_startup_summary():
    print(f"[{now_str()}] Riempimento: {vision.fill_history.last['fill_pct'] if vision.fill_history.last else '—'}%, "
          f"{vision.describe_eta(vision.fill_history.predict())}")

    scheme = "https" if USE_HTTPS else "http"
    if vision.vision_mode is None:
        print("Visione disattivata (solo sensori gas)")
    else:
        print(f"Visione: modalita' {vision.vision_mode}, camera {scheme}://{PHONE_IP}:{PORT}, "
              f"uno scatto ogni {CAPTURE_INTERVAL_S} s")
    print(f"Odore: profilo '{state.odor.profile}', mediana su {state.odor.status()['window_label']}, soglie "
          f"{state.odor.p['yellow_ratio']}x / {state.odor.p['red_ratio']}x; allarme gas rapido "
          f"{'attivo' if GAS_ALARM_ENABLED else 'disattivato (modalita demo)'}")


def main():
    gas_sensors.register()        # messages from the sketch
    panels.register_dashboard()   # local dashboard
    vision.init()                 # vision mode, fill history (simulated if enabled)
    panels.start_cloud()          # upload to the cloud control panel, if configured
    print_startup_summary()
    App.run(user_loop=loop)


main()
