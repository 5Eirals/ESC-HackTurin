"""ESC-HackTurin: app Linux della UNO Q.

Riceve dal microcontrollore le letture di AHT20 + SGP30, decide (allarme gas relativo al
valore a riposo), registra gli eventi per il "centro di controllo" simulato dell'azienda
rifiuti e alimenta la dashboard web (brick WebUI, porta 7000).
"""

import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

from baseline_store import BaselineStore
from gas_alarm import GasAlarm

# --- Configurazione -------------------------------------------------------------------
BIN_ID = "TO-0001"
BIN_ADDRESS = "Via Vincenzo Vela 17, Torino"

HISTORY_SIZE = 600            # campioni tenuti per i grafici (1/s -> 10 minuti)
MAX_EVENTS = 100              # eventi tenuti nel centro di controllo
SGP30_WARMUP_S = 15           # dopo l'avvio l'SGP30 restituisce valori fissi per ~15 s
BASELINE_MIN_RUNTIME_S = 600  # salva la baseline solo dopo 10 min di funzionamento continuo
SENSOR_FAULT_AFTER = 3        # letture fallite consecutive prima di segnalare un guasto

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# --- Stato ------------------------------------------------------------------------------
ui = WebUI()
baseline_store = BaselineStore(DATA_DIR / "sgp30_baseline.json")
alarm = GasAlarm()

lock = threading.Lock()
history = deque(maxlen=HISTORY_SIZE)
events = deque(maxlen=MAX_EVENTS)
event_counter = 0
last_sample = None

sgp_started_uptime = None   # uptime dello sketch quando l'algoritmo SGP30 e' ripartito
need_restore = True         # all'avvio di Python proviamo subito a ripristinare la baseline
baseline_restored = False

failures = {"aht20": 0, "sgp30": 0}
faulty = {"aht20": False, "sgp30": False}


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def push_event(kind: str, severity: str, title: str, message: str, values: dict | None = None) -> dict:
    """Crea un evento per il centro di controllo e lo invia alla dashboard."""
    global event_counter
    with lock:
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
        events.append(event)
    print(f"[{event['time']}] EVENTO {severity.upper()} {kind}: {title} - {message}")
    ui.send_message("event", event)
    return event


def track_sensor_health(name: str, ok: bool):
    """Segnala un guasto dopo N letture fallite consecutive e il ritorno alla normalita'."""
    if ok:
        failures[name] = 0
        if faulty[name]:
            faulty[name] = False
            push_event("SENSOR_OK", "info", f"Sensore {name.upper()} di nuovo operativo",
                       "Le letture sono tornate regolari.")
        return
    failures[name] += 1
    if failures[name] == SENSOR_FAULT_AFTER and not faulty[name]:
        faulty[name] = True
        push_event("SENSOR_FAULT", "warning", f"Sensore {name.upper()} non risponde",
                   f"{SENSOR_FAULT_AFTER} letture fallite consecutive: serve un controllo tecnico.")


# --- Callback dal microcontrollore -------------------------------------------------------
def on_sensor_data(uptime_s, aht_ok, sgp_ok, temp_c, rh_pct, tvoc_ppb, eco2_ppm, h2_raw, ethanol_raw):
    global last_sample
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
        "aht_ok": bool(aht_ok),
        "sgp_ok": bool(sgp_ok),
        "sgp_warming_up": bool(warming),
    }

    track_sensor_health("aht20", bool(aht_ok))
    track_sensor_health("sgp30", bool(sgp_ok))

    result = None
    if sgp_ok and not warming:
        result = alarm.update(tvoc_ppb, sample["ts"])
    status = alarm.status(sample["tvoc_ppb"])

    with lock:
        history.append(sample)
        last_sample = sample

    print(f"[{now_str()}] T={sample['temperature_c']}°C UR={sample['humidity_pct']}% "
          f"TVOC={sample['tvoc_ppb']} ppb eCO2={sample['eco2_ppm']} ppm "
          f"H2={sample['h2_raw']} EtOH={sample['ethanol_raw']} "
          f"allarme={status['state']}{' (riscaldamento)' if warming else ''}")

    ui.send_message("reading", {"sample": sample, "alarm": status})

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


Bridge.provide("sensor_data", on_sensor_data)
Bridge.provide("sgp30_started", on_sgp30_started)
Bridge.provide("sgp30_baseline", on_sgp30_baseline)


# --- Dashboard ------------------------------------------------------------------------------
def on_connect(sid):
    with lock:
        snapshot = {
            "bin": {"id": BIN_ID, "address": BIN_ADDRESS},
            "history": list(history),
            "events": list(events),
            "alarm": alarm.status(last_sample["tvoc_ppb"] if last_sample else None),
        }
    ui.send_message("snapshot", snapshot, room=sid)


ui.on_connect(on_connect)


# --- Loop principale ------------------------------------------------------------------------
def loop():
    """Le chiamate verso lo sketch si fanno qui, mai dentro le callback del Bridge."""
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
            except Exception as exc:  # sketch non pronto o sensore assente: si riprova al prossimo avvio
                print(f"[{now_str()}] Ripristino baseline fallito: {exc}")
                ok = False
            baseline_restored = bool(ok)
    time.sleep(1)


App.run(user_loop=loop)
