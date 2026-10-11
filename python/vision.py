"""Vision: photo with the phone + ToF, depth analysis (depth_fill.py), fill history and forecast
(fill_history.py) and the fill events for the control centre."""

import os
import sys
import threading
import time
from datetime import datetime

import phone_camera
import simulate_fill
import state
from common import mcu_call, now_str
from config import (CAPTURE_INTERVAL_S, DATA_DIR, FILL_FULL_PCT, FILL_WARN_PCT, FLASH_SETTLE_S,
                    SIMULATED_HISTORY, VISION_MODE, VISION_MODES)
from events import push_event
from fill_history import FillHistory
from gas_sensors import new_odor_reference

try:
    import depth_fill
except Exception as exc:  # e.g. onnxruntime/opencv not installed
    print(f"[warn] depth_fill non disponibile ({exc}): niente analisi di profondita', "
          "le foto vengono solo salvate e i sensori gas restano attivi")
    depth_fill = None

vision_mode = None      # set by init(): "snapshot" | "calibrate" | "run" | None (off)
last_capture_t = None   # time.monotonic() of the last photo
last_fill = None        # last result of depth_fill.analyze() (diagnostics for the dashboard)

fill_lock = threading.Lock()   # fill_history is written by the loop and read when the dashboard connects
fill_history = FillHistory(DATA_DIR / "fill_log.csv")
fill_tracker = None
fill_alert = None       # last level reported to the control centre: None | "warn" | "full"
fill_startup_check = True  # at the first loop round, report the initial state (e.g. already almost full)


def pick_vision_mode():
    mode = sys.argv[1] if len(sys.argv) > 1 else None   # python main.py calibrate
    mode = mode or os.environ.get("MODE")               # MODE=calibrate python main.py
    if not mode and os.path.exists("mode.txt"):         # mode.txt containing "calibrate"
        mode = open("mode.txt").read().strip()
    mode = (mode or VISION_MODE or "").lower() or None
    if mode == "off":
        mode = None
    if mode is not None and mode not in VISION_MODES:
        print(f"[warn] modalita' visione '{mode}' sconosciuta (usa {', '.join(VISION_MODES)}): uso 'run'")
        mode = "run"
    return mode


def init():
    """Vision mode, simulated history (if enabled and there is no measurement yet) and the filter."""
    global vision_mode, fill_tracker
    vision_mode = pick_vision_mode()
    if SIMULATED_HISTORY and not fill_history.rows:
        n = simulate_fill.generate(fill_history.path)
        fill_history.load()
        print(f"[{now_str()}] Nessuna misura di riempimento: generati {n} campioni simulati (30 giorni)")
    fill_tracker = (depth_fill.FillTracker(fill_history.last["fill_pct"] if fill_history.last else None)
                    if depth_fill else None)


# --- Fill level and forecast ---------------------------------------------------------------
def fill_level_name(pct):
    if pct is None:
        return "unknown"
    if pct >= FILL_FULL_PCT:
        return "full"
    if pct >= FILL_WARN_PCT:
        return "warn"
    return "ok"


def fill_status(include_chart=True) -> dict:
    """Everything the "Fill level" section of the dashboard needs."""
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
    """Events for the control centre: emptied, almost full, full (once per cycle)."""
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
        fill_alert = None   # hysteresis: re-armed only when the level really drops


# --- Photo + depth analysis ------------------------------------------------------------------
def take_photo():
    """Takes a photo with the phone torch on and reads the ToF at the same moment.
    Returns (jpeg, tof_mm), or (None, tof_mm) if the camera does not answer."""
    phone_camera.select_camera_once()
    # Photo at the stream resolution: at full size (12 MP) the photo is slow and the model
    # shrinks it to 640 px anyway
    phone_camera.phone_command("snapshot_res", "stream")
    phone_camera.phone_torch(True)
    time.sleep(FLASH_SETTLE_S)
    try:
        method, data, ms = phone_camera.grab()
        if data[:2] != bytes((0xFF, 0xD8)):
            raise ValueError("la risposta non è un JPEG")
    except Exception as e:
        print(f"[{now_str()}] Camera: ERRORE {e}")
        data = None
    finally:
        phone_camera.phone_torch(False)
    tof = mcu_call("read_tof")
    tof = int(tof) if tof is not None else -1
    mcu_call("camera_status", data is not None)
    if data is not None:
        size = phone_camera.jpeg_size(data)
        res = f"{size[0]}x{size[1]}" if size else "?x?"
        print(f"[{now_str()}] Camera OK [{method}] {res} {len(data) // 1024} kB {ms:.0f} ms, ToF {tof} mm")
        phone_camera.save(data)
    return data, tof


def vision_step():
    """One photo and, depending on the mode, snapshot / calibration / fill measurement."""
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
        # Without masks nothing can be measured: with the auto-calibration "calibrate" creates
        # them, otherwise we save the frame to draw them on.
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
    if state.cloud:
        state.cloud.add_fill(result["ts"], result["fill_pct"], result["peak_pct"], result["emptied"])
    print(f"[{now_str()}] Riempimento {result['fill_pct']}% (grezzo {result['raw_fill_pct']}%, "
          f"picchi {result['peak_pct']}%), superficie a {result['surface_mm']} mm, "
          f"riferimento a {result['rim_mm']} mm, ToF {'usato' if result['tof_used'] else 'non valido'}, "
          f"fondo occupato {result.get('anchors_pct', 0)}%, inferenza {result['inference_ms']} ms"
          f"{' - CASSONETTO SVUOTATO' if result['emptied'] else ''}"
          f"{'' if saved else ' (NON salvato su file)'}")
    print(f"[{now_str()}] Previsione: {describe_eta(status['prediction'])}")
    state.ui.send_message("fill", status)
    check_fill_events(result["emptied"])


# --- Steps of the main loop ----------------------------------------------------------------
def report_startup_fill():
    """At the first loop round, reports the initial fill state (e.g. already almost full).
    Done here and not at start-up because by now the WebUI is running."""
    global fill_startup_check
    if fill_startup_check:
        fill_startup_check = False
        with fill_lock:
            check_fill_events(False)


def capture_if_due():
    """One photo every CAPTURE_INTERVAL_S, when the vision is enabled."""
    global last_capture_t
    if vision_mode is not None and (last_capture_t is None
                                    or time.monotonic() - last_capture_t >= CAPTURE_INTERVAL_S):
        last_capture_t = time.monotonic()
        vision_step()
