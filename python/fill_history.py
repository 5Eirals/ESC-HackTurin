"""Fill level history and forecast of when the bin will be full.

Every analysed photo (depth_fill.analyze) adds a row to data/fill_log.csv:
    timestamp, fill_pct, peak_pct, event ("emptied" if the bin was emptied), source

Forecast (formerly the "predict" mode, now run after every "run" measurement):
- with at least PROFILE_MIN_DAYS days of data: WEEKLY PROFILE. For each of the 168 hours of the
  week the average fill rate (% per hour) over the last 30 days is computed, so the forecast
  knows that the bin fills little at night and more at the weekend.
  The profile is scaled on the current cycle (if this week it fills faster than usual, the
  forecast adapts) and projected forward from the current level up to 100%.
- otherwise: STRAIGHT LINE on the data since the last emptying (the old predict()).

Standard library only: works even if onnxruntime/opencv are not installed.
"""

import csv
import os
import time
from datetime import datetime
from pathlib import Path

FIELDS = ["timestamp", "fill_pct", "peak_pct", "event", "source"]
TS_FMT = "%Y-%m-%d %H:%M:%S"

WINDOW_DAYS = 30          # data kept in memory and used for profile and statistics
PROFILE_MIN_DAYS = 7      # with less history than this the straight line is used
FULL_PCT = 100.0          # "full" level for the forecast
NEAR_FULL_PCT = 95.0      # a cycle reaching this counts as "full before the collection"
MAX_GAP_H = 2.0           # pairs of measurements further apart are left out of the profile
STEP_S = 900              # projection step (15 min)
HORIZON_H = 30 * 24       # beyond this horizon the forecast says "does not fill up"
TREND_MIN_H = 6           # minimum length of the current cycle to scale the profile
TREND_CLAMP = (0.5, 2.0)  # limits of the scale factor on the current cycle


def hour_of_week(ts: float) -> int:
    d = datetime.fromtimestamp(ts)
    return d.weekday() * 24 + d.hour


