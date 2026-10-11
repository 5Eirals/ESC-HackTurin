"""
TO-Breath Vision – bin fill estimation with monocular depth (Strategy 2).
Runs on the Linux side of the Arduino UNO Q.

  pip install onnxruntime opencv-python-headless numpy

Usage (standalone, USB camera):
  python depth_fill.py snapshot    # save calib_frame.jpg (only needed to draw masks by hand)
  python depth_fill.py calibrate   # with the bin EMPTY: masks, distances and empty_depth.npy
  python depth_fill.py run         # periodic measurement + logging + time-to-full prediction

From main.py (phone camera): the frame is passed in as `img`, e.g.
  img = decode_jpeg(jpeg_bytes); snapshot(img) / calibrate(img) / analyze(img, tracker)

How it works:
  The model outputs RELATIVE inverse depth (bigger = closer), with an unknown
  scale and shift that change from image to image. Every frame we re-fit them
  with two anchors of known distance:
    1. the reference band -> along the image edge, fixed distance from the camera
       (the bin rim when it is in view)
    2. the ToF spot       -> Modulino Distance gives the true distance at one pixel
  Then:  1/Z = a * d + b   ->  metric depth Z for every pixel.
  Fill height = (empty-bin depth) - (current depth), inside the bin mask.

  Calibration (bin EMPTY) is automatic: the AI depth map gives the floor (bin_mask.png),
  the reference band (rim_mask.png) and, with the ToF distance to the floor, the band's
  distance from the camera. Nothing is measured or drawn by hand (see auto_calibrate()).
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
    "bin_mask": "bin_mask.png",   # white = bin floor seen by the camera
    "rim_mask": "rim_mask.png",   # white = reference pixels (never covered by waste)
    "rim_dist_mm": 150,           # camera -> reference band (auto-calibration overwrites it)
    "tof_px": None,               # pixel where the ToF beam lands; None = image centre
    "tof_radius": 6,
    "bin_depth_mm": 1100,         # floor -> "full" level (auto-calibration overwrites it)
    "auto_calibration": True,     # calibrate: the AI finds floor, band and distances by itself
    "auto_band_px": 12,           # thickness of the reference band along the image edge
    "auto_floor_grad": 0.2,       # floor = depth changes slower than this share of the walls
    "full_dist_mm": None,         # camera -> "full" level; None = the reference band
    "calib_overlay": "calib_overlay.jpg",  # what the AI found, to check by eye
    "anchor_tol": 0.15,           # pixel "unchanged" if its brightness differs less than this
    "anchor_min": 0.03,           # share of unchanged pixels needed for the anchored fit
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

def tof_spot(shape):
    """Pixel where the ToF beam lands: CFG["tof_px"], or the image centre (sensor next to the lens)."""
    h, w = shape[:2]
    return CFG["tof_px"] if CFG["tof_px"] is not None else [w // 2, h // 2]

def tof_value(d):
    x, y = tof_spot(d.shape); r = CFG["tof_radius"]
    h, w = d.shape[:2]
    x, y = min(max(x, r), w - r), min(max(y, r), h - r)       # stay inside the frame
    return float(np.median(d[y - r:y + r, x - r:x + r]))

def to_metric(d, rim_mask, tof_distance_mm, fallback=None):
    """Fit 1/Z = a*d + b from the rim anchor and the ToF anchor."""
    d_rim, z_rim = anchor_value(d, rim_mask), CFG["rim_dist_mm"]
    if tof_distance_mm > 0:
        d_tof, z_tof = tof_value(d), tof_distance_mm
        # Anchors too close to each other (bin almost full) would blow up the fit: use the fallback
        if z_tof > 1.15 * z_rim and abs(d_tof - d_rim) > 1e-6:
            a = (1 / z_tof - 1 / z_rim) / (d_tof - d_rim)
            b = 1 / z_rim - a * d_rim
            return 1.0 / np.clip(a * d + b, 1e-6, None), (a, b), True
    # Fallback: ToF missing or bin almost full (both anchors at the same depth):
    # keep the calibrated shift b, re-fit only the scale on the rim.
    if fallback is None:
        raise RuntimeError("No valid anchors and no fallback calibration")
    b = fallback[1]
    a = (1 / z_rim - b) / d_rim
    return 1.0 / np.clip(a * d + b, 1e-6, None), (a, b), False

def unchanged_pixels(img, binm):
    """Pixels that look as in the calibration photo (the camera is fixed): walls and the floor
    still uncovered. Their distance is known from empty_depth.npy, so they are anchors."""
    ref = cv2.imread(CFG["calib_frame"])
    if ref is None or ref.shape != img.shape:
        return None
    def gray(im):
        return cv2.GaussianBlur(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 3) + 1
    ratio = gray(img) / gray(ref)
    # The phone's auto-exposure changes the overall brightness when bright waste comes in:
    # normalise on the walls, which stay uncovered the longest
    walls = ~cv2.dilate(binm.astype(np.uint8), np.ones((31, 31), np.uint8)).astype(bool)
    ratio /= np.median(ratio[walls])
    same = (np.abs(ratio - 1) < CFG["anchor_tol"]).astype(np.uint8)
    return cv2.erode(same, np.ones((7, 7), np.uint8)) > 0

def to_metric_anchored(d, same, empty_z, tof_distance_mm):
    """Fit 1/Z = a*d + b on all unchanged pixels (known depth from the calibration), dropping the
    worst 20% at each pass. With a valid ToF the line goes through the ToF point, so the waste
    under the sensor is exact and the anchors give the slope."""
    x = d[same].astype(np.float64)
    y = 1.0 / empty_z[same]
    pinned = tof_distance_mm > 0
    if pinned:
        dt, yt = tof_value(d), 1.0 / tof_distance_mm
    keep = np.ones(len(x), bool)
    for _ in range(4):
        if pinned:
            dx, dy = x[keep] - dt, y[keep] - yt
            a = float((dx * dy).sum() / (dx * dx).sum())
            b = yt - a * dt
        else:
            a, b = (float(v) for v in np.polyfit(x[keep], y[keep], 1))
        r = np.abs(a * x + b - y)
        keep = r <= np.percentile(r[keep], 80)
    return 1.0 / np.clip(a * d + b, 1e-6, None), (a, b), pinned

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

def load_geometry():
    """Distances found by the auto-calibration (saved in calib_ab.json) replace the CFG values."""
    if os.path.exists(CFG["calib_ab"]):
        saved = json.load(open(CFG["calib_ab"]))
        for key in ("rim_dist_mm", "bin_depth_mm"):
            if key in saved:
                CFG[key] = saved[key]

# ---------------------------------------------------------------- auto-calibration
# With the bin EMPTY the AI depth map replaces the measurements by hand.
# - Floor: the flat region around the image centre (one plane in the depth map).
# - Walls: along a plane the model output d = a/Z + b is a straight line in the image. Two
#   opposite walls are parallel, so their lines meet on their common vanishing line, where Z is
#   infinite and therefore d = b: the meeting point gives the shift b, even if the camera is tilted.
# - Scale: the ToF reads the floor distance Z_f, so a = (d_floor - b) * Z_f.
# Then Z = a / (d - b) for every pixel, including the reference band along the image edge that
# replaces the bin rim (the rim itself is usually out of view with the camera under the lid).

def _floor_region(d):
    """Floor of the empty bin: the region around the centre where depth changes slowly.
    The walls, which fill the image edges, change fast; the floor is flat (or almost)."""
    h, w = d.shape
    ds = cv2.GaussianBlur(d.astype(np.float32), (0, 0), 3)
    grad = np.hypot(cv2.Sobel(ds, cv2.CV_32F, 1, 0), cv2.Sobel(ds, cv2.CV_32F, 0, 1)) / 8
    edges = np.ones(d.shape, bool)
    edges[h // 10:h - h // 10, w // 10:w - w // 10] = False
    thr = CFG["auto_floor_grad"] * np.median(grad[edges])
    slow = cv2.morphologyEx((grad < thr).astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    # The floor is the largest slow region reaching the central window (the exact centre pixel
    # may be off: ToF board in view, a reflection of the flash, a crease of the cardboard)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(slow)
    win = labels[h // 2 - h // 8:h // 2 + h // 8, w // 2 - w // 8:w // 2 + w // 8]
    ids = [i for i in np.unique(win) if i != 0]
    _floor_region.debug = (f"edge gradient {np.median(grad[edges]):.5f}, centre gradient "
                           f"{np.median(grad[h // 2 - 20:h // 2 + 20, w // 2 - 20:w // 2 + 20]):.5f}, "
                           f"threshold {thr:.5f}")
    if not ids:
        return np.zeros(d.shape, bool)
    best = max(ids, key=lambda i: stats[i, cv2.CC_STAT_AREA])
    return labels == best

def _shift_from_walls(profile, lo, hi):
    """Shift b where the lines of the two walls on a profile meet (None if not usable).
    profile: d across the image; lo/hi: first and last floor index on it."""
    n = len(profile)
    lines = []
    # Wall pixels only: away from the image edge and from the blurred crease with the floor
    for start, stop in ((3, lo - max(10, lo // 4)), (hi + max(10, (n - hi) // 4), n - 3)):
        t = np.arange(start, stop)
        if len(t) < 20:
            return None, None
        s, c = np.polyfit(t, profile[t], 1)
        r2 = 1 - np.var(profile[t] - (s * t + c)) / np.var(profile[t])
        lines.append((s, c, r2))
    (s1, c1, r1), (s2, c2, r2) = lines
    fit = round(min(r1, r2), 4)
    # Each wall must get closer towards the image edge and be a clean straight line
    if s1 >= 0 or s2 <= 0 or fit < 0.98:
        return None, fit
    t0 = (c2 - c1) / (s1 - s2)
    if not lo < t0 < hi:                       # the vanishing line must cross the floor
        return None, fit
    return float(s1 * t0 + c1), fit

def _edge_bands(shape):
    """Bands along the four image edges, without the corners where two walls meet."""
    h, w = shape
    t = CFG["auto_band_px"]
    bands = {name: np.zeros(shape, bool) for name in ("top", "bottom", "left", "right")}
    bands["top"][:t, w // 10:w - w // 10] = True
    bands["bottom"][h - t:, w // 10:w - w // 10] = True
    bands["left"][h // 10:h - h // 10, :t] = True
    bands["right"][h // 10:h - h // 10, w - t:] = True
    return bands

def auto_calibrate(d, tof_distance_mm):
    """Empty bin: floor, reference band and their distances from the AI depth map and the ToF.
    Writes bin_mask.png and rim_mask.png, updates CFG and returns the figures to save."""
    if tof_distance_mm <= 0:
        raise RuntimeError("auto-calibration needs a valid ToF reading of the empty bin floor")
    h, w = d.shape
    floor = _floor_region(d)
    if floor.mean() < 0.05:
        raise RuntimeError(f"floor not found ({100 * floor.mean():.1f}% of the image; "
                           f"{_floor_region.debug}): is the bin empty and the camera looking at "
                           f"its centre? See {CFG['calib_overlay']}")
    rows = np.nonzero(floor[:, w // 2])[0]
    cols = np.nonzero(floor[h // 2, :])[0]

    # Shift b from the two pairs of opposite walls (a few central columns / rows, against noise)
    k = max(2, w // 32)
    b_rows, fit_rows = _shift_from_walls(np.median(d[:, w // 2 - k:w // 2 + k], axis=1),
                                         rows.min(), rows.max())
    b_cols, fit_cols = _shift_from_walls(np.median(d[h // 2 - k:h // 2 + k, :], axis=0),
                                         cols.min(), cols.max())
    shifts = [b for b in (b_rows, b_cols) if b is not None]
    if not shifts:
        raise RuntimeError("walls not found on both sides of the floor: centre the camera over the bin")
    b = float(np.mean(shifts))

    # Scale a from the ToF distance to the floor
    x, y = tof_spot(d.shape)
    on_floor = 0 <= x < w and 0 <= y < h and floor[y, x]
    d_floor = tof_value(d) if on_floor else float(np.median(d[floor]))
    if not on_floor:
        print(f"[warn] tof_px {x},{y} is not on the floor: using the floor median")
    if d_floor <= b:
        raise RuntimeError("inconsistent depth map: the floor comes out beyond the vanishing line")
    a = (d_floor - b) * tof_distance_mm

    # Reference band: the edge band closest to the camera (stays uncovered the longest)
    bands = _edge_bands(d.shape)
    edge = max(bands, key=lambda name: np.median(d[bands[name]]))
    band = bands[edge]
    d_band = float(np.median(d[band]))
    band_mm = a / (d_band - b)
    spread = [(d_floor - s) * tof_distance_mm / (d_band - s) for s in shifts]
    if not 0 < band_mm < 0.9 * tof_distance_mm:
        raise RuntimeError(f"reference band at {band_mm:.0f} mm: not plausible with the floor "
                           f"at {tof_distance_mm} mm")

    full_mm = CFG["full_dist_mm"] or band_mm
    CFG["rim_dist_mm"] = round(band_mm, 1)
    CFG["bin_depth_mm"] = round(tof_distance_mm - full_mm, 1)

    bin_mask = cv2.erode(floor.astype(np.uint8) * 255, np.ones((9, 9), np.uint8))
    cv2.imwrite(CFG["bin_mask"], bin_mask)
    cv2.imwrite(CFG["rim_mask"], band.astype(np.uint8) * 255)

    ys, xs = np.nonzero(floor)
    info = {
        "edge": edge,
        "band_mm": round(band_mm, 1),
        "band_mm_range": [round(min(spread), 1), round(max(spread), 1)],
        "floor_mm": int(tof_distance_mm),
        "floor_px": [int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())],
        "shift_rows": b_rows, "shift_cols": b_cols,
        "wall_fit_rows": fit_rows, "wall_fit_cols": fit_cols,
    }
    if max(spread) - min(spread) > 0.2 * band_mm:
        print("[warn] the two pairs of walls disagree: the camera is tilted or off-centre "
              f"(band between {min(spread):.0f} and {max(spread):.0f} mm); straighten it and recalibrate")
    return info, bin_mask > 127, band

def save_overlay(img, d, floor, band):
    """Photo with the floor (green), the reference band (red) and the ToF spot, next to the depth map."""
    over = img.copy()
    over[floor] = (0.55 * over[floor] + 0.45 * np.array([60, 190, 60])).astype(np.uint8)
    over[band] = (0.35 * over[band] + 0.65 * np.array([40, 40, 230])).astype(np.uint8)
    cv2.circle(over, tuple(int(v) for v in tof_spot(d.shape)), 6, (255, 255, 255), 2)
    depth = cv2.applyColorMap(cv2.normalize(d, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8),
                              cv2.COLORMAP_INFERNO)
    cv2.imwrite(CFG["calib_overlay"], np.hstack([over, depth]))

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
    load_geometry()
    t0 = time.time()
    d = infer(img)
    infer_ms = (time.time() - t0) * 1000
    rim = load_mask(CFG["rim_mask"], d.shape)
    binm = load_mask(CFG["bin_mask"], d.shape)
    empty_z = np.load(CFG["empty_depth"])
    if empty_z.shape != d.shape:                                 # camera resolution changed
        empty_z = cv2.resize(empty_z, (d.shape[1], d.shape[0]), interpolation=cv2.INTER_LINEAR)
    fb = load_fallback()
    # Many anchors (walls and uncovered floor as in the calibration photo) + the ToF; the model
    # rescales its relative depth on every photo, two anchors alone are not stable enough
    same = unchanged_pixels(img, binm)
    anchors = float(same.mean()) if same is not None else 0.0
    z = None
    if anchors >= CFG["anchor_min"]:
        z, ab, tof_used = to_metric_anchored(d, same, empty_z, tof_distance_mm)
        if ab[0] <= 0:                                           # nonsense slope: fall back
            z = None
    if z is None:
        z, ab, tof_used = to_metric(d, rim, tof_distance_mm, fb)
        anchors = 0.0

    fill_h = np.clip(empty_z - z, 0, CFG["bin_depth_mm"])[binm]
    raw_pct = 100 * float(np.mean(fill_h)) / CFG["bin_depth_mm"]     # volume: mean height over the floor
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
        "tof_used": tof_used,                     # False = rim-only fallback fit
        "inference_ms": round(infer_ms),
        "anchors_pct": round(100 * anchors, 1),  # 0 = two-anchor fallback (band + ToF)
    }

# ---------------------------------------------------------------- modes
def snapshot(img=None):
    cv2.imwrite(CFG["calib_frame"], capture() if img is None else img)
    if CFG["auto_calibration"]:
        print(f"Saved {CFG['calib_frame']} – masks are not needed: calibrate (bin EMPTY) finds them.")
    else:
        print(f"Saved {CFG['calib_frame']} – paint bin_mask.png and rim_mask.png on it (white = area).")

def calibrate(img=None, tof_distance_mm=None):
    img = capture() if img is None else img
    if tof_distance_mm is None:
        tof_distance_mm = tof_mm()
    d = infer(img)
    calib = {}
    if CFG["auto_calibration"]:
        cv2.imwrite(CFG["calib_frame"], img)
        empty = np.zeros(d.shape, bool)
        save_overlay(img, d, empty, empty)              # kept if the calibration fails
        info, floor_mask, band = auto_calibrate(d, tof_distance_mm)
        save_overlay(img, d, floor_mask, band)
        calib = {"rim_dist_mm": CFG["rim_dist_mm"], "bin_depth_mm": CFG["bin_depth_mm"], "auto": info}
        print(f"Auto-calibration: floor at {tof_distance_mm} mm (ToF), reference band on the "
              f"{info['edge']} edge at {info['band_mm']:.0f} mm "
              f"(estimates {info['band_mm_range'][0]:.0f}-{info['band_mm_range'][1]:.0f} mm), "
              f"full = {CFG['bin_depth_mm']:.0f} mm above the floor; check {CFG['calib_overlay']}")
    rim = load_mask(CFG["rim_mask"], d.shape)
    z, ab, _ = to_metric(d, rim, tof_distance_mm)
    np.save(CFG["empty_depth"], z)
    json.dump({"a": ab[0], "b": ab[1], **calib}, open(CFG["calib_ab"], "w"), indent=2)
    binm = load_mask(CFG["bin_mask"], d.shape)
    floor = float(np.median(z[binm]))
    expected = tof_distance_mm if CFG["auto_calibration"] else CFG["rim_dist_mm"] + CFG["bin_depth_mm"]
    print(f"Empty-bin floor depth (median): {floor:.0f} mm – expected about {expected:.0f} mm")
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