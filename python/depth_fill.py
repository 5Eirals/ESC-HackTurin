"""
TO-Breath Vision – bin fill estimation with monocular depth (Strategy 2).
Runs on the Linux side of the Arduino UNO Q.

  pip install onnxruntime opencv-python-headless numpy

Usage (standalone, USB camera):
  python depth_fill.py snapshot    # save calib_frame.jpg to draw the masks on
  python depth_fill.py calibrate   # with the bin EMPTY: builds empty_depth.npy
  python depth_fill.py run         # periodic measurement + logging + time-to-full prediction

From main.py (phone camera): the frame is passed in as `img`, e.g.
  img = decode_jpeg(jpeg_bytes); snapshot(img) / calibrate(img) / analyze(img, tracker)

How it works:
  The model outputs RELATIVE inverse depth (bigger = closer), with an unknown
  scale and shift that change from image to image. Every frame we re-fit them
  with two anchors of known distance:
    1. the bin rim  -> always visible, fixed distance from the camera
    2. the ToF spot -> Modulino Distance gives the true distance at one pixel
  Then:  1/Z = a * d + b   ->  metric depth Z for every pixel.
  Fill height = (empty-bin depth) - (current depth), inside the bin mask.
"""
import json, os, sys, time
import numpy as np
import cv2
import onnxruntime as ort

from fill_history import FillHistory

CFG = {
    "model": "depth_anything_v2_vits_252.onnx",
    "input_size": 252,
    "camera_index": 0,            # check with: v4l2-ctl --list-devices
    "bin_mask": "bin_mask.png",   # white = bin interior seen by the camera
    "rim_mask": "rim_mask.png",   # white = rim pixels (never covered by waste)
    "rim_dist_mm": 150,           # measured once: camera -> rim
    "tof_px": [320, 240],         # pixel where the ToF beam lands (find once)
    "tof_radius": 6,
    "bin_depth_mm": 1100,         # rim -> floor, measured once
    "empty_depth": "empty_depth.npy",
    "interval_s": 900,            # one measurement every 15 minutes
    "log": os.path.join("..", "data", "fill_log.csv"),  # shared with main.py / dashboard
    "emptied_drop_pct": 30,       # a drop bigger than this = bin was emptied
    "work_width": 640,            # frames are resized to this width (masks + tof_px refer to it)
    "calib_frame": "calib_frame.jpg",
    "calib_ab": "calib_ab.json",
}

# ---------------------------------------------------------------- MCU bridge
# LED ring and Modulino Distance live on the MCU side (see sketch.ino).
# App Lab exposes them to Python through the Bridge; check the import path
# against your App Lab version.
try:
    from arduino.app_utils import Bridge
    def flash(on):        Bridge.call("set_flash", 255 if on else 0)
    def tof_mm():         return int(Bridge.call("read_tof"))
except Exception:
    print("[warn] Bridge not available – flash off, ToF disabled (desk testing).")
    def flash(on):        pass
    def tof_mm():         return -1

# ---------------------------------------------------------------- capture
def capture():
    cap = cv2.VideoCapture(CFG["camera_index"])
    flash(True)
    time.sleep(0.3)
    for _ in range(5):            # let auto-exposure settle on the flash
        cap.read()
    frames = [cap.read()[1] for _ in range(3)]
    flash(False)
    cap.release()
    frames = [f for f in frames if f is not None]
    if not frames:
        raise RuntimeError("No frames from camera")
    return to_work_size(np.median(np.stack(frames), axis=0).astype(np.uint8))  # denoise

def to_work_size(img):
    """Resize to CFG["work_width"] keeping the aspect ratio, so that masks and tof_px
    always refer to the same resolution as calib_frame.jpg."""
    h, w = img.shape[:2]
    W = CFG["work_width"]
    if w == W:
        return img
    return cv2.resize(img, (W, round(h * W / w)), interpolation=cv2.INTER_AREA)

