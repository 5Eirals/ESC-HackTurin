"""TO-Breath: app Linux della UNO Q.

- Gas: riceve dal microcontrollore le letture di AHT20 + SGP30, decide (allarme gas relativo al
  valore a riposo), registra gli eventi per il "centro di controllo" simulato dell'azienda
  rifiuti e alimenta la dashboard web (brick WebUI, porta 7000).
- Visione: nel loop scatta una foto con il telefono (IP camera) e con il flash dell'anello LED,
  legge il ToF e stima la profondita' con Depth Anything V2 (depth_fill.py) per ricavare il
  livello di riempimento. Ogni misura finisce in data/fill_log.csv (fill_history.py), da cui
  si prevede quando il cassonetto sara' pieno; livello e previsione vanno sulla dashboard.
- Cloud: se configurato (data/cloud.json), cloud_sync.py manda letture, misure, eventi e stato all'API
  su Vercel, che li salva su Supabase per il pannello di controllo. I file locali restano il dataset
  della scheda e la copia di sicurezza.
"""

import base64
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))  # modello, maschere e calibrazione stanno qui

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

from baseline_store import BaselineStore
from cloud_sync import CloudSync
from fill_history import FillHistory
from gas_alarm import GasAlarm
from odor_index import OdorMonitor
import simulate_fill

try:
    import depth_fill
except Exception as exc:  # es. onnxruntime/opencv non installati
    print(f"[warn] depth_fill non disponibile ({exc}): niente analisi di profondita', "
          "le foto vengono solo salvate e i sensori gas restano attivi")
    depth_fill = None

# --- Configurazione -------------------------------------------------------------------
BIN_ID = "TO-0001"
BIN_ADDRESS = "Via Vincenzo Vela 17, Torino"

# App Lab avvia main.py senza argomenti: la modalita' della visione si sceglie qui
# (oppure con un argomento, la variabile d'ambiente MODE o un file mode.txt accanto a main.py):
#   "snapshot"  -> salva calib_frame.jpg (serve solo per disegnare le maschere a mano, con
#                  depth_fill.CFG["auto_calibration"] = False)
#   "calibrate" -> con il cassonetto VUOTO: l'AI trova il fondo e la fascia di riferimento, il
#                  ToF da' la distanza dal fondo; crea maschere, distanze e mappa di riferimento
#                  (controllare python/calib_overlay.jpg), poi passa da sola a "run"
#   "run"       -> funzionamento normale: foto + analisi di profondita' ogni CAPTURE_INTERVAL_S
#   "off"/None  -> visione disattivata (solo sensori gas)
VISION_MODE = "run"         # "calibrate" solo a cassonetto vuoto, per rifare la calibrazione
VISION_MODES = ("snapshot", "calibrate", "run")

CAPTURE_INTERVAL_S = 20      # prove: uno scatto ogni 20 s (in uso normale 300 = 5 minuti)
FLASH_SETTLE_S = 1.5         # attesa perche' l'esposizione del telefono si adatti alla torcia
SNAPSHOT_TIMEOUT_S = 20      # attesa massima per uno scatto (l'app arriva a 14 s)

# Telefono usato come camera (app IP Webcam)
PHONE_IP = "192.168.22.74"
PORT = 4444
USE_HTTPS = True
USER, PASSWORD = "ESCam67", "ESCamotage420"   # login dell'app IP camera
# Obiettivo: "auto" = la camera posteriore piu' grandangolare (serve vedere le pareti per la
# calibrazione), oppure un id di /info.json come "0" o "0:2" (l'elenco e' stampato al primo scatto)
CAMERA = "auto"

SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_frame.jpg")

# Livello di riempimento
FILL_WARN_PCT = 80           # evento "quasi pieno"
FILL_FULL_PCT = 95           # evento "pieno"
# Se non c'e' ancora nessuna misura, genera un mese di dati INVENTATI (simulate_fill.py) per
# mostrare previsione e grafico. Mettere False sul cassonetto vero.
SIMULATED_HISTORY = True

