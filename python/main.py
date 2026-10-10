from arduino.app_utils import *
import os, sys
import base64
import http.client
import ssl
import time

"""ESC-HackTurin: app Linux della UNO Q.

- Gas: riceve dal microcontrollore le letture di AHT20 + SGP30, decide (allarme gas relativo al
  valore a riposo), registra gli eventi per il "centro di controllo" simulato dell'azienda
  rifiuti e alimenta la dashboard web (brick WebUI, porta 7000).
- Visione: avvia depth_fill.py (livello di riempimento con camera + ToF) nella modalita' scelta.
"""


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

MODES = {"snapshot": depth_fill.snapshot,
         "calibrate": depth_fill.calibrate,
         "run": depth_fill.run,
         "predict": depth_fill.predict}

def pick_mode():
    if len(sys.argv) > 1:                       # python main.py calibrate
        return sys.argv[1]
    if os.environ.get("MODE"):                  # MODE=calibrate python main.py
        return os.environ["MODE"]
    if os.path.exists("mode.txt"):              # mode.txt containing "calibrate"
        return open("mode.txt").read().strip()
    return "run"                                # normal operation

mode = pick_mode().lower()
if mode not in MODES:
    sys.exit(f"Unknown mode '{mode}'. Use one of: {', '.join(MODES)}")
print(f"Starting in mode: {mode}")
MODES[mode]()

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

from baseline_store import BaselineStore
from gas_alarm import GasAlarm

# --- Configurazione -------------------------------------------------------------------
BIN_ID = "TO-0001"
BIN_ADDRESS = "Via Vincenzo Vela 17, Torino"

# App Lab starts main.py without arguments, so choose the vision mode here:
#   "snapshot"  -> save calib_frame.jpg to draw the masks on
#   "calibrate" -> with the bin EMPTY, build the empty-bin depth map
#   "run"       -> normal operation: measure every 15 minutes and log
#   "predict"   -> estimate time until the bin is full
#   None        -> vision disabled (gas sensors only)
VISION_MODE = "snapshot"


{"snapshot": depth_fill.snapshot,
 "calibrate": depth_fill.calibrate,
 "run": depth_fill.run,
 "predict": depth_fill.predict}[MODE]()


PHONE_IP = "192.168.23.177"
PORT = 4444
USE_HTTPS = True
USER, PASSWORD = "", ""     # solo se hai impostato login/password nell'app
INTERVAL = 5                # secondi tra un fotogramma e l'altro

SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_frame.jpg")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64)",
    "Accept": "image/jpeg,image/*,*/*",
}
if USER:
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    HEADERS["Authorization"] = "Basic " + token

# Il telefono usa un certificato autofirmato: niente verifica (va bene in rete locale)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def request(path):
    if USE_HTTPS:
        conn = http.client.HTTPSConnection(PHONE_IP, PORT, timeout=5, context=SSL_CTX)
    else:
        conn = http.client.HTTPConnection(PHONE_IP, PORT, timeout=5)
    conn.request("GET", path, headers=HEADERS)
    r = conn.getresponse()
    if r.status != 200:
        conn.close()
        raise ValueError(f"HTTP {r.status} {r.reason}")
    return conn, r


def snapshot():
    conn, r = request("/video/snapshot?camera=back")
    try:
        return r.read()
    finally:
        conn.close()


def frame_from_stream():
    """Estrae il primo fotogramma JPEG dallo stream MJPEG /video."""
    conn, r = request("/video/mjpeg")
    try:
        buf = b""
        while len(buf) < 3_000_000:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if end >= 0:
                return buf[start:end + 2]
        raise ValueError("nessun fotogramma trovato nello stream")
    finally:
        conn.close()


METHODS = [("/video/snapshot", snapshot), ("/video/mjpeg", frame_from_stream)]
preferred = 0   # il metodo che ha funzionato l'ultima volta viene provato per primo


def grab():
    global preferred
    errors = []
    order = [preferred] + [i for i in range(len(METHODS)) if i != preferred]
    for i in order:
        name, fn = METHODS[i]
        try:
            t0 = time.time()
            data = fn()
            preferred = i
            return name, data, (time.time() - t0) * 1000
        except Exception as e:
            errors.append(f"{name} -> {type(e).__name__}: {e}")
    raise ConnectionError(" | ".join(errors))


def jpeg_size(data):
    """Legge larghezza e altezza dall'intestazione JPEG (marker SOF)."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + length
    return None


def save(data):
    try:
        with open(SAVE_PATH, "wb") as f:
            f.write(data)
    except OSError as e:
        print("  (fotogramma non salvato:", e, ")")

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


# --- Visione (depth_fill.py) ----------------------------------------------------------------
def start_vision():
    """Avvia la modalita' scelta in un thread separato, cosi' i sensori gas continuano a girare."""
    if VISION_MODE is None:
        return
    try:
        import depth_fill
    except Exception as exc:  # es. onnxruntime/opencv non installati
        print(f"[warn] depth_fill non disponibile ({exc}): visione disattivata, i sensori gas restano attivi")
        return
    mode = {"snapshot": depth_fill.snapshot,
            "calibrate": depth_fill.calibrate,
            "run": depth_fill.run,
            "predict": depth_fill.predict}[VISION_MODE]
    threading.Thread(target=mode, name=f"vision-{VISION_MODE}", daemon=True).start()


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

    # camera
    ok = False
    try:
        method, data, ms = grab()
        if data[:2] != b"\xff\xd8":
            raise ValueError("la risposta non è un JPEG")
        size = jpeg_size(data)
        res = f"{size[0]}x{size[1]}" if size else "?x?"
        print(f"OK [{method}]  {res}  {len(data) // 1024} kB  {ms:.0f} ms")
        ok = True
        save(data)
    except Exception as e:
        print("ERRORE:", e)
    try:
        Bridge.call("camera_status", ok)
    except Exception as e:
        print("Bridge non pronto:", e)
    time.sleep(INTERVAL)

start_vision()
scheme = "https" if USE_HTTPS else "http"
print(f"Test camera su {scheme}://{PHONE_IP}:{PORT}")
App.run(user_loop=loop)