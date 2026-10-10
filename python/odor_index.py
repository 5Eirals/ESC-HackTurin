"""Indice di odore percepito vicino al cassonetto (sensore SGP30 montato FUORI dal cassonetto).

Riferimento ("opzione 4", un solo sensore): l'aria misurata nei primi minuti dopo uno
svuotamento, quando il cassonetto e' al minimo della puzza. Se non c'e' ancora nessuno
svuotamento, si usa provvisoriamente l'aria dei primi minuti dopo l'avvio.

Perche' i segnali grezzi e non il TVOC: l'algoritmo interno dell'SGP30 ricalcola lentamente
il proprio valore di base, quindi un odore che dura ore verrebbe "assorbito" e il TVOC
tornerebbe normale. I segnali grezzi H2 ed etanolo non hanno questa correzione e, dal
datasheet Sensirion, sono legati alla concentrazione da  ln(c) = costante - s / 512.
Quindi (s_rif - s) / 512 = ln(c / c_rif): di quante volte l'aria e' piu' carica del riferimento.

Indice = media dei due ln-rapporti (H2, etanolo), calcolata sulla MEDIANA degli ultimi 30 minuti.
Le soglie sono un punto di partenza: vanno tarate con le prove (es. "senti odore? si'/no").

Limite noto: i segnali grezzi dipendono anche da temperatura e umidita'; un forte cambio di
meteo rispetto al momento del riferimento puo' spostare l'indice.
"""

import json
import math
import os
import statistics
import time
from collections import deque
from pathlib import Path

WINDOW_S = 30 * 60          # finestra della mediana
REF_WINDOW_S = 30 * 60      # durata della raccolta del riferimento dopo uno svuotamento
REF_MIN_SAMPLES = 300       # campioni minimi per fissare il riferimento (5 min a 1 Hz)
MIN_WINDOW_FRACTION = 0.3   # serve almeno il 30% della finestra per giudicare
EVAL_PERIOD_S = 60          # ogni quanto si ricalcola il livello
RAW_SCALE = 512.0           # costante del datasheet SGP30 per i segnali grezzi

# Soglie in unita' ln (ln 2 = "il doppio del riferimento"), da tarare
YELLOW_LN = math.log(2)     # odore percepibile
RED_LN = math.log(5)        # puzza: intervento
RED_HOT_LN = math.log(3)    # puzza con il caldo
HOT_C = 25.0
MIN_PERSISTENCE = 0.6       # quota dei campioni della finestra sopra la soglia gialla
HYSTERESIS_LN = 0.15        # per scendere di livello bisogna stare un po' sotto la soglia

LEVELS = ("green", "yellow", "red")


def raw_to_ln(raw: float) -> float:
    """ln della concentrazione (a meno di una costante) dal segnale grezzo SGP30."""
    return -raw / RAW_SCALE


class OdorMonitor:
    def __init__(self, reference_path: Path):
        self.reference_path = Path(reference_path)
        self.samples = deque()      # (ts, ln_h2, ln_ethanol, temp_c)
        self.reference = self._load_reference()
        self.collecting = None      # {"source", "start", "h2": [], "eth": []}
        self.level = None
        self.last_eval = None
        self.last_status = None
        if self.reference is None:
            self.start_reference("avvio")

    # --- riferimento ----------------------------------------------------------------------
    def start_reference(self, source: str):
        """Inizia a raccogliere un nuovo riferimento (cassonetto appena svuotato).
        La finestra parte dal primo campione ricevuto da qui in poi."""
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

    # --- campioni -------------------------------------------------------------------------
    def add_sample(self, ts: float, h2_raw: float, ethanol_raw: float, temp_c: float | None):
        """Aggiunge un campione valido (1 Hz). Ritorna la lista degli eventi da segnalare:
        ("reference", status) quando un nuovo riferimento e' pronto,
        ("level", old_level, new_level, status) quando cambia il livello di odore."""
        ln_h2, ln_eth = raw_to_ln(h2_raw), raw_to_ln(ethanol_raw)
        self.samples.append((ts, ln_h2, ln_eth, temp_c))
        while self.samples and self.samples[0][0] < ts - WINDOW_S:
            self.samples.popleft()

        events = []
        if self.collecting is not None:
            c = self.collecting
            if c["start"] is None:
                c["start"] = ts
            c["h2"].append(ln_h2)
            c["eth"].append(ln_eth)
            if ts - c["start"] >= REF_WINDOW_S and len(c["h2"]) >= REF_MIN_SAMPLES:
                self.reference = {"h2": statistics.median(c["h2"]), "eth": statistics.median(c["eth"]),
                                  "ts": ts, "source": c["source"]}
                self.collecting = None
                self._save_reference()
                self.last_eval = None   # ricalcola subito con il nuovo riferimento
                events.append(("reference", self.status(ts)))

        if self.last_eval is None or ts - self.last_eval >= EVAL_PERIOD_S:
            self.last_eval = ts
            old = self.level
            self.level = self._evaluate(ts)
            if self.level is not None and self.level != (old or "green"):
                events.append(("level", old or "green", self.level, self.last_status))
        return events

    # --- valutazione ----------------------------------------------------------------------
    def _evaluate(self, now: float):
        self.last_status = self.status(now)
        s = self.last_status
        if s["state"] != "ok":
            return self.level   # niente giudizio: resta il livello precedente
        delta, persistence, temp = s["delta"], s["persistence"], s["temperature_c"]

        def level_for(margin, need_persistence):
            persistent = persistence >= MIN_PERSISTENCE or not need_persistence
            hot = temp is not None and temp >= HOT_C
            if persistent and (delta >= RED_LN - margin or (hot and delta >= RED_HOT_LN - margin)):
                return "red"
            if persistent and delta >= YELLOW_LN - margin:
                return "yellow"
            return "green"

        # Per salire servono mediana sopra soglia E persistenza; per scendere la mediana deve
        # scendere sotto la soglia meno l'isteresi (la persistenza da sola non basta).
        new = level_for(0.0, need_persistence=True)
        if self.level in LEVELS and LEVELS.index(new) < LEVELS.index(self.level):
            hold = level_for(HYSTERESIS_LN, need_persistence=False)
            new = min(max(new, hold, key=LEVELS.index), self.level, key=LEVELS.index)
        s["level"] = new
        return new

    def status(self, now: float | None = None) -> dict:
        """Stato per la dashboard e per i messaggi degli eventi."""
        now = now if now is not None else time.time()
        ref = self.reference
        out = {
            "state": "ok",
            "level": self.level,
            "delta": None, "ratio": None, "delta_h2": None, "delta_ethanol": None,
            "persistence": None, "temperature_c": None,
            "window_min": WINDOW_S // 60,
            "reference": None if ref is None else {
                "source": ref["source"], "ts": ref["ts"], "age_h": round((now - ref["ts"]) / 3600, 1)},
            "collecting": None if self.collecting is None else {
                "source": self.collecting["source"],
                "progress": 0.0 if self.collecting["start"] is None else
                round(min(1.0, (now - self.collecting["start"]) / REF_WINDOW_S), 2)},
            "thresholds": {"yellow_ratio": 2, "red_ratio": 5, "red_hot_ratio": 3, "hot_c": HOT_C,
                           "min_persistence": MIN_PERSISTENCE},
        }
        if ref is None:
            out["state"] = "collecting"
            return out
        if len(self.samples) < WINDOW_S * MIN_WINDOW_FRACTION:
            out["state"] = "insufficient"
            return out

        d_h2 = [h - ref["h2"] for _, h, _, _ in self.samples]
        d_eth = [e - ref["eth"] for _, _, e, _ in self.samples]
        delta_h2 = statistics.median(d_h2)
        delta_eth = statistics.median(d_eth)
        delta = (delta_h2 + delta_eth) / 2
        above = sum(1 for a, b in zip(d_h2, d_eth) if (a + b) / 2 >= YELLOW_LN)
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