# Odore percepito: modalita' demo = finestre di 60 s invece di 30 min (soglie uguali, 2x / 5x) per
# mostrarlo dal vivo in pochi minuti. Mettere False sul cassonetto vero.
ODOR_DEMO_MODE = True
# L'allarme gas rapido (gas_alarm.py) e' pensato per il sensore DENTRO il cassonetto (degrado,
# principio d'incendio): in demo, con il sensore fuori e l'alcol come sorgente, darebbe allarmi
# fuorvianti, quindi resta spento.
GAS_ALARM_ENABLED = not ODOR_DEMO_MODE

HISTORY_SIZE = 600           # campioni tenuti per i grafici (1/s -> 10 minuti)
MAX_EVENTS = 100              # eventi tenuti nel centro di controllo
SGP30_WARMUP_S = 15           # dopo l'avvio l'SGP30 restituisce valori fissi per ~15 s
BASELINE_MIN_RUNTIME_S = 600  # salva la baseline solo dopo 10 min di funzionamento continuo
SENSOR_FAULT_AFTER = 3        # letture fallite consecutive prima di segnalare un guasto

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# --- Camera (telefono) -------------------------------------------------------------------
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


def request(path, timeout=5):
    if USE_HTTPS:
        conn = http.client.HTTPSConnection(PHONE_IP, PORT, timeout=timeout, context=SSL_CTX)
    else:
        conn = http.client.HTTPConnection(PHONE_IP, PORT, timeout=timeout)
    conn.request("GET", path, headers=HEADERS)
    r = conn.getresponse()
    if r.status != 200:
        conn.close()
        raise ValueError(f"HTTP {r.status} {r.reason}")
    return conn, r


camera_id = None   # obiettivo scelto al primo scatto (pick_camera)


def pick_camera():
    """Camera da usare: CAMERA se fissata, altrimenti la posteriore piu' grandangolare di
    /info.json: focale piu' corta nel nome ("Back Lens 2 (1.4mm)"), poi zoom minimo piu' basso
    (molti telefoni riportano 1.0x per tutte le camere)."""
    if CAMERA != "auto":
        return CAMERA
    try:
        conn, r = request("/info.json")
        cameras = json.loads(r.read()).get("cameras", [])
        conn.close()
    except Exception as e:
        print(f"[{now_str()}] Telefono: elenco camere non disponibile ({e}), uso 'back'")
        return "back"

    def focal_mm(c):
        found = re.findall(r"([0-9]+(?:\.[0-9]+)?)mm", str(c.get("label", "")))
        return min(float(f) for f in found) if found else float("inf")

    def min_zoom(c):
        try:
            return float((c.get("zoom") or {}).get("min"))
        except (TypeError, ValueError):
            return 1.0

    for c in cameras:
        print(f"[{now_str()}] Telefono: camera {c.get('id')} {c.get('facing')} '{c.get('label')}' "
              f"zoom min {(c.get('zoom') or {}).get('min')}")
    back = [c for c in cameras if c.get("facing") == "back"]
    if not back:
        return "back"
    best = min(back, key=lambda c: (focal_mm(c), min_zoom(c)))
    print(f"[{now_str()}] Telefono: uso la camera {best.get('id')} ({best.get('label')})")
    return str(best.get("id"))


def phone_snapshot():
    # L'app puo' metterci fino a 14 s (riapre la camera, riprova): 5 s non bastano
    conn, r = request(f"/video/snapshot?camera={camera_id or 'back'}", timeout=SNAPSHOT_TIMEOUT_S)
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


METHODS = [("/video/snapshot", phone_snapshot), ("/video/mjpeg", frame_from_stream)]
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


# --- Stato ------------------------------------------------------------------------------
ui = WebUI()
baseline_store = BaselineStore(DATA_DIR / "sgp30_baseline.json")
alarm = GasAlarm()
# Odore percepito vicino al cassonetto (SGP30 fuori dal cassonetto): mediana dei segnali grezzi
# rispetto all'aria misurata dopo l'ultimo svuotamento. Vedi odor_index.py.
odor = OdorMonitor(DATA_DIR / "odor_reference.json", "demo" if ODOR_DEMO_MODE else "normale")
odor_lock = threading.Lock()   # campioni dal Bridge, nuovo riferimento dal loop o dalla dashboard

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
cloud = None                # CloudSync, creato piu' sotto quando lo stato da inviare e' pronto


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def format_ratio(ratio) -> str:
    return "—" if ratio is None else f"{ratio:.1f}x" if ratio < 10 else f"{ratio:.0f}x"


def gas_alarm_status(tvoc_ppb) -> dict:
    """Stato dell'allarme gas rapido per dashboard e cloud ("disabled" in modalita' demo)."""
    status = alarm.status(tvoc_ppb)
    if not GAS_ALARM_ENABLED:
        status["state"] = "disabled"
    return status


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
    if cloud:
        cloud.add_event(event)
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


ODOR_EVENTS = {
    "yellow": ("ODOR_NOTICEABLE", "warning", "Odore percepibile vicino al cassonetto"),
    "red": ("ODOR_STRONG", "critical", "Puzza vicino al cassonetto: serve un intervento"),
    "green": ("ODOR_CLEAR", "info", "Odore rientrato"),
}
REFERENCE_SOURCES = {"avvio": "all'avvio del sistema", "svuotamento": "dopo lo svuotamento",
                     "manuale": "dopo lo svuotamento segnalato dalla dashboard"}


def report_odor_event(event):
    """Traduce gli eventi di OdorMonitor in eventi per il centro di controllo."""
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
    """Il cassonetto e' appena stato svuotato: si misura di nuovo l'aria di riferimento."""
    with odor_lock:
        odor.start_reference(source)
        status = odor.status()
    print(f"[{now_str()}] Odore: raccolta di un nuovo riferimento ({REFERENCE_SOURCES.get(source, source)})")
    ui.send_message("odor", status)


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
        "h2_ratio": None,        # volte l'aria di riferimento (piu' alto = piu' gas), vedi odor_index
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
        with odor_lock:
            odor_events = odor.add_sample(sample["ts"], h2_raw, ethanol_raw, sample["temperature_c"])
            if odor.last_eval == sample["ts"]:   # livello ricalcolato (una volta al minuto)
                odor_status = odor.last_status
            sample["h2_ratio"], sample["ethanol_ratio"] = odor.instant_ratios(h2_raw, ethanol_raw)
    status = gas_alarm_status(sample["tvoc_ppb"])

    with lock:
        history.append(sample)
        last_sample = sample
    if cloud:
        cloud.add_sample(sample)

    print(f"[{now_str()}] T={sample['temperature_c']}°C UR={sample['humidity_pct']}% "
          f"TVOC={sample['tvoc_ppb']} ppb eCO2={sample['eco2_ppm']} ppm "
          f"H2={format_ratio(sample['h2_ratio'])} EtOH={format_ratio(sample['ethanol_ratio'])} "
          f"(grezzi {sample['h2_raw']}/{sample['ethanol_raw']}) "
          f"allarme={status['state']} odore={odor.level or '—'}{' (riscaldamento)' if warming else ''}")

    ui.send_message("reading", {"sample": sample, "alarm": status})
    if odor_status is not None:
        ui.send_message("odor", odor_status)
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


Bridge.provide("sensor_data", on_sensor_data)
Bridge.provide("sgp30_started", on_sgp30_started)
Bridge.provide("sgp30_baseline", on_sgp30_baseline)


# --- Dashboard ------------------------------------------------------------------------------
def on_connect(sid):
    with lock:
        state = {
            "bin": {"id": BIN_ID, "address": BIN_ADDRESS},
            "history": list(history),
            "events": list(events),
            "alarm": gas_alarm_status(last_sample["tvoc_ppb"] if last_sample else None),
        }
    with fill_lock:
        state["fill"] = fill_status()
    with odor_lock:
        state["odor"] = odor.status()
    ui.send_message("snapshot", state, room=sid)


ui.on_connect(on_connect)
# Pulsante della dashboard: "cassonetto appena svuotato" (utile se la visione non e' attiva)
ui.on_message("mark_emptied", lambda sid, data: new_odor_reference("manuale"))


