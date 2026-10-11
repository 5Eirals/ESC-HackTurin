"""Perceived odour index near the bin (SGP30 sensor mounted OUTSIDE the bin).

Reference ("option 4", a single sensor): the air measured in the first minutes after an
emptying, when the bin smells the least. If there has been no emptying yet, the air of the
first minutes after start-up is used for the time being.

Why the raw signals and not the TVOC: the SGP30 internal algorithm slowly recomputes its own
baseline, so an odour lasting hours would be "absorbed" and the TVOC would go back to normal.
The raw H2 and ethanol signals have no such correction and, from the Sensirion datasheet, are
related to the concentration by  ln(c) = constant - s / 512.
So (s_ref - s) / 512 = ln(c / c_ref): how many times the air is more loaded than the reference.

Index = ln-ratios of H2 and ethanol combined (mean or maximum), on the MEDIAN of the window.
Two profiles:
- "normale": 30-minute windows, for the real bin;
- "demo": same thresholds, but 60 s windows, to show the system live in a few minutes
  (e.g. alcohol hand gel next to the cardboard box, sensor outside).
The thresholds are a starting point: they must be tuned with tests (e.g. "do you smell it? yes/no").

Known limit: the raw signals also depend on temperature and humidity; a big change in the
weather since the reference was taken can shift the index.
"""

import json
import math
import os
import statistics
import time
from collections import deque
from pathlib import Path

RAW_SCALE = 512.0           # SGP30 datasheet constant for the raw signals
LEVELS = ("green", "yellow", "red")

PROFILES = {
    "normale": {
        "window_s": 30 * 60,          # median window
        "ref_window_s": 30 * 60,      # how long the reference is collected after an emptying
        "ref_min_samples": 300,       # minimum samples to set the reference (5 min at 1 Hz)
        "min_window_fraction": 0.3,   # at least this share of the window is needed to judge
        "eval_period_s": 60,          # how often the level is recomputed
        "yellow_ratio": 2.0,          # noticeable odour (times the reference)
        "red_ratio": 5.0,             # stench: intervention
        "red_hot_ratio": 3.0,         # stench when it is hot
        "hot_c": 25.0,
        "min_persistence": 0.6,       # share of the window samples above the yellow threshold
        "hysteresis_ln": 0.15,        # to go down a level it must stay a bit below the threshold
        "combine": "mean",            # mean of H2 and ethanol: more robust to noise
        "reuse_saved_reference": True,
    },
    "demo": {
        "window_s": 60,
        "ref_window_s": 60,
        "ref_min_samples": 30,
        "min_window_fraction": 0.5,
        "eval_period_s": 5,
        "yellow_ratio": 2.0,          # same thresholds as the normal profile: only the times change
        "red_ratio": 5.0,
        "red_hot_ratio": 3.0,
        "hot_c": 25.0,
        "min_persistence": 0.5,
        "hysteresis_ln": 0.08,
        "combine": "max",             # alcohol mostly raises ethanol: the highest channel counts
        "reuse_saved_reference": False,  # in the demo the reference is always measured from scratch
    },
}


def raw_to_ln(raw: float) -> float:
    """ln of the concentration (up to a constant) from the raw SGP30 signal."""
    return -raw / RAW_SCALE


def format_duration(seconds: float) -> str:
    return f"{round(seconds / 60)} min" if seconds >= 120 else f"{round(seconds)} s"


