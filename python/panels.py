"""The two views of the bin state: the local dashboard (WebUI, port 7000) and the cloud control
panel (cloud_sync.py -> API on Vercel -> Supabase)."""

import state
import vision
from cloud_sync import CloudSync
from common import now_str
from config import BIN_ADDRESS, BIN_ID, DATA_DIR
from gas_sensors import gas_alarm_status, new_odor_reference


# --- Local dashboard -------------------------------------------------------------------------
def on_connect(sid):
    with state.lock:
        snapshot = {
            "bin": {"id": BIN_ID, "address": BIN_ADDRESS},
            "history": list(state.history),
            "events": list(state.events),
            "alarm": gas_alarm_status(state.last_sample["tvoc_ppb"] if state.last_sample else None),
        }
    with vision.fill_lock:
        snapshot["fill"] = vision.fill_status()
    with state.odor_lock:
        snapshot["odor"] = state.odor.status()
    state.ui.send_message("snapshot", snapshot, room=sid)


def register_dashboard():
    state.ui.on_connect(on_connect)
    # Dashboard button: "bin just emptied" (useful when the vision is not active)
    state.ui.on_message("mark_emptied", lambda sid, data: new_odor_reference("manuale"))


# --- Cloud control panel (Vercel + Supabase) -------------------------------------------------
def cloud_status() -> dict:
    """Current state for the control panel: the same one the local dashboard shows."""
    with state.lock:
        sample = state.last_sample
        alarm_status = gas_alarm_status(sample["tvoc_ppb"] if sample else None)
    with vision.fill_lock:
        fill = vision.fill_status(include_chart=False)   # the cloud rebuilds the 30-day series itself
    with state.odor_lock:
        odor_status = state.odor.status()
    return {"sample": sample, "alarm": alarm_status, "odor": odor_status, "fill": fill,
            "vision_mode": vision.vision_mode,
            "sensors": {"aht20": not state.faulty["aht20"], "sgp30": not state.faulty["sgp30"]}}


def on_cloud_command(command: dict):
    """Commands from the control panel, received with the answer to the last upload."""
    if command.get("command") == "mark_emptied":
        print(f"[{now_str()}] Cloud: il pannello segnala il cassonetto appena svuotato")
        new_odor_reference("manuale")


def start_cloud():
    """Starts the upload to the cloud, if configured (data/cloud.json or environment variables)."""
    state.cloud = CloudSync(DATA_DIR, BIN_ADDRESS, cloud_status, on_cloud_command)
    with vision.fill_lock:
        queued = state.cloud.backfill_fill(vision.fill_history.rows)
    if queued:
        print(f"[{now_str()}] Cloud: {queued} misure di riempimento dal CSV messe in coda")
    state.cloud.start()
    if not state.cloud.enabled:
        state.cloud = None
