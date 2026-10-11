"""Gas side of the app: AHT20 + SGP30 readings from the MCU, sensor health, fast gas alarm,
perceived odour and the SGP30 baseline (saved to file and restored at start-up)."""

import time

from arduino.app_utils import Bridge

import state
from baseline_store import BaselineStore
from common import now_str
from config import (BASELINE_MIN_RUNTIME_S, DATA_DIR, GAS_ALARM_ENABLED, SENSOR_FAULT_AFTER,
                    SGP30_WARMUP_S)
from events import push_event
from gas_alarm import GasAlarm

baseline_store = BaselineStore(DATA_DIR / "sgp30_baseline.json")
alarm = GasAlarm()

sgp_started_uptime = None   # sketch uptime when the SGP30 algorithm restarted
need_restore = True         # when Python starts, try to restore the baseline right away
baseline_restored = False

failures = {"aht20": 0, "sgp30": 0}


def format_ratio(ratio) -> str:
    return "—" if ratio is None else f"{ratio:.1f}x" if ratio < 10 else f"{ratio:.0f}x"


def gas_alarm_status(tvoc_ppb) -> dict:
    """State of the fast gas alarm for dashboard and cloud ("disabled" in demo mode)."""
    status = alarm.status(tvoc_ppb)
    if not GAS_ALARM_ENABLED:
        status["state"] = "disabled"
    return status


def track_sensor_health(name: str, ok: bool):
    """Reports a fault after N consecutive failed readings, and the return to normal."""
    if ok:
        failures[name] = 0
        if state.faulty[name]:
            state.faulty[name] = False
            push_event("SENSOR_OK", "info", f"Sensore {name.upper()} di nuovo operativo",
                       "Le letture sono tornate regolari.")
        return
    failures[name] += 1
    if failures[name] == SENSOR_FAULT_AFTER and not state.faulty[name]:
        state.faulty[name] = True
        push_event("SENSOR_FAULT", "warning", f"Sensore {name.upper()} non risponde",
                   f"{SENSOR_FAULT_AFTER} letture fallite consecutive: serve un controllo tecnico.")


# --- Perceived odour ---------------------------------------------------------------------
ODOR_EVENTS = {
    "yellow": ("ODOR_NOTICEABLE", "warning", "Odore percepibile vicino al cassonetto"),
    "red": ("ODOR_STRONG", "critical", "Puzza vicino al cassonetto: serve un intervento"),
    "green": ("ODOR_CLEAR", "info", "Odore rientrato"),
}
REFERENCE_SOURCES = {"avvio": "all'avvio del sistema", "svuotamento": "dopo lo svuotamento",
                     "manuale": "dopo lo svuotamento segnalato dalla dashboard"}


def report_odor_event(event):
    """Turns the OdorMonitor events into events for the control centre."""
    if event[0] == "reference":
        s = event[1]
        push_event("ODOR_REFERENCE", "info", "Riferimento dell'aria aggiornato",
                   f"Aria di riferimento misurata {REFERENCE_SOURCES.get(s['reference']['source'], '')}: "
                   "da qui l'odore si misura rispetto a questi valori.")
        return
    _, old, new, s = event
    kind, severity, title = ODOR_EVENTS[new]
    values = {k: s[k] for k in ("ratio", "persistence", "delta_h2", "delta_ethanol", "temperature_c")}
    if new == "green":
        message = f"Aria {s['ratio']}x il riferimento (mediana {s['window_label']})."
    else:
        temp = f", {s['temperature_c']} °C" if s["temperature_c"] is not None else ""
        message = (f"Aria {s['ratio']}x piu' carica del riferimento (mediana {s['window_label']}), "
                   f"sopra soglia per il {round(s['persistence'] * 100)}% del tempo{temp}.")
    push_event(kind, severity, title, message, values)


def new_odor_reference(source: str):
    """The bin has just been emptied: the reference air is measured again."""
    with state.odor_lock:
        state.odor.start_reference(source)
        status = state.odor.status()
    print(f"[{now_str()}] Odore: raccolta di un nuovo riferimento ({REFERENCE_SOURCES.get(source, source)})")
    state.ui.send_message("odor", status)


