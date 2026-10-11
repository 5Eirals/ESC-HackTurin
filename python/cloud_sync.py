"""Invio dei dati di TO-Breath al cloud (API su Vercel -> database Supabase).

La scheda resta autonoma: data/fill_log.csv e gli altri file locali sono il dataset con cui lavora
(previsione, dashboard locale) e la copia di sicurezza. Il cloud raccoglie i dati di tutti i cassonetti
per il pannello di controllo.

Cosa viene inviato (POST <url>/api/ingest, Authorization: Bearer <token del cassonetto>):
- readings: gas, temperatura e umidita' mediati per minuto (la scheda legge ogni secondo);
- fill:     ogni misura di riempimento (all'avvio anche lo storico del CSV non ancora inviato);
- events:   gli eventi del centro di controllo, con un id unico (reinviarli non crea doppioni);
- status:   lo stato attuale (allarme, odore, previsione del pieno, ultima lettura, ultimi 10 minuti
            a passi di 10 s), sostituito a ogni invio.
La risposta porta i comandi del pannello (es. "cassonetto appena svuotato").

Senza rete i dati restano in coda in data/cloud_outbox.sqlite e partono appena torna la connessione.
Configurazione (se manca, il cloud e' semplicemente spento):
- variabili d'ambiente TOBREATH_CLOUD_URL e TOBREATH_DEVICE_TOKEN, oppure
- file data/cloud.json: {"url": "https://<progetto>.vercel.app", "token": "tbd_..."}
Solo libreria standard.
"""

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

SYNC_INTERVAL_S = 15        # un invio ogni 15 s
MAX_ROWS_PER_BATCH = 400    # righe della coda per invio (la coda si svuota a raffiche)
MAX_BATCHES_PER_ROUND = 10  # dopo un periodo offline, al massimo 10 invii di fila
MAX_OUTBOX_ROWS = 100_000   # oltre, si scartano le letture piu' vecchie (eventi e misure restano)
RECENT_WINDOW_S = 600       # ultimi 10 minuti per i grafici del pannello...
RECENT_BUCKET_S = 10        # ...a passi di 10 s
TIMEOUT_S = 15
BACKOFF_MAX_S = 300
READING_FIELDS = ("temperature_c", "humidity_pct", "tvoc_ppb", "eco2_ppm", "h2_raw", "ethanol_raw")


def load_config(data_dir: Path) -> tuple[str | None, str | None]:
    url = os.environ.get("TOBREATH_CLOUD_URL")
    token = os.environ.get("TOBREATH_DEVICE_TOKEN")
    if not (url and token):
        try:
            cfg = json.loads((Path(data_dir) / "cloud.json").read_text())
            url, token = url or cfg.get("url"), token or cfg.get("token")
        except (OSError, ValueError, AttributeError):
            pass
    return (url.rstrip("/") if url else None), token


