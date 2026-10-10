"""Genera un mese di misure di riempimento INVENTATE (data/fill_log.csv) per provare la
previsione e la dashboard senza aspettare 30 giorni di foto vere.

Modello del cassonetto simulato:
- raccolta lunedi', mercoledi' e venerdi' alle 06:00 (il livello torna a pochi %);
- di giorno si riempie piu' che di notte, la sera di piu' (rientro dal lavoro);
- nel weekend si riempie il 40% in piu': dal venerdi' al lunedi' a volte trabocca;
- ogni giorno ha un fattore casuale (+-15%) e ogni misura ha il rumore della stima di
  profondita', filtrato come in depth_fill.FillTracker (mediana delle ultime 3).
Le righe hanno source = "sim", cosi' la dashboard puo' segnalare che i dati sono simulati.

Uso:  python simulate_fill.py [percorso.csv]      (default ../data/fill_log.csv)
"""

import csv
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from fill_history import FIELDS, TS_FMT

# % all'ora per ciascuna ora del giorno (feriale)
HOURLY_RATE = [0.15] * 6 + [0.4, 1.6, 1.6, 1.1, 1.1, 1.1, 1.6, 1.6,
                            1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 0.8, 0.3]
WEEKEND_FACTOR = 1.4
COLLECTION_DAYS = (0, 2, 4)   # lunedi', mercoledi', venerdi'
COLLECTION_HOUR = 6
NOISE_PCT = 1.5               # rumore della stima di profondita' (deviazione standard)


def generate(path, days: int = 30, end: float | None = None, interval_s: int = 900,
             seed: int | None = 7) -> int:
    """Scrive `days` giorni di misure simulate che finiscono a `end` (default: adesso).
    Ritorna il numero di righe scritte."""
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
            heap = min(15.0, max(2.0, heap + rng.gauss(0, 0.5)))  # mucchi sopra il livello medio

        measured = min(100.0, max(0.0, level + rng.gauss(0, NOISE_PCT)))
        window.append(measured)
        smooth = sorted(window[-3:])[len(window[-3:]) // 2]       # mediana delle ultime 3
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