# --- Visione: foto + analisi di profondita' -------------------------------------------------
def pick_vision_mode():
    mode = sys.argv[1] if len(sys.argv) > 1 else None   # python main.py calibrate
    mode = mode or os.environ.get("MODE")               # MODE=calibrate python main.py
    if not mode and os.path.exists("mode.txt"):         # mode.txt con dentro "calibrate"
        mode = open("mode.txt").read().strip()
    mode = (mode or VISION_MODE or "").lower() or None
    if mode == "off":
        mode = None
    if mode is not None and mode not in VISION_MODES:
        print(f"[warn] modalita' visione '{mode}' sconosciuta (usa {', '.join(VISION_MODES)}): uso 'run'")
        mode = "run"
    return mode


vision_mode = pick_vision_mode()
last_capture_t = None   # time.monotonic() dell'ultimo scatto
last_fill = None        # ultimo risultato di depth_fill.analyze() (diagnostica per la dashboard)

fill_lock = threading.Lock()   # fill_history e' scritto dal loop e letto alla connessione della dashboard
fill_history = FillHistory(DATA_DIR / "fill_log.csv")
if SIMULATED_HISTORY and not fill_history.rows:
    n = simulate_fill.generate(fill_history.path)
    fill_history.load()
    print(f"[{now_str()}] Nessuna misura di riempimento: generati {n} campioni simulati (30 giorni)")
fill_tracker = (depth_fill.FillTracker(fill_history.last["fill_pct"] if fill_history.last else None)
                if depth_fill else None)
fill_alert = None       # ultimo livello segnalato al centro di controllo: None | "warn" | "full"
fill_startup_check = True  # al primo giro del loop segnala lo stato iniziale (es. gia' quasi pieno)


def fill_level_name(pct):
    if pct is None:
        return "unknown"
    if pct >= FILL_FULL_PCT:
        return "full"
    if pct >= FILL_WARN_PCT:
        return "warn"
    return "ok"


def fill_status(include_chart=True) -> dict:
    """Tutto quello che serve alla sezione "Livello di riempimento" della dashboard."""
    last = fill_history.last
    status = {
        "last": None if last is None else {
            "ts": last["ts"], "fill_pct": last["fill_pct"], "peak_pct": last["peak_pct"],
            "source": last["source"], "level": fill_level_name(last["fill_pct"]),
        },
        "prediction": fill_history.predict(),
        "stats": fill_history.stats(),
        "thresholds": {"warn": FILL_WARN_PCT, "full": FILL_FULL_PCT},
        "diagnostics": last_fill,
        "vision_mode": vision_mode,
    }
    if include_chart:
        status["chart"] = fill_history.chart()
    return status


def describe_eta(prediction) -> str:
    if not prediction:
        return "previsione non ancora disponibile"
    if prediction["full_at"] is None:
        return "non si riempie nei prossimi 30 giorni"
    when = datetime.fromtimestamp(prediction["full_at"]).strftime("%a %d/%m %H:%M")
    return f"pieno tra circa {prediction['hours_left']} h ({when})"


def check_fill_events(emptied: bool):
    """Eventi per il centro di controllo: svuotato, quasi pieno, pieno (una volta per ciclo)."""
    global fill_alert
    last = fill_history.last
    if last is None:
        return
    pct = last["fill_pct"]
    prediction = fill_history.predict()
    values = {"fill_pct": pct, "peak_pct": last["peak_pct"],
              "hours_left": prediction["hours_left"] if prediction else None}
    if emptied:
        fill_alert = None
        push_event("BIN_EMPTIED", "info", "Cassonetto svuotato",
                   f"Livello sceso al {pct}%.", values)
        new_odor_reference("svuotamento")
    level = fill_level_name(pct)
    if level == "full" and fill_alert != "full":
        fill_alert = "full"
        push_event("BIN_FULL", "critical", "Cassonetto pieno",
                   f"Riempimento {pct}% (picchi {last['peak_pct']}%): serve una raccolta.", values)
    elif level == "warn" and fill_alert is None:
        fill_alert = "warn"
        push_event("BIN_ALMOST_FULL", "warning", "Cassonetto quasi pieno",
                   f"Riempimento {pct}%, {describe_eta(prediction)}.", values)
    elif level == "ok" and pct < FILL_WARN_PCT - 10:
        fill_alert = None   # isteresi: si riarma solo quando il livello scende davvero


