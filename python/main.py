from arduino.app_utils import *
import os, sys
import base64
import http.client
import ssl
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import depth_fill

# App Lab starts main.py without arguments, so choose the mode here:
#   "snapshot"  -> save calib_frame.jpg to draw the masks on
#   "calibrate" -> with the bin EMPTY, build the empty-bin depth map
#   "run"       -> normal operation: measure every 15 minutes and log
MODE = "snapshot"

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


def loop():
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


scheme = "https" if USE_HTTPS else "http"
print(f"Test camera su {scheme}://{PHONE_IP}:{PORT}")
App.run(user_loop=loop)