class OdorMonitor:
    def __init__(self, reference_path: Path, profile: str = "normale"):
        self.profile = profile
        self.p = PROFILES[profile]
        self.yellow_ln = math.log(self.p["yellow_ratio"])
        self.red_ln = math.log(self.p["red_ratio"])
        self.red_hot_ln = math.log(self.p["red_hot_ratio"])

        self.reference_path = Path(reference_path)
        self.samples = deque()      # (ts, ln_h2, ln_ethanol, temp_c)
        self.reference = self._load_reference() if self.p["reuse_saved_reference"] else None
        self.collecting = None      # {"source", "start", "h2": [], "eth": []}
        self.level = None
        self.last_eval = None
        self.last_status = None
        if self.reference is None:
            self.start_reference("avvio")

    # --- Reference ---------------------------------------------------------------------------
    def start_reference(self, source: str):
        """Starts collecting a new reference (bin just emptied).
        The window starts from the first sample received from now on."""
        self.collecting = {"source": source, "start": None, "h2": [], "eth": []}

    def _load_reference(self):
        try:
            data = json.loads(self.reference_path.read_text())
            return {k: data[k] for k in ("h2", "eth", "ts", "source")}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_reference(self) -> bool:
        try:
            self.reference_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.reference_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.reference))
            os.replace(tmp, self.reference_path)
            return True
        except OSError:
            return False

    def instant_ratios(self, h2_raw: float, ethanol_raw: float):
        """How many times H2 and ethanol exceed the reference air at this instant (not averaged).
        Used to read the sensor live: the raw value goes the other way (lower = more gas)."""
        ref = self.reference
        if ref is None:
            return None, None
        return (round(math.exp(raw_to_ln(h2_raw) - ref["h2"]), 2),
                round(math.exp(raw_to_ln(ethanol_raw) - ref["eth"]), 2))

    def _combine(self, d_h2: float, d_eth: float) -> float:
        return max(d_h2, d_eth) if self.p["combine"] == "max" else (d_h2 + d_eth) / 2

    # --- Samples -----------------------------------------------------------------------------
    def add_sample(self, ts: float, h2_raw: float, ethanol_raw: float, temp_c: float | None):
        """Adds a valid sample (1 Hz). Returns the list of events to report:
        ("reference", status) when a new reference is ready,
        ("level", old_level, new_level, status) when the odour level changes."""
        ln_h2, ln_eth = raw_to_ln(h2_raw), raw_to_ln(ethanol_raw)
        self.samples.append((ts, ln_h2, ln_eth, temp_c))
        while self.samples and self.samples[0][0] < ts - self.p["window_s"]:
            self.samples.popleft()

        events = []
        if self.collecting is not None:
            c = self.collecting
            if c["start"] is None:
                c["start"] = ts
            c["h2"].append(ln_h2)
            c["eth"].append(ln_eth)
            if ts - c["start"] >= self.p["ref_window_s"] and len(c["h2"]) >= self.p["ref_min_samples"]:
                self.reference = {"h2": statistics.median(c["h2"]), "eth": statistics.median(c["eth"]),
                                  "ts": ts, "source": c["source"]}
                self.collecting = None
                self._save_reference()
                self.last_eval = None   # recompute right away with the new reference
                events.append(("reference", self.status(ts)))

        if self.last_eval is None or ts - self.last_eval >= self.p["eval_period_s"]:
            self.last_eval = ts
            old = self.level
            self.level = self._evaluate(ts)
            if self.level is not None and self.level != (old or "green"):
                events.append(("level", old or "green", self.level, self.last_status))
        return events

    # --- Evaluation --------------------------------------------------------------------------
    def _evaluate(self, now: float):
        self.last_status = self.status(now)
        s = self.last_status
        if s["state"] != "ok":
            return self.level   # no judgement: the previous level stays
        delta, persistence, temp = s["delta"], s["persistence"], s["temperature_c"]

        def level_for(margin, need_persistence):
            persistent = persistence >= self.p["min_persistence"] or not need_persistence
            hot = temp is not None and temp >= self.p["hot_c"]
            if persistent and (delta >= self.red_ln - margin or (hot and delta >= self.red_hot_ln - margin)):
                return "red"
            if persistent and delta >= self.yellow_ln - margin:
                return "yellow"
            return "green"

        # Going up needs the median above the threshold AND persistence; going down needs the
        # median below the threshold minus the hysteresis (persistence alone is not enough).
        new = level_for(0.0, need_persistence=True)
        if self.level in LEVELS and LEVELS.index(new) < LEVELS.index(self.level):
            hold = level_for(self.p["hysteresis_ln"], need_persistence=False)
            new = min(max(new, hold, key=LEVELS.index), self.level, key=LEVELS.index)
        s["level"] = new
        return new

    def status(self, now: float | None = None) -> dict:
        """State for the dashboard and for the event messages."""
        now = now if now is not None else time.time()
        ref = self.reference
        p = self.p
        out = {
            "state": "ok",
            "level": self.level,
            "delta": None, "ratio": None, "delta_h2": None, "delta_ethanol": None,
            "persistence": None, "temperature_c": None,
            "profile": self.profile,
            "window_s": p["window_s"],
            "window_label": format_duration(p["window_s"]),
            "reference": None if ref is None else {
                "source": ref["source"], "ts": ref["ts"], "age_h": round((now - ref["ts"]) / 3600, 1)},
            "collecting": None if self.collecting is None else {
                "source": self.collecting["source"],
                "progress": 0.0 if self.collecting["start"] is None else
                round(min(1.0, (now - self.collecting["start"]) / p["ref_window_s"]), 2)},
            "thresholds": {"yellow_ratio": p["yellow_ratio"], "red_ratio": p["red_ratio"],
                           "red_hot_ratio": p["red_hot_ratio"], "hot_c": p["hot_c"],
                           "min_persistence": p["min_persistence"]},
        }
        if ref is None:
            out["state"] = "collecting"
            return out
        if len(self.samples) < p["window_s"] * p["min_window_fraction"]:
            out["state"] = "insufficient"
            return out

        d_h2 = [h - ref["h2"] for _, h, _, _ in self.samples]
        d_eth = [e - ref["eth"] for _, _, e, _ in self.samples]
        delta_h2 = statistics.median(d_h2)
        delta_eth = statistics.median(d_eth)
        delta = self._combine(delta_h2, delta_eth)
        above = sum(1 for a, b in zip(d_h2, d_eth) if self._combine(a, b) >= self.yellow_ln)
        temps = [t for *_, t in self.samples if t is not None]
        out.update({
            "delta": round(delta, 3),
            "ratio": round(math.exp(delta), 2),
            "delta_h2": round(delta_h2, 3),
            "delta_ethanol": round(delta_eth, 3),
            "persistence": round(above / len(self.samples), 2),
            "temperature_c": round(statistics.median(temps), 1) if temps else None,
        })
        return out