def linear_fit(xs, ys):
    """Least squares y = m*x + q. Returns (m, q)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    m = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return m, my - m * mx


class FillHistory:
    def __init__(self, path, window_days: int = WINDOW_DAYS):
        self.path = Path(path)
        self.window_s = window_days * 86400
        self.rows: list[dict] = []
        self.load()

    # --- File --------------------------------------------------------------------------------
    def load(self):
        self.rows = []
        try:
            with open(self.path, newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        self.rows.append({
                            "ts": time.mktime(time.strptime(r["timestamp"], TS_FMT)),
                            "fill_pct": float(r["fill_pct"]),
                            "peak_pct": float(r["peak_pct"]),
                            "event": r.get("event") or "",
                            "source": r.get("source") or "camera",
                        })
                    except (KeyError, ValueError, TypeError):
                        continue  # corrupted row: skipped
        except OSError:
            pass
        self.rows.sort(key=lambda r: r["ts"])
        self._trim()

    def append(self, ts: float, fill_pct: float, peak_pct: float, event: str = "",
               source: str = "camera") -> bool:
        row = {"ts": ts, "fill_pct": float(fill_pct), "peak_pct": float(peak_pct),
               "event": event, "source": source}
        self.rows.append(row)
        self._trim()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            with open(self.path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(FIELDS)
                w.writerow([time.strftime(TS_FMT, time.localtime(ts)), row["fill_pct"],
                            row["peak_pct"], event, source])
            return True
        except OSError:
            return False

    def _trim(self):
        if self.rows:
            cutoff = self.rows[-1]["ts"] - self.window_s
            self.rows = [r for r in self.rows if r["ts"] >= cutoff]

    # --- Analysis ----------------------------------------------------------------------------
    @property
    def last(self) -> dict | None:
        return self.rows[-1] if self.rows else None

    def current_cycle(self) -> list[dict]:
        """Measurements from the last emptying (included) to now."""
        start = max((i for i, r in enumerate(self.rows) if r["event"] == "emptied"), default=0)
        return self.rows[start:]

    def span_days(self) -> float:
        return (self.rows[-1]["ts"] - self.rows[0]["ts"]) / 86400 if len(self.rows) > 1 else 0.0

    def weekly_profile(self) -> list[float] | None:
        """Average fill rate (% per hour) for each of the 168 hours of the week."""
        dy = [0.0] * 168
        dt = [0.0] * 168
        for a, b in zip(self.rows, self.rows[1:]):
            hours = (b["ts"] - a["ts"]) / 3600
            if hours <= 0 or hours > MAX_GAP_H or b["event"] == "emptied":
                continue
            if a["fill_pct"] >= NEAR_FULL_PCT or b["fill_pct"] >= NEAR_FULL_PCT:
                continue  # bin full: it can no longer be seen rising, it would distort the rate
            h = hour_of_week((a["ts"] + b["ts"]) / 2)
            dy[h] += b["fill_pct"] - a["fill_pct"]
            dt[h] += hours
        total_dt = sum(dt)
        if total_dt == 0:
            return None
        overall = sum(dy) / total_dt
        # Measurement noise cancels out by summing before dividing; hours without data
        # take the overall average rate.
        return [max(0.0, dy[h] / dt[h]) if dt[h] > 0 else max(0.0, overall) for h in range(168)]

    def _project(self, profile, start_ts, level, scale):
        """Steps forward by STEP_S until the level reaches 100%. Returns the timestamp."""
        t = start_ts
        end = start_ts + HORIZON_H * 3600
        while t < end:
            rate = profile[hour_of_week(t)] * scale  # % per hour
            step = rate * STEP_S / 3600
            if level + step >= FULL_PCT:
                return t + (FULL_PCT - level) / rate * 3600 if rate > 0 else t
            level += step
            t += STEP_S
        return None

    def predict(self, now: float | None = None) -> dict | None:
        """When the bin will be full. None if there is not enough data."""
        if not self.rows:
            return None
        now = time.time() if now is None else now
        last = self.rows[-1]
        level = last["fill_pct"]
        cycle = self.current_cycle()
        result = {"level_pct": level, "measured_at": last["ts"]}

        profile = self.weekly_profile() if self.span_days() >= PROFILE_MIN_DAYS else None
        if profile is not None:
            # Scale the profile on the current cycle: observed rise / rise expected from the profile.
            scale = 1.0
            cycle_h = (last["ts"] - cycle[0]["ts"]) / 3600
            if cycle_h >= TREND_MIN_H:
                expected = sum(profile[hour_of_week(t)] * STEP_S / 3600
                               for t in range(int(cycle[0]["ts"]), int(last["ts"]), STEP_S))
                observed = level - cycle[0]["fill_pct"]
                if expected >= 5:
                    scale = min(max(observed / expected, TREND_CLAMP[0]), TREND_CLAMP[1])
            full_at = last["ts"] if level >= FULL_PCT else self._project(profile, last["ts"], level, scale)
            next_24h = sum(profile[hour_of_week(last["ts"] + h * 3600)] for h in range(24)) * scale
            result.update(method="profile", scale=round(scale, 2), rate_pct_day=round(next_24h, 1))
        else:
            if len(cycle) < 4 or cycle[-1]["ts"] - cycle[0]["ts"] < 2 * 3600:
                return None
            xs = [(r["ts"] - cycle[0]["ts"]) / 3600 for r in cycle]
            m, _ = linear_fit(xs, [r["fill_pct"] for r in cycle])  # % per hour
            if level >= FULL_PCT:
                full_at = last["ts"]
            elif m <= 0:
                full_at = None
            else:
                hours = (FULL_PCT - level) / m
                full_at = last["ts"] + hours * 3600 if hours <= HORIZON_H else None
            result.update(method="linear", scale=1.0, rate_pct_day=round(m * 24, 1))

        result["full_at"] = full_at
        result["hours_left"] = None if full_at is None else round(max(0.0, (full_at - now) / 3600), 1)
        return result

    def stats(self) -> dict:
        """Statistics over the last 30 days, useful to plan the collection."""
        emptied = [i for i, r in enumerate(self.rows) if r["event"] == "emptied"]
        before = [self.rows[i - 1]["fill_pct"] for i in emptied if i > 0]
        # Complete cycles (from one emptying to the next) that reached full.
        bounds = [0] + emptied + [len(self.rows)]
        full_cycles = sum(
            1 for a, b in zip(bounds, bounds[1:])
            if b > a and max(r["fill_pct"] for r in self.rows[a:b]) >= NEAR_FULL_PCT)
        intervals = [(self.rows[b]["ts"] - self.rows[a]["ts"]) / 86400
                     for a, b in zip(emptied, emptied[1:])]
        return {
            "days": round(self.span_days(), 1),
            "measurements": len(self.rows),
            "emptyings": len(emptied),
            "avg_interval_days": round(sum(intervals) / len(intervals), 1) if intervals else None,
            "avg_fill_at_emptying_pct": round(sum(before) / len(before), 1) if before else None,
            "full_before_collection": full_cycles,
            "simulated": any(r["source"] == "sim" for r in self.rows),
        }

    def chart(self, bucket_s: int = 3600) -> list[list]:
        """Series for the chart: the last measurement of every hour, plus the two measurements
        around every emptying (so the peak before the collection stays visible).
        Returns [[ts, fill_pct, peak_pct, emptied], ...]."""
        rows = self.rows
        out = []
        for i, r in enumerate(rows):
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            keep = (nxt is None or int(nxt["ts"] // bucket_s) != int(r["ts"] // bucket_s)
                    or r["event"] == "emptied" or nxt["event"] == "emptied")
            if keep:
                out.append([round(r["ts"]), r["fill_pct"], r["peak_pct"], r["event"] == "emptied"])
        return out
