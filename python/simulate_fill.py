"""Generates a month of INVENTED fill measurements (data/fill_log.csv) to try the forecast
and the dashboard without waiting for 30 days of real photos.

Model of the simulated bin:
- collection on Monday, Wednesday and Friday at 06:00 (the level drops to a few %);
- it fills more by day than by night, and most in the evening (people back from work);
- at the weekend it fills 40% more: from Friday to Monday it sometimes overflows;
- every day has a random factor (+-15%) and every measurement has the noise of the depth
  estimate, filtered as in depth_fill.FillTracker (median of the last 3).
The rows have source = "sim", so the dashboard can show that the data is simulated.

Usage:  python simulate_fill.py [path.csv]      (default ../data/fill_log.csv)
"""

import csv
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from fill_history import FIELDS, TS_FMT

# % per hour for each hour of the day (weekday)
HOURLY_RATE = [0.15] * 6 + [0.4, 1.6, 1.6, 1.1, 1.1, 1.1, 1.6, 1.6,
                            1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 0.8, 0.3]
WEEKEND_FACTOR = 1.4
COLLECTION_DAYS = (0, 2, 4)   # Monday, Wednesday, Friday
COLLECTION_HOUR = 6
NOISE_PCT = 1.5               # noise of the depth estimate (standard deviation)


def generate(path, days: int = 30, end: float | None = None, interval_s: int = 900,
             seed: int | None = 7) -> int:
    """Writes `days` days of simulated measurements ending at `end` (default: now).
    Returns the number of rows written."""
    rng = random.Random(seed)
    end = time.time() if end is None else end
    t = end - days * 86400
    level, heap = rng.uniform(5, 15), rng.uniform(4, 10)
    day_factor, current_day = 1.0, None
    window, collected_today = [], None
    rows = []

    while t <= end:
        d = datetime.fromtimestamp(t)
        if d.date() != current_day:
            current_day = d.date()
            day_factor = rng.lognormvariate(0, 0.15)

        event = ""
        if (d.weekday() in COLLECTION_DAYS and d.hour >= COLLECTION_HOUR
                and collected_today != d.date()):
            collected_today = d.date()
            level, heap = rng.uniform(1, 4), rng.uniform(3, 6)
            window = []
            event = "emptied"
        else:
            rate = HOURLY_RATE[d.hour] * day_factor
            if d.weekday() >= 5:
                rate *= WEEKEND_FACTOR
            level = min(100.0, level + rate * interval_s / 3600 * rng.uniform(0.3, 1.7))
            heap = min(15.0, max(2.0, heap + rng.gauss(0, 0.5)))  # heaps above the average level

        measured = min(100.0, max(0.0, level + rng.gauss(0, NOISE_PCT)))
        window.append(measured)
        smooth = sorted(window[-3:])[len(window[-3:]) // 2]       # median of the last 3
        peak = min(100.0, smooth + heap)
        rows.append([time.strftime(TS_FMT, time.localtime(t)), round(smooth, 1),
                     round(peak, 1), event, "sim"])
        t += interval_s

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)
    os.replace(tmp, path)
    return len(rows)


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else here.parent / "data" / "fill_log.csv"
    n = generate(out)
    print(f"Scritte {n} misure simulate in {out}")