class CloudSync:
    def __init__(self, data_dir, address: str, status_provider, on_command, log=print):
        self.data_dir = Path(data_dir)
        self.address = address
        self.status_provider = status_provider   # () -> dict, chiamata dal thread di invio
        self.on_command = on_command             # (dict) -> None, comandi arrivati dal pannello
        self.log = log
        self.url, self.token = load_config(self.data_dir)
        self.enabled = bool(self.url and self.token)
        self.boot = uuid.uuid4().hex[:8]         # rende unici gli id degli eventi tra un riavvio e l'altro
        self.lock = threading.Lock()
        self.minute = None                       # minuto in corso e somme per la media
        self.sums, self.counts, self.samples, self.tvoc_max = {}, {}, 0, None
        self.recent = deque()                    # (ts_fine_passo, tvoc medio, eco2 medio)
        self.bucket = None                       # passo da 10 s in corso: [inizio, somma tvoc, n, somma eco2, n]
        self.last_ok = None
        self.failures = 0
        self.db = None
        if self.enabled:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.data_dir / "cloud_outbox.sqlite", check_same_thread=False)
            self.db.execute("create table if not exists outbox "
                            "(id integer primary key autoincrement, kind text not null, payload text not null)")
            self.db.execute("create table if not exists meta (key text primary key, value text not null)")
            self.db.commit()

    # --- coda ------------------------------------------------------------------------------
    def _enqueue(self, kind: str, payload: dict):
        with self.lock:
            self.db.execute("insert into outbox (kind, payload) values (?, ?)", (kind, json.dumps(payload)))
            self.db.commit()

    def _meta(self, key: str, default=None):
        row = self.db.execute("select value from meta where key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_meta(self, key: str, value):
        self.db.execute("insert into meta (key, value) values (?, ?) "
                        "on conflict (key) do update set value = excluded.value", (key, json.dumps(value)))

    def pending(self) -> int:
        if not self.enabled:
            return 0
        with self.lock:
            return self.db.execute("select count(*) from outbox").fetchone()[0]

    # --- dati dalla scheda -------------------------------------------------------------------
    def add_sample(self, sample: dict):
        """Una lettura al secondo: media al minuto per il cloud e passi da 10 s per i grafici."""
        if not self.enabled:
            return
        ts = sample["ts"]
        minute = int(ts // 60) * 60
        finished = None
        with self.lock:
            if self.minute is not None and minute != self.minute:
                finished = self._take_minute()
            if self.minute is None:
                self.minute = minute
            self.samples += 1
            for field in READING_FIELDS:
                value = sample.get(field)
                if value is not None:
                    self.sums[field] = self.sums.get(field, 0.0) + value
                    self.counts[field] = self.counts.get(field, 0) + 1
            if sample.get("tvoc_ppb") is not None:
                self.tvoc_max = max(self.tvoc_max or 0, sample["tvoc_ppb"])
            self._add_recent(ts, sample.get("tvoc_ppb"), sample.get("eco2_ppm"))
        if finished:
            self._enqueue("reading", finished)

    def _take_minute(self):
        """Chiude il minuto in corso (da chiamare con il lock preso)."""
        reading = {"ts": self.minute, "samples": min(self.samples, 120), "tvoc_max_ppb": self.tvoc_max}
        for field in READING_FIELDS:
            n = self.counts.get(field, 0)
            reading[field] = round(self.sums[field] / n, 2) if n else None
        self.minute, self.sums, self.counts, self.samples, self.tvoc_max = None, {}, {}, 0, None
        return reading if reading["samples"] else None

    def _add_recent(self, ts, tvoc, eco2):
        start = int(ts // RECENT_BUCKET_S) * RECENT_BUCKET_S
        if self.bucket is not None and self.bucket[0] != start:
            self._close_bucket()
        if self.bucket is None:
            self.bucket = [start, 0.0, 0, 0.0, 0]
        if tvoc is not None:
            self.bucket[1] += tvoc
            self.bucket[2] += 1
        if eco2 is not None:
            self.bucket[3] += eco2
            self.bucket[4] += 1

    def _close_bucket(self):
        start, tvoc_sum, tvoc_n, eco_sum, eco_n = self.bucket
        self.recent.append((start + RECENT_BUCKET_S,
                            round(tvoc_sum / tvoc_n, 1) if tvoc_n else None,
                            round(eco_sum / eco_n, 1) if eco_n else None))
        self.bucket = None
        while self.recent and self.recent[0][0] < start - RECENT_WINDOW_S:
            self.recent.popleft()

    def recent_series(self) -> list:
        with self.lock:
            return [list(p) for p in self.recent]

    def add_event(self, event: dict):
        if not self.enabled:
            return
        self._enqueue("event", {
            "uid": f"{self.boot}-{event['id']}", "ts": event["ts"], "type": event["type"],
            "severity": event["severity"], "title": event["title"], "message": event.get("message", ""),
            "values": event.get("values") or {},
        })

    def add_fill(self, ts: float, fill_pct: float, peak_pct: float, emptied: bool, source: str = "camera"):
        if not self.enabled:
            return
        with self.lock:
            self.db.execute("insert into outbox (kind, payload) values ('fill', ?)", (json.dumps({
                "ts": ts, "fill_pct": max(0.0, min(100.0, fill_pct)), "peak_pct": max(0.0, min(100.0, peak_pct)),
                "emptied": bool(emptied), "source": source}),))
            self._set_meta("fill_cursor", max(ts, self._meta("fill_cursor", 0)))
            self.db.commit()

    def backfill_fill(self, rows: list[dict]) -> int:
        """Mette in coda le righe del CSV non ancora inviate (tutto lo storico, al primo avvio)."""
        if not self.enabled:
            return 0
        with self.lock:
            cursor = self._meta("fill_cursor", 0)
            new = [r for r in rows if r["ts"] > cursor]
            for r in new:
                self.db.execute("insert into outbox (kind, payload) values ('fill', ?)", (json.dumps({
                    "ts": r["ts"], "fill_pct": max(0.0, min(100.0, r["fill_pct"])),
                    "peak_pct": max(0.0, min(100.0, r["peak_pct"])), "emptied": r["event"] == "emptied",
                    "source": "sim" if r["source"] == "sim" else "camera"}),))
            if new:
                self._set_meta("fill_cursor", new[-1]["ts"])
            self.db.commit()
        return len(new)

    # --- invio -----------------------------------------------------------------------------
    def start(self):
        if not self.enabled:
            self.log("Cloud: non configurato (data/cloud.json o TOBREATH_CLOUD_URL/TOBREATH_DEVICE_TOKEN), "
                     "i dati restano solo sulla scheda")
            return
        self.log(f"Cloud: invio a {self.url} ogni {SYNC_INTERVAL_S} s ({self.pending()} righe in coda)")
        threading.Thread(target=self._run, name="cloud-sync", daemon=True).start()

    def _run(self):
        while True:
            delay = SYNC_INTERVAL_S
            try:
                self.sync_once()
                self.failures = 0
            except Exception as exc:  # rete assente, server giu', token sbagliato: si riprova piu' tardi
                self.failures += 1
                delay = min(BACKOFF_MAX_S, SYNC_INTERVAL_S * 2 ** min(self.failures, 5))
                if self.failures in (1, 3) or self.failures % 20 == 0:
                    self.log(f"Cloud: invio non riuscito ({exc}), riprovo tra {delay} s, "
                             f"{self.pending()} righe in coda")
            time.sleep(delay)

    def _flush_stale_minute(self):
        """Se le letture si fermano, il minuto in corso parte comunque."""
        finished = None
        with self.lock:
            if self.minute is not None and time.time() - self.minute > 120:
                finished = self._take_minute()
            if self.bucket is not None and time.time() - self.bucket[0] > 2 * RECENT_BUCKET_S:
                self._close_bucket()
        if finished:
            self._enqueue("reading", finished)

    def _trim_outbox(self):
        with self.lock:
            n = self.db.execute("select count(*) from outbox").fetchone()[0]
            if n > MAX_OUTBOX_ROWS:
                self.db.execute("delete from outbox where id in (select id from outbox where kind = 'reading' "
                                "order by id limit ?)", (n - MAX_OUTBOX_ROWS,))
                self.db.commit()

    def sync_once(self):
        """Svuota la coda (a raffiche) e manda lo stato attuale. Solleva un'eccezione se fallisce."""
        self._flush_stale_minute()
        self._trim_outbox()
        for _ in range(MAX_BATCHES_PER_ROUND):
            with self.lock:
                rows = self.db.execute("select id, kind, payload from outbox order by id limit ?",
                                       (MAX_ROWS_PER_BATCH,)).fetchall()
            batch = {"address": self.address, "readings": [], "fill": [], "events": []}
            for _, kind, payload in rows:
                batch[{"reading": "readings", "fill": "fill", "event": "events"}[kind]].append(json.loads(payload))
            status = self.status_provider()
            status["recent"] = self.recent_series()
            batch["status"] = status
            answer = self._post(batch)
            if rows:
                with self.lock:
                    self.db.execute("delete from outbox where id <= ?", (rows[-1][0],))
                    self.db.commit()
            self.last_ok = time.time()
            for command in answer.get("commands", []):
                try:
                    self.on_command(command)
                except Exception as exc:
                    self.log(f"Cloud: comando {command.get('command')} non eseguito ({exc})")
            if len(rows) < MAX_ROWS_PER_BATCH:
                break

    def _post(self, batch: dict) -> dict:
        body = json.dumps(batch, separators=(",", ":"), default=float).encode()
        request = urllib.request.Request(f"{self.url}/api/ingest", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "TO-Breath/1.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode(errors="replace")
            if exc.code == 401:
                raise RuntimeError("token del cassonetto non valido (vedi data/cloud.json)") from None
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
