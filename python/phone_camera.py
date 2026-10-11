"""Phone used as camera (IP Webcam app): HTTP(S) requests, lens choice, photos and commands."""

import base64
import http.client
import json
import re
import ssl
import time

from common import now_str
from config import CAMERA, PASSWORD, PHONE_IP, PORT, SAVE_PATH, SNAPSHOT_TIMEOUT_S, USE_HTTPS, USER

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64)",
    "Accept": "image/jpeg,image/*,*/*",
}
if USER:
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    HEADERS["Authorization"] = "Basic " + token

# The phone uses a self-signed certificate: no verification (fine on a local network)
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


# --- Lens --------------------------------------------------------------------------------
camera_id = None   # lens chosen at the first photo (pick_camera)


def pick_camera():
    """Camera to use: CAMERA if set, otherwise the widest back camera in /info.json: shortest
    focal length in the label ("Back Lens 2 (1.4mm)"), then lowest minimum zoom (many phones
    report 1.0x for every camera)."""
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


def select_camera_once():
    """At the first photo: choose the lens and make it the active one, so the settings apply to it."""
    global camera_id
    if camera_id is None:
        camera_id = pick_camera()
        phone_command("camera", camera_id)


# --- Photos ------------------------------------------------------------------------------
def phone_snapshot():
    # The app can take up to 14 s (it reopens the camera, retries): 5 s are not enough
    conn, r = request(f"/video/snapshot?camera={camera_id or 'back'}", timeout=SNAPSHOT_TIMEOUT_S)
    try:
        return r.read()
    finally:
        conn.close()


def frame_from_stream():
    """Extracts the first JPEG frame from the MJPEG stream /video."""
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
preferred = 0   # the method that worked last time is tried first


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
    """Reads width and height from the JPEG header (SOF marker)."""
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


# --- Commands ----------------------------------------------------------------------------
def phone_command(key, value):
    """Command to the IP camera app (v0.12): parameters on the main page, e.g. /?torch=on.
    On /control/... the app ignores the parameters and answers 200 anyway."""
    try:
        conn, r = request(f"/?{key}={value}")
        r.read()
        conn.close()
        return True
    except Exception as e:
        print(f"[{now_str()}] Telefono, comando {key}={value}: ERRORE {e}")
        return False


def phone_torch(on):
    """Turns the phone torch on or off."""
    return phone_command("torch", "on" if on else "off")
