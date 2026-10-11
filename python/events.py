"""Events for the (simulated) control centre of the waste collection company."""

import time

import state
from common import now_str
from config import BIN_ADDRESS, BIN_ID

event_counter = 0


def push_event(kind: str, severity: str, title: str, message: str, values: dict | None = None) -> dict:
    """Creates an event for the control centre and sends it to the dashboard (and the cloud)."""
    global event_counter
    with state.lock:
        event_counter += 1
        event = {
            "id": event_counter,
            "ts": time.time(),
            "time": now_str(),
            "bin_id": BIN_ID,
            "address": BIN_ADDRESS,
            "type": kind,
            "severity": severity,  # info | warning | critical
            "title": title,
            "message": message,
            "values": values or {},
        }
        state.events.append(event)
    print(f"[{event['time']}] EVENTO {severity.upper()} {kind}: {title} - {message}")
    state.ui.send_message("event", event)
    if state.cloud:
        state.cloud.add_event(event)
    return event