# --- Callbacks from the microcontroller ----------------------------------------------------
def on_sensor_data(uptime_s, aht_ok, sgp_ok, temp_c, rh_pct, tvoc_ppb, eco2_ppm, h2_raw, ethanol_raw):
    warming = sgp_ok and sgp_started_uptime is not None and uptime_s - sgp_started_uptime < SGP30_WARMUP_S

    sample = {
        "ts": time.time(),
        "uptime_s": uptime_s,
        "temperature_c": round(temp_c, 2) if aht_ok else None,
        "humidity_pct": round(rh_pct, 1) if aht_ok else None,
        "tvoc_ppb": tvoc_ppb if sgp_ok else None,
        "eco2_ppm": eco2_ppm if sgp_ok else None,
        "h2_raw": h2_raw if sgp_ok else None,
        "ethanol_raw": ethanol_raw if sgp_ok else None,
        "h2_ratio": None,        # times the reference air (higher = more gas), see odor_index
        "ethanol_ratio": None,
        "aht_ok": bool(aht_ok),
        "sgp_ok": bool(sgp_ok),
        "sgp_warming_up": bool(warming),
    }

    track_sensor_health("aht20", bool(aht_ok))
    track_sensor_health("sgp30", bool(sgp_ok))

    result = None
    odor_events, odor_status = [], None
    if sgp_ok and not warming:
        if GAS_ALARM_ENABLED:
            result = alarm.update(tvoc_ppb, sample["ts"])
        with state.odor_lock:
            odor_events = state.odor.add_sample(sample["ts"], h2_raw, ethanol_raw, sample["temperature_c"])
            if state.odor.last_eval == sample["ts"]:   # level recomputed (once a minute)
                odor_status = state.odor.last_status
            sample["h2_ratio"], sample["ethanol_ratio"] = state.odor.instant_ratios(h2_raw, ethanol_raw)
    status = gas_alarm_status(sample["tvoc_ppb"])

    with state.lock:
        state.history.append(sample)
        state.last_sample = sample
    if state.cloud:
        state.cloud.add_sample(sample)

    print(f"[{now_str()}] T={sample['temperature_c']}°C UR={sample['humidity_pct']}% "
          f"TVOC={sample['tvoc_ppb']} ppb eCO2={sample['eco2_ppm']} ppm "
          f"H2={format_ratio(sample['h2_ratio'])} EtOH={format_ratio(sample['ethanol_ratio'])} "
          f"(grezzi {sample['h2_raw']}/{sample['ethanol_raw']}) "
          f"allarme={status['state']} odore={state.odor.level or '—'}{' (riscaldamento)' if warming else ''}")

    state.ui.send_message("reading", {"sample": sample, "alarm": status})
    if odor_status is not None:
        state.ui.send_message("odor", odor_status)
    for event in odor_events:
        report_odor_event(event)

    values = {"tvoc_ppb": tvoc_ppb, "eco2_ppm": eco2_ppm,
              "rest_tvoc_ppb": status["rest_tvoc_ppb"], "temperature_c": sample["temperature_c"]}
    if result == "ALERT":
        push_event("GAS_ALERT", "critical", "Livello di gas anomalo nel cassonetto",
                   f"TVOC {tvoc_ppb} ppb, {status['ratio']}x il valore a riposo "
                   f"({status['rest_tvoc_ppb']} ppb): possibile degrado o principio d'incendio.",
                   values)
    elif result == "CLEAR":
        push_event("GAS_CLEAR", "info", "Livello di gas rientrato",
                   f"TVOC {tvoc_ppb} ppb, tornato vicino al valore a riposo.", values)


def on_sgp30_started(uptime_s):
    global sgp_started_uptime, need_restore, baseline_restored
    print(f"[{now_str()}] SGP30 avviato (uptime sketch {uptime_s} s): ripristino baseline e nuovo apprendimento")
    sgp_started_uptime = uptime_s
    need_restore = True
    baseline_restored = False
    alarm.reset()


def on_sgp30_baseline(uptime_s, eco2_base, tvoc_base):
    runtime = uptime_s - (sgp_started_uptime or 0)
    if baseline_restored or runtime >= BASELINE_MIN_RUNTIME_S:
        saved = baseline_store.save(eco2_base, tvoc_base)
        print(f"[{now_str()}] Baseline SGP30 eCO2=0x{eco2_base:04X} TVOC=0x{tvoc_base:04X} "
              f"{'salvata' if saved else 'NON salvata (errore di scrittura)'}")


def register():
    """Registers the callbacks for the messages sent by the sketch."""
    Bridge.provide("sensor_data", on_sensor_data)
    Bridge.provide("sgp30_started", on_sgp30_started)
    Bridge.provide("sgp30_baseline", on_sgp30_baseline)


# --- SGP30 baseline restore (from the main loop) -------------------------------------------
def restore_baseline_if_needed():
    """Sends the saved baseline to the SGP30 after every (re)start of its algorithm."""
    global need_restore, baseline_restored
    if need_restore:
        need_restore = False
        stored = baseline_store.load()
        if stored is None:
            print(f"[{now_str()}] Nessuna baseline SGP30 salvata (o piu' vecchia di 7 giorni): "
                  "il sensore parte da zero")
        else:
            try:
                ok = Bridge.call("sgp30_set_baseline", stored[0], stored[1], timeout=5)
            except Exception as exc:  # sketch not ready or sensor missing: retried at the next start
                print(f"[{now_str()}] Ripristino baseline fallito: {exc}")
                ok = False
            baseline_restored = bool(ok)
