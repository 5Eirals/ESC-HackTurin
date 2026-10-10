"""Storico del livello di riempimento e previsione di quando il cassonetto sara' pieno.

Ogni foto analizzata (depth_fill.analyze) aggiunge una riga a data/fill_log.csv:
    timestamp, fill_pct, peak_pct, event ("emptied" se il cassonetto e' stato svuotato), source

Previsione (ex modalita' "predict", ora eseguita dopo ogni misura di "run"):
- con almeno PROFILE_MIN_DAYS giorni di dati: PROFILO SETTIMANALE. Per ciascuna delle 168 ore
  della settimana si calcola il ritmo medio di riempimento (% all'ora) sugli ultimi 30 giorni,
  cosi' la previsione sa che di notte il cassonetto si riempie poco e nel weekend di piu'.
  Il profilo viene scalato sul ciclo in corso (se questa settimana si riempie piu' in fretta
  del solito, la previsione si adegua) e proiettato in avanti dal livello attuale fino al 100%.
- altrimenti: RETTA sui dati dall'ultimo svuotamento (il vecchio predict()).

Solo libreria standard: funziona anche se onnxruntime/opencv non sono installati.
"""

import csv
import os
import time
from datetime import datetime
from pathlib import Path

FIELDS = ["timestamp", "fill_pct", "peak_pct", "event", "source"]
TS_FMT = "%Y-%m-%d %H:%M:%S"

WINDOW_DAYS = 30          # dati tenuti in memoria e usati per profilo e statistiche
PROFILE_MIN_DAYS = 7      # sotto questa durata di storico si usa la retta
FULL_PCT = 100.0          # livello "pieno" per la previsione
NEAR_FULL_PCT = 95.0      # un ciclo che arriva qui conta come "pieno prima della raccolta"
MAX_GAP_H = 2.0           # coppie di misure piu' distanti non entrano nel profilo
STEP_S = 900              # passo della proiezione (15 min)
HORIZON_H = 30 * 24       # oltre questo orizzonte la previsione dice "non si riempie"
TREND_MIN_H = 6           # durata minima del ciclo in corso per scalare il profilo
TREND_CLAMP = (0.5, 2.0)  # limiti del fattore di scala sul ciclo in corso


def hour_of_week(ts: float) -> int:
    d = datetime.fromtimestamp(ts)
    return d.weekday() * 24 + d.hour


def linear_fit(xs, ys):
    """Minimi quadrati y = m*x + q. Ritorna (m, q)."""
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

    # ------------------------------------------------------------------ file
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
                        continue  # riga corrotta: si salta
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

    # ------------------------------------------------------------------ analisi
    @property
    def last(self) -> dict | None:
        return self.rows[-1] if self.rows else None

    def current_cycle(self) -> list[dict]:
        """Misure dall'ultimo svuotamento (compreso) a oggi."""
        start = max((i for i, r in enumerate(self.rows) if r["event"] == "emptied"), default=0)
        return self.rows[start:]

    def span_days(self) -> float:
        return (self.rows[-1]["ts"] - self.rows[0]["ts"]) / 86400 if len(self.rows) > 1 else 0.0

    def weekly_profile(self) -> list[float] | None:
        """Ritmo medio di riempimento (% all'ora) per ciascuna delle 168 ore della settimana."""
        dy = [0.0] * 168
        dt = [0.0] * 168
        for a, b in zip(self.rows, self.rows[1:]):
            hours = (b["ts"] - a["ts"]) / 3600
            if hours <= 0 or hours > MAX_GAP_H or b["event"] == "emptied":
                continue
            if a["fill_pct"] >= NEAR_FULL_PCT or b["fill_pct"] >= NEAR_FULL_PCT:
                continue  # cassonetto pieno: non si vede piu' salire, falserebbe il ritmo
            h = hour_of_week((a["ts"] + b["ts"]) / 2)
            dy[h] += b["fill_pct"] - a["fill_pct"]
            dt[h] += hours
        total_dt = sum(dt)
        if total_dt == 0:
            return None
        overall = sum(dy) / total_dt
        # Il rumore della misura si compensa sommando prima di dividere; le ore senza dati
        # prendono il ritmo medio generale.
        return [max(0.0, dy[h] / dt[h]) if dt[h] > 0 else max(0.0, overall) for h in range(168)]

    def _project(self, profile, start_ts, level, scale):
        """Avanza a passi di STEP_S finche' il livello arriva al 100%. Ritorna il timestamp."""
        t = start_ts
        end = start_ts + HORIZON_H * 3600
        while t < end:
            rate = profile[hour_of_week(t)] * scale  # % all'ora
            step = rate * STEP_S / 3600
            if level + step >= FULL_PCT:
                return t + (FULL_PCT - level) / rate * 3600 if rate > 0 else t
            level += step
            t += STEP_S
        return None

    def predict(self, now: float | None = None) -> dict | None:
        """Quando sara' pieno il cassonetto. None se non ci sono dati sufficienti."""
        if not self.rows:
            return None
        now = time.time() if now is None else now
        last = self.rows[-1]
        level = last["fill_pct"]
        cycle = self.current_cycle()
        result = {"level_pct": level, "measured_at": last["ts"]}

        profile = self.weekly_profile() if self.span_days() >= PROFILE_MIN_DAYS else None
        if profile is not None:
            # Scala il profilo sul ciclo in corso: salita osservata / salita attesa dal profilo.
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
            m, _ = linear_fit(xs, [r["fill_pct"] for r in cycle])  # % all'ora
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
        """Statistiche sugli ultimi 30 giorni, utili per pianificare la raccolta."""
        emptied = [i for i, r in enumerate(self.rows) if r["event"] == "emptied"]
        before = [self.rows[i - 1]["fill_pct"] for i in emptied if i > 0]
        # Cicli completi (da uno svuotamento al successivo) che hanno raggiunto il pieno.
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
        """Serie per il grafico: l'ultima misura di ogni ora, piu' le due misure a cavallo di
        ogni svuotamento (cosi' il picco prima della raccolta resta visibile).
        Ritorna [[ts, fill_pct, peak_pct, emptied], ...]."""
        rows = self.rows
        out = []
        for i, r in enumerate(rows):
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            keep = (nxt is None or int(nxt["ts"] // bucket_s) != int(r["ts"] // bucket_s)
                    or r["event"] == "emptied" or nxt["event"] == "emptied")
            if keep:
                out.append([round(r["ts"]), r["fill_pct"], r["peak_pct"], r["event"] == "emptied"])
        return out
