"""Runtime state shared by several modules.

Modules use these as attributes (state.cloud, state.last_sample, ...), never with
`from state import ...`, so they always see the current value.
"""

import threading
from collections import deque

from arduino.app_bricks.web_ui import WebUI

from config import DATA_DIR, HISTORY_SIZE, MAX_EVENTS, ODOR_DEMO_MODE
from odor_index import OdorMonitor

ui = WebUI()

# Perceived odour near the bin (SGP30 outside the bin): median of the raw signals compared with
# the air measured after the last emptying. See odor_index.py.
odor = OdorMonitor(DATA_DIR / "odor_reference.json", "demo" if ODOR_DEMO_MODE else "normale")
odor_lock = threading.Lock()   # samples from the Bridge, new reference from the loop or the dashboard

lock = threading.Lock()        # protects history, events, last_sample and the event counter
history = deque(maxlen=HISTORY_SIZE)
events = deque(maxlen=MAX_EVENTS)
last_sample = None

faulty = {"aht20": False, "sgp30": False}
cloud = None                   # CloudSync, set by panels.start_cloud() when it is configured