def mcu_call(name, *args, timeout=5):
    """Chiamata allo sketch che non fa cadere il loop se il Bridge non risponde."""
    try:
        return Bridge.call(name, *args, timeout=timeout)
    except Exception as exc:
        print(f"[{now_str()}] Bridge.call({name}) fallita: {exc}")
        return None


def phone_command(key, value):
    """Comando all'app IP camera (v0.12): parametri sulla pagina principale, es. /?torch=on.
    Su /control/... l'app ignora i parametri e risponde comunque 200."""
    try:
        conn, r = request(f"/?{key}={value}")
        r.read()
        conn.close()
        return True
    except Exception as e:
        print(f"[{now_str()}] Telefono, comando {key}={value}: ERRORE {e}")
        return False


def phone_torch(on):
    """Accende o spegne la torcia del telefono."""
    return phone_command("torch", "on" if on else "off")


def take_photo():
    """Scatta con la torcia del telefono accesa e legge il ToF nello stesso momento.
    Ritorna (jpeg, tof_mm) oppure (None, tof_mm) se la camera non risponde."""
    global camera_id
    if camera_id is None:
        camera_id = pick_camera()
        phone_command("camera", camera_id)   # obiettivo attivo, cosi' le impostazioni valgono per lui
    # Foto alla risoluzione dello stream: al massimo (12 MP) lo scatto e' lento e il modello
    # la riduce comunque a 640 px
    phone_command("snapshot_res", "stream")
    phone_torch(True)
    time.sleep(FLASH_SETTLE_S)
    try:
        method, data, ms = grab()
        if data[:2] != bytes((0xFF, 0xD8)):
            raise ValueError("la risposta non è un JPEG")
    except Exception as e:
        print(f"[{now_str()}] Camera: ERRORE {e}")
        data = None
    finally:
        phone_torch(False)
    tof = mcu_call("read_tof")
    tof = int(tof) if tof is not None else -1
    mcu_call("camera_status", data is not None)
    if data is not None:
        size = jpeg_size(data)
        res = f"{size[0]}x{size[1]}" if size else "?x?"
        print(f"[{now_str()}] Camera OK [{method}] {res} {len(data) // 1024} kB {ms:.0f} ms, ToF {tof} mm")
        save(data)
    return data, tof


def vision_step():
    """Una foto e, a seconda della modalita', snapshot / calibrazione / misura del riempimento."""
    global vision_mode, last_fill
    data, tof = take_photo()
    if data is None or depth_fill is None:
        return
    try:
        img = depth_fill.decode_jpeg(data)
    except Exception as exc:
        print(f"[{now_str()}] Foto non decodificabile: {exc}")
        return

    if vision_mode == "snapshot":
        depth_fill.snapshot(img)
        return

    auto = depth_fill.CFG["auto_calibration"]
    masks = [depth_fill.CFG["bin_mask"], depth_fill.CFG["rim_mask"]]
    missing_masks = [m for m in masks if not os.path.exists(m)]
    if missing_masks and not (auto and vision_mode == "calibrate"):
        # Senza maschere non si puo' misurare: con l'autocalibrazione le crea "calibrate",
        # altrimenti salviamo il fotogramma su cui disegnarle.
        depth_fill.snapshot(img)
        print(f"[{now_str()}] Visione: mancano {', '.join(missing_masks)}. "
              + ("Con il cassonetto vuoto imposta VISION_MODE = \"calibrate\"" if auto
                 else "Disegnale su calib_frame.jpg"))
        return

    if vision_mode == "calibrate":
        try:
            floor = depth_fill.calibrate(img, tof)
        except Exception as exc:
            print(f"[{now_str()}] Calibrazione fallita: {exc} (riprovo al prossimo scatto)")
            return
        print(f"[{now_str()}] Calibrazione a vuoto completata (fondo a {floor:.0f} mm, riferimento a "
              f"{depth_fill.CFG['rim_dist_mm']:.0f} mm, pieno a {depth_fill.CFG['bin_depth_mm']:.0f} mm "
              "dal fondo): passo a 'run'")
        vision_mode = "run"
        return

    missing = depth_fill.missing_calibration()
    if missing:
        print(f"[{now_str()}] Visione: manca {', '.join(missing)}. "
              "Con il cassonetto vuoto imposta VISION_MODE = \"calibrate\"")
        return
    try:
        result = depth_fill.analyze(img, fill_tracker, tof)
    except Exception as exc:
        print(f"[{now_str()}] Analisi di profondita' fallita: {exc}")
        return
    last_fill = result
    with fill_lock:
        saved = fill_history.append(result["ts"], result["fill_pct"], result["peak_pct"],
                                    "emptied" if result["emptied"] else "")
        status = fill_status()
    if cloud:
        cloud.add_fill(result["ts"], result["fill_pct"], result["peak_pct"], result["emptied"])
    print(f"[{now_str()}] Riempimento {result['fill_pct']}% (grezzo {result['raw_fill_pct']}%, "
          f"picchi {result['peak_pct']}%), superficie a {result['surface_mm']} mm, "
          f"riferimento a {result['rim_mm']} mm, ToF {'usato' if result['tof_used'] else 'non valido'}, "
          f"inferenza {result['inference_ms']} ms"
          f"{' - CASSONETTO SVUOTATO' if result['emptied'] else ''}"
          f"{'' if saved else ' (NON salvato su file)'}")
    print(f"[{now_str()}] Previsione: {describe_eta(status['prediction'])}")
    ui.send_message("fill", status)
    check_fill_events(result["emptied"])