def decode_jpeg(data):
    """JPEG bytes (e.g. from the phone camera) -> BGR image at work size."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("JPEG could not be decoded")
    return to_work_size(img)

# ---------------------------------------------------------------- inference
_sess = None
def infer(img_bgr):
    global _sess
    if _sess is None:
        _sess = ort.InferenceSession(CFG["model"], providers=["CPUExecutionProvider"])
    s = CFG["input_size"]
    x = cv2.cvtColor(cv2.resize(img_bgr, (s, s), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    x = (x.astype(np.float32) / 255.0 - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
    x = x.transpose(2, 0, 1)[None].astype(np.float32)
    d = _sess.run(None, {"image": x})[0].squeeze()
    h, w = img_bgr.shape[:2]
    return cv2.resize(d, (w, h), interpolation=cv2.INTER_LINEAR)

# ---------------------------------------------------------------- scale fit
def load_mask(path, shape):
    m = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if m is None:
        raise FileNotFoundError(f"{path} missing – draw it on calib_frame.jpg")
    return cv2.resize(m, (shape[1], shape[0])) > 127

def anchor_value(d, mask):
    return float(np.median(d[mask]))

def tof_value(d):
    x, y = CFG["tof_px"]; r = CFG["tof_radius"]
    h, w = d.shape[:2]
    x, y = min(max(x, r), w - r), min(max(y, r), h - r)       # stay inside the frame
    return float(np.median(d[y - r:y + r, x - r:x + r]))

def to_metric(d, rim_mask, tof_distance_mm, fallback=None):
    """Fit 1/Z = a*d + b from the rim anchor and the ToF anchor."""
    d_rim, z_rim = anchor_value(d, rim_mask), CFG["rim_dist_mm"]
    if tof_distance_mm > 0:
        d_tof, z_tof = tof_value(d), tof_distance_mm
        if abs(d_tof - d_rim) > 1e-3 * (abs(d_rim) + 1):          # not degenerate
            a = (1 / z_tof - 1 / z_rim) / (d_tof - d_rim)
            b = 1 / z_rim - a * d_rim
            return 1.0 / np.clip(a * d + b, 1e-6, None), (a, b)
    # Fallback: ToF missing or bin almost full (both anchors at the same depth):
    # keep the calibrated shift b, re-fit only the scale on the rim.
    if fallback is None:
        raise RuntimeError("No valid anchors and no fallback calibration")
    b = fallback[1]
    a = (1 / z_rim - b) / d_rim
    return 1.0 / np.clip(a * d + b, 1e-6, None), (a, b)

# ---------------------------------------------------------------- calibration state
def missing_calibration():
    """Files still needed before fill can be measured (empty list = ready)."""
    need = [CFG["bin_mask"], CFG["rim_mask"], CFG["empty_depth"]]
    return [p for p in need if not os.path.exists(p)]

def load_fallback():
    if not os.path.exists(CFG["calib_ab"]):
        return None
    ab = json.load(open(CFG["calib_ab"]))
    return ab["a"], ab["b"]

# ---------------------------------------------------------------- analysis
class FillTracker:
    """Temporal median filter over the last 3 measurements + emptying detection."""
    def __init__(self, last=None):
        # last: previous level (e.g. from the log), so an emptying across a restart is detected
        self.history, self.last = ([last] if last is not None else []), last

    def update(self, fill_pct):
        emptied = self.last is not None and self.last - fill_pct > CFG["emptied_drop_pct"]
        if emptied:
            self.history = []
        self.history.append(fill_pct)
        smooth = round(float(np.median(self.history[-3:])), 1)
        self.last = smooth
        return smooth, emptied

def analyze(img, tracker, tof_distance_mm=None):
    """Depth analysis of one frame -> fill level of the bin.

    img: BGR image at work size (capture() or decode_jpeg()).
    Returns a dict with fill/peak percentage, depth figures and diagnostics."""
    if tof_distance_mm is None:
        tof_distance_mm = tof_mm()
    t0 = time.time()
    d = infer(img)
    infer_ms = (time.time() - t0) * 1000
    rim = load_mask(CFG["rim_mask"], d.shape)
    binm = load_mask(CFG["bin_mask"], d.shape)
    empty_z = np.load(CFG["empty_depth"])
    if empty_z.shape != d.shape:                                 # camera resolution changed
        empty_z = cv2.resize(empty_z, (d.shape[1], d.shape[0]), interpolation=cv2.INTER_LINEAR)
    fb = load_fallback()
    z, ab = to_metric(d, rim, tof_distance_mm, fb)

    fill_h = np.clip(empty_z - z, 0, CFG["bin_depth_mm"])[binm]
    raw_pct = 100 * float(np.median(fill_h)) / CFG["bin_depth_mm"]   # typical level
    peak_pct = 100 * float(np.percentile(fill_h, 90)) / CFG["bin_depth_mm"]  # heaps
    fill_pct, emptied = tracker.update(round(raw_pct, 1))
    return {
        "ts": time.time(),
        "fill_pct": fill_pct,                    # smoothed (median of last 3)
        "raw_fill_pct": round(raw_pct, 1),
        "peak_pct": round(peak_pct, 1),
        "emptied": emptied,
        "surface_mm": round(float(np.median(z[binm]))),  # camera -> waste surface
        "rim_mm": round(float(np.median(z[rim]))),       # sanity check vs rim_dist_mm
        "tof_mm": int(tof_distance_mm),
        "tof_used": bool(fb is None or ab[1] != fb[1]),  # False = rim-only fallback fit
        "inference_ms": round(infer_ms),
    }

# ---------------------------------------------------------------- modes
def snapshot(img=None):
    cv2.imwrite(CFG["calib_frame"], capture() if img is None else img)
    print(f"Saved {CFG['calib_frame']} – paint bin_mask.png and rim_mask.png on it (white = area).")

def calibrate(img=None, tof_distance_mm=None):
    img = capture() if img is None else img
    if tof_distance_mm is None:
        tof_distance_mm = tof_mm()
    d = infer(img)
    rim = load_mask(CFG["rim_mask"], d.shape)
    z, ab = to_metric(d, rim, tof_distance_mm)
    np.save(CFG["empty_depth"], z)
    json.dump({"a": ab[0], "b": ab[1]}, open(CFG["calib_ab"], "w"))
    binm = load_mask(CFG["bin_mask"], d.shape)
    floor = float(np.median(z[binm]))
    print(f"Empty-bin floor depth (median): {floor:.0f} mm "
          f"– expected about {CFG['rim_dist_mm'] + CFG['bin_depth_mm']} mm")
    return floor

def describe_prediction(p):
    """Human-readable time-to-full line (used by run() and main.py)."""
    if p is None:
        return "not enough data since last emptying for a prediction"
    if p["full_at"] is None:
        return f"fill rate {p['rate_pct_day']}%/day – not full within 30 days"
    when = time.strftime("%a %d/%m %H:%M", time.localtime(p["full_at"]))
    return f"fill rate {p['rate_pct_day']}%/day – full in about {p['hours_left']} h ({when}, {p['method']})"

def run():
    """Measure every interval_s, log to CFG["log"] and predict when the bin will be full
    (run and predict merged: the prediction is refreshed after every measurement)."""
    history = FillHistory(CFG["log"])
    tracker = FillTracker(history.last["fill_pct"] if history.last else None)
    while True:
        try:
            r = analyze(capture(), tracker)
            event = "emptied" if r["emptied"] else ""
            history.append(r["ts"], r["fill_pct"], r["peak_pct"], event)
            print(f"fill {r['fill_pct']}%  peak {r['peak_pct']}%  {event}")
            print("  " + describe_prediction(history.predict()))
        except Exception as e:
            print("[error]", e)
        time.sleep(CFG["interval_s"])

if __name__ == "__main__":
    # Resolve model, masks, calibration and log next to this script,
    # whatever folder the script is launched from.
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    {"snapshot": snapshot, "calibrate": calibrate, "run": run}.get(
        sys.argv[1] if len(sys.argv) > 1 else "run", run)()