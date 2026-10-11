"""Indice di odore percepito vicino al cassonetto (sensore SGP30 montato FUORI dal cassonetto).

Riferimento ("opzione 4", un solo sensore): l'aria misurata nei primi minuti dopo uno
svuotamento, quando il cassonetto e' al minimo della puzza. Se non c'e' ancora nessuno
svuotamento, si usa provvisoriamente l'aria dei primi minuti dopo l'avvio.

Perche' i segnali grezzi e non il TVOC: l'algoritmo interno dell'SGP30 ricalcola lentamente
il proprio valore di base, quindi un odore che dura ore verrebbe "assorbito" e il TVOC
tornerebbe normale. I segnali grezzi H2 ed etanolo non hanno questa correzione e, dal
datasheet Sensirion, sono legati alla concentrazione da  ln(c) = costante - s / 512.
Quindi (s_rif - s) / 512 = ln(c / c_rif): di quante volte l'aria e' piu' carica del riferimento.

Indice = ln-rapporti di H2 ed etanolo combinati (media o massimo), sulla MEDIANA della finestra.
Due profili:
- "normale": finestre di 30 minuti, per il cassonetto vero;
- "demo": finestre di 60 s e soglie basse, per mostrare il sistema dal vivo in pochi minuti
  (es. gel igienizzante all'alcol in uno scatolone chiuso, sensore fuori).
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

RAW_SCALE = 512.0           # costante del datasheet SGP30 per i segnali grezzi
LEVELS = ("green", "yellow", "red")

PROFILES = {
    "normale": {
        "window_s": 30 * 60,          # finestra della mediana
        "ref_window_s": 30 * 60,      # durata della raccolta del riferimento dopo uno svuotamento
        "ref_min_samples": 300,       # campioni minimi per fissare il riferimento (5 min a 1 Hz)
        "min_window_fraction": 0.3,   # serve almeno questa quota della finestra per giudicare
        "eval_period_s": 60,          # ogni quanto si ricalcola il livello
        "yellow_ratio": 2.0,          # odore percepibile (volte il riferimento)
        "red_ratio": 5.0,             # puzza: intervento
        "red_hot_ratio": 3.0,         # puzza con il caldo
        "hot_c": 25.0,
        "min_persistence": 0.6,       # quota dei campioni della finestra sopra la soglia gialla
        "hysteresis_ln": 0.15,        # per scendere di livello bisogna stare un po' sotto la soglia
        "combine": "mean",            # media di H2 ed etanolo: piu' robusta al rumore
        "reuse_saved_reference": True,
    },
    "demo": {
        "window_s": 60,
        "ref_window_s": 60,
        "ref_min_samples": 30,
        "min_window_fraction": 0.5,
        "eval_period_s": 5,
        "yellow_ratio": 1.3,
        "red_ratio": 2.0,
        "red_hot_ratio": 2.0,
        "hot_c": 25.0,
        "min_persistence": 0.5,
        "hysteresis_ln": 0.08,
        "combine": "max",             # l'alcol fa salire soprattutto l'etanolo: conta il canale piu' alto
        "reuse_saved_reference": False,  # in demo il riferimento si misura sempre da capo
    },
}


def raw_to_ln(raw: float) -> float:
    """ln della concentrazione (a meno di una costante) dal segnale grezzo SGP30."""
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

    def instant_ratios(self, h2_raw: float, ethanol_raw: float):
        """Quante volte H2 ed etanolo superano l'aria di riferimento in questo istante (non mediato).
        Serve a leggere il sensore dal vivo: il valore grezzo va al contrario (piu' basso = piu' gas)."""
        ref = self.reference
        if ref is None:
            return None, None
        return (round(math.exp(raw_to_ln(h2_raw) - ref["h2"]), 2),
                round(math.exp(raw_to_ln(ethanol_raw) - ref["eth"]), 2))

    def _combine(self, d_h2: float, d_eth: float) -> float:
        return max(d_h2, d_eth) if self.p["combine"] == "max" else (d_h2 + d_eth) / 2

    # --- campioni -------------------------------------------------------------------------
    def add_sample(self, ts: float, h2_raw: float, ethanol_raw: float, temp_c: float | None):
        """Aggiunge un campione valido (1 Hz). Ritorna la lista degli eventi da segnalare:
        ("reference", status) quando un nuovo riferimento e' pronto,
        ("level", old_level, new_level, status) quando cambia il livello di odore."""
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
                self.last_eval = None   # ricalcola subito con il nuovo riferimento
                events.append(("reference", self.status(ts)))

        if self.last_eval is None or ts - self.last_eval >= self.p["eval_period_s"]:
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
            persistent = persistence >= self.p["min_persistence"] or not need_persistence
            hot = temp is not None and temp >= self.p["hot_c"]
            if persistent and (delta >= self.red_ln - margin or (hot and delta >= self.red_hot_ln - margin)):
                return "red"
            if persistent and delta >= self.yellow_ln - margin:
                return "yellow"
            return "green"

        # Per salire servono mediana sopra soglia E persistenza; per scendere la mediana deve
        # scendere sotto la soglia meno l'isteresi (la persistenza da sola non basta).
        new = level_for(0.0, need_persistence=True)
        if self.level in LEVELS and LEVELS.index(new) < LEVELS.index(self.level):
            hold = level_for(self.p["hysteresis_ln"], need_persistence=False)
            new = min(max(new, hold, key=LEVELS.index), self.level, key=LEVELS.index)
        s["level"] = new
        return new

    def status(self, now: float | None = None) -> dict:
        """Stato per la dashboard e per i messaggi degli eventi."""
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