# --- Loop principale ------------------------------------------------------------------------
def loop():
    """Le chiamate verso lo sketch si fanno qui, mai dentro le callback del Bridge."""
    global need_restore, baseline_restored, last_capture_t, fill_startup_check
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

    if fill_startup_check:  # qui e non all'avvio: la WebUI e' gia' partita
        fill_startup_check = False
        with fill_lock:
            check_fill_events(False)

    if vision_mode is not None and (last_capture_t is None
                                    or time.monotonic() - last_capture_t >= CAPTURE_INTERVAL_S):
        last_capture_t = time.monotonic()
        vision_step()

    time.sleep(1)


# --- Cloud (Vercel + Supabase) --------------------------------------------------------------
def cloud_status() -> dict:
    """Stato attuale per il pannello di controllo: lo stesso che vede la dashboard locale."""
    with lock:
        sample = last_sample
        alarm_status = gas_alarm_status(sample["tvoc_ppb"] if sample else None)
    with fill_lock:
        fill = fill_status(include_chart=False)   # la serie dei 30 giorni la ricostruisce il cloud
    with odor_lock:
        odor_status = odor.status()
    return {"sample": sample, "alarm": alarm_status, "odor": odor_status, "fill": fill,
            "vision_mode": vision_mode, "sensors": {"aht20": not faulty["aht20"], "sgp30": not faulty["sgp30"]}}


def on_cloud_command(command: dict):
    """Comandi dal pannello di controllo, arrivati con la risposta all'ultimo invio."""
    if command.get("command") == "mark_emptied":
        print(f"[{now_str()}] Cloud: il pannello segnala il cassonetto appena svuotato")
        new_odor_reference("manuale")


cloud = CloudSync(DATA_DIR, BIN_ADDRESS, cloud_status, on_cloud_command)
with fill_lock:
    queued = cloud.backfill_fill(fill_history.rows)
if queued:
    print(f"[{now_str()}] Cloud: {queued} misure di riempimento dal CSV messe in coda")
cloud.start()
if not cloud.enabled:
    cloud = None

print(f"[{now_str()}] Riempimento: {fill_history.last['fill_pct'] if fill_history.last else '—'}%, "
      f"{describe_eta(fill_history.predict())}")

scheme = "https" if USE_HTTPS else "http"
if vision_mode is None:
    print("Visione disattivata (solo sensori gas)")
else:
    print(f"Visione: modalita' {vision_mode}, camera {scheme}://{PHONE_IP}:{PORT}, "
          f"uno scatto ogni {CAPTURE_INTERVAL_S} s")
print(f"Odore: profilo '{odor.profile}', mediana su {odor.status()['window_label']}, soglie "
      f"{odor.p['yellow_ratio']}x / {odor.p['red_ratio']}x; allarme gas rapido "
      f"{'attivo' if GAS_ALARM_ENABLED else 'disattivato (modalita demo)'}")
App.run(user_loop=loop)
