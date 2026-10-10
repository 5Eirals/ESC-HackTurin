"""
Toret-Breath Vision – bin fill estimation with monocular depth (Strategy 2).
Runs on the Linux side of the Arduino UNO Q.

  pip install onnxruntime opencv-python-headless numpy

Usage:
  python depth_fill.py snapshot    # save calib_frame.jpg to draw the masks on
  python depth_fill.py calibrate   # with the bin EMPTY: builds empty_depth.npy
  python depth_fill.py run         # periodic measurement + logging
  python depth_fill.py predict     # estimate time until the bin is full

How it works:
  The model outputs RELATIVE inverse depth (bigger = closer), with an unknown
  scale and shift that change from image to image. Every frame we re-fit them
  with two anchors of known distance:
    1. the bin rim  -> always visible, fixed distance from the camera
    2. the ToF spot -> Modulino Distance gives the true distance at one pixel
  Then:  1/Z = a * d + b   ->  metric depth Z for every pixel.
  Fill height = (empty-bin depth) - (current depth), inside the bin mask.
"""
import csv, json, os, sys, time
import numpy as np
import cv2
import onnxruntime as ort

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
    "log": "fill_log.csv",
    "emptied_drop_pct": 30,       # a drop bigger than this = bin was emptied
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
    return np.median(np.stack(frames), axis=0).astype(np.uint8)  # denoise

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

# ---------------------------------------------------------------- modes
def snapshot():
    cv2.imwrite("calib_frame.jpg", capture())
    print("Saved calib_frame.jpg – paint bin_mask.png and rim_mask.png on it (white = area).")

def calibrate():
    img = capture()
    d = infer(img)
    rim = load_mask(CFG["rim_mask"], d.shape)
    z, ab = to_metric(d, rim, tof_mm())
    np.save(CFG["empty_depth"], z)
    json.dump({"a": ab[0], "b": ab[1]}, open("calib_ab.json", "w"))
    binm = load_mask(CFG["bin_mask"], d.shape)
    print(f"Empty-bin floor depth (median): {np.median(z[binm]):.0f} mm "
          f"– expected about {CFG['rim_dist_mm'] + CFG['bin_depth_mm']} mm")

def measure_once(history):
    img = capture()
    d = infer(img)
    rim = load_mask(CFG["rim_mask"], d.shape)
    binm = load_mask(CFG["bin_mask"], d.shape)
    empty_z = np.load(CFG["empty_depth"])
    fb = json.load(open("calib_ab.json")) if os.path.exists("calib_ab.json") else None
    z, _ = to_metric(d, rim, tof_mm(), (fb["a"], fb["b"]) if fb else None)

    fill_h = np.clip(empty_z - z, 0, CFG["bin_depth_mm"])[binm]
    fill_pct = 100 * np.median(fill_h) / CFG["bin_depth_mm"]   # typical level
    peak_pct = 100 * np.percentile(fill_h, 90) / CFG["bin_depth_mm"]  # heaps

    history.append(fill_pct)
    smooth = float(np.median(history[-3:]))                     # temporal filter
    return round(smooth, 1), round(float(peak_pct), 1)

def run():
    history, last = [], None
    new = not os.path.exists(CFG["log"])
    with open(CFG["log"], "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "fill_pct", "peak_pct", "event"])
        while True:
            try:
                fill, peak = measure_once(history)
                event = ""
                if last is not None and last - fill > CFG["emptied_drop_pct"]:
                    event = "emptied"
                    history = [fill]
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), fill, peak, event])
                f.flush()
                print(f"fill {fill}%  peak {peak}%  {event}")
                last = fill
            except Exception as e:
                print("[error]", e)
            time.sleep(CFG["interval_s"])

def predict():
    """Linear fill rate since the last emptying -> hours until 100%."""
    rows = list(csv.DictReader(open(CFG["log"])))
    start = max((i for i, r in enumerate(rows) if r["event"] == "emptied"), default=0)
    rows = rows[start:]
    if len(rows) < 4:
        print("Not enough data since last emptying."); return
    t = np.array([time.mktime(time.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")) for r in rows])
    y = np.array([float(r["fill_pct"]) for r in rows])
    rate, intercept = np.polyfit((t - t[0]) / 3600, y, 1)       # % per hour
    if rate <= 0:
        print("Fill level not rising."); return
    hours_left = (100 - y[-1]) / rate
    print(f"Fill rate {rate:.2f}%/h – full in about {hours_left:.1f} h")

if __name__ == "__main__":
    # Resolve model, masks, calibration and log next to this script,
    # whatever folder the script is launched from.
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    {"snapshot": snapshot, "calibrate": calibrate, "run": run, "predict": predict}.get(
        sys.argv[1] if len(sys.argv) > 1 else "run", run)()