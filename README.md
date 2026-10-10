# ESC-HackTurin: cassonetto smart

Progetto per hackTurin 2026, challenge **City Signals**. Un cassonetto misura i gas da decomposizione (SGP30) e la temperatura e l'umidità (AHT20). La decisione avviene in locale sulla Arduino UNO Q: in caso di anomalia il cassonetto manda un evento al centro di controllo, simulato, dell'azienda di raccolta rifiuti.

## Architettura

```
 SGP30 (0x58) ─┐  I2C (Grove Base Shield, porte I2C, VCC 3,3 V)
 AHT20 (0x38) ─┘
        │
        ▼
 MCU STM32U585 (sketch/)            ── Bridge ──►    Linux (python/)
 driver I2C scritti da noi,                          allarme gas relativo al riposo,
 compensazione d'umidità,                            salvataggio/ripristino della baseline,
 lettura ogni 1 s                                    eventi per il centro di controllo
                                                             │
                                                             ▼
                                                     Dashboard web (assets/), porta 7000
```

| Cartella | Contenuto |
| :--- | :--- |
| `sketch/` | Firmware MCU: anello LED (flash) e Modulino Distance per la visione; `aht20.*` e `sgp30.*` (driver I2C minimali con controllo CRC, usano solo `Wire`), `sketch.ino` (lettura a 1 Hz e invio via Bridge) |
| `python/` | `depth_fill.py` (livello di riempimento: camera + modello di profondità + ToF; modalità scelta con `VISION_MODE` in `main.py`), `main.py` (logica, eventi, WebUI), `fill_history.py` (storico del riempimento e previsione del pieno), `simulate_fill.py` (un mese di misure inventate per le prove), `gas_alarm.py` (allarme relativo al valore a riposo), `baseline_store.py` (persistenza della baseline SGP30) |
| `assets/` | Dashboard: livello di riempimento con previsione, letture in tempo reale, grafici TVOC/eCO₂, centro di controllo simulato |

### Messaggi via Bridge

| Direzione | Nome | Contenuto |
| :--- | :--- | :--- |
| MCU → Python | `sensor_data` | `uptime_s, aht_ok, sgp_ok, temp_c, rh_pct, tvoc_ppb, eco2_ppm, h2_raw, ethanol_raw`, ogni 1 s |
| MCU → Python | `sgp30_started` | `uptime_s`, ogni volta che l'algoritmo SGP30 riparte |
| MCU → Python | `sgp30_baseline` | `uptime_s, eco2_base, tvoc_base`, ogni 60 s |
| Python → MCU | `sgp30_set_baseline` | `eco2_base, tvoc_base` → `bool` |
| Python → MCU | `set_flash` | `level` (0-255), luminosità dell'anello LED |
| Python → MCU | `read_tof` | → distanza in mm (mediana di 5), `-1` se non valida |
| Python → MCU | `camera_status` | `ok` (bool), esito dell'ultimo scatto, stampato sul Monitor |

## Collegamento

1. Montare il Grove Base Shield v2 sulla UNO Q e mettere l'interruttore **VCC su 3,3 V**.
2. Collegare SGP30 e AHT20 a due porte **I2C** qualsiasi dello shield.
3. All'avvio lo sketch scansiona il bus e stampa nel Monitor gli indirizzi trovati: devono comparire `0x38` e `0x58`.

## Avvio

1. Aprire la cartella in **Arduino App Lab** e premere Run. Per i sensori gas non servono librerie esterne (solo `Wire` e `Arduino_RouterBridge`); la parte di visione richiede `Modulino` e `Adafruit_NeoPixel` nello sketch e `onnxruntime`, `opencv-python-headless`, `numpy` in Python. Se mancano i pacchetti Python, la visione si disattiva e i sensori gas continuano a funzionare.
2. Aprire la dashboard su `http://<ip-della-uno-q>:7000`.
3. Nei primi ~15 s l'SGP30 è in riscaldamento (eCO₂ 400, TVOC 0). Nei 2 minuti successivi l'allarme impara il valore "a riposo".
4. Test rapido: avvicinare un pennarello all'alcol al sensore. Dopo ~10 s compare l'evento `GAS_ALERT`.

## Visione: livello di riempimento

Ogni `CAPTURE_INTERVAL_S` (5 min) il loop di `main.py` accende l'anello LED, scatta una foto con il telefono (IP Webcam, `PHONE_IP`/`PORT`), legge il ToF e passa il fotogramma a `depth_fill.analyze()`. Depth Anything V2 stima la profondità relativa, che viene convertita in mm con due ancore (bordo del cassonetto e punto del ToF) e confrontata con la mappa del cassonetto vuoto. La modalità si sceglie con `VISION_MODE` in `main.py` (oppure con un argomento, la variabile `MODE` o un file `python/mode.txt`; `off` la disattiva):

1. **`snapshot`**: salva `python/calib_frame.jpg` (640 px di larghezza). Su questa immagine si disegnano `bin_mask.png` (interno del cassonetto) e `rim_mask.png` (bordo), in bianco su nero, e si salvano nella cartella `python/`. Si impostano anche `tof_px`, `rim_dist_mm` e `bin_depth_mm` in `depth_fill.CFG`. Anche in `run`, se mancano le maschere, viene salvato `calib_frame.jpg`.
2. **`calibrate`** (cassonetto VUOTO): crea `empty_depth.npy` e `calib_ab.json`, poi passa da sola a `run`.
3. **`run`**: misura il riempimento (mediana delle ultime 3 misure), i picchi (90° percentile) e lo svuotamento (calo maggiore del 30%), salva la misura e aggiorna la previsione (le vecchie modalità `run` e `predict` sono ora una sola).

### Storico e previsione

Ogni misura viene aggiunta a `data/fill_log.csv` (`timestamp, fill_pct, peak_pct, event, source`). Dopo ogni misura `fill_history.py` stima quando il cassonetto sarà pieno:

- **con almeno 7 giorni di dati: profilo settimanale.** Per ciascuna delle 168 ore della settimana calcola il ritmo medio di riempimento (% all'ora) sugli ultimi 30 giorni, quindi sa che di notte si riempie poco e nel weekend di più. Il profilo viene scalato sul ciclo in corso (se questa settimana si riempie più in fretta del solito, la previsione si adegua) e proiettato in avanti dal livello attuale fino al 100%;
- **con meno dati: retta** sulle misure dall'ultimo svuotamento (il vecchio `predict`).

Calcola anche le statistiche degli ultimi 30 giorni: numero di svuotamenti, intervallo medio, livello medio al momento della raccolta, volte in cui era pieno prima della raccolta.

**Dati simulati.** Con `SIMULATED_HISTORY = True` (in `main.py`), se `data/fill_log.csv` non esiste viene generato un mese di misure inventate (`simulate_fill.py`). Il cassonetto simulato viene svuotato lunedì, mercoledì e venerdì alle 6, si riempie di più la sera e nel weekend, e dal venerdì al lunedì a volte trabocca. Queste righe hanno `source = sim` e la dashboard le segnala con "Dati simulati". Per rigenerarle: `python python/simulate_fill.py`. Sul cassonetto vero mettere `SIMULATED_HISTORY = False` e cancellare il file.

### Dashboard e centro di controllo

La sezione **Livello di riempimento** mostra il livello attuale (con un indicatore a forma di cassonetto), il tempo che manca al pieno con data e ora, il ritmo previsto, i picchi, il livello medio alla raccolta, il grafico degli ultimi 30 giorni con la previsione tratteggiata (passando col mouse, o con le frecce da tastiera, si leggono i valori) e una tabella giornaliera. Python invia `snapshot` alla connessione e `fill` dopo ogni misura.

Eventi per il centro di controllo: `BIN_ALMOST_FULL` (≥ 80%, con il tempo previsto al pieno), `BIN_FULL` (≥ 95%), `BIN_EMPTIED`. Ogni evento parte una volta per ciclo e si riattiva dopo lo svuotamento.

## Parametri da tarare

- **Allarme gas:** `GasAlarm(...)` in `python/gas_alarm.py` (rapporto rispetto al riposo, delta minimo, tempi di conferma). **Il criterio definitivo è ancora da decidere.**
- **Cassonetto:** `BIN_ID` e `BIN_ADDRESS` in `python/main.py`.
- **Riempimento:** soglie `FILL_WARN_PCT` / `FILL_FULL_PCT` e intervallo `CAPTURE_INTERVAL_S` in `python/main.py`; parametri della previsione in testa a `python/fill_history.py`.
- **Baseline SGP30:** viene salvata in `data/sgp30_baseline.json` dopo 10 minuti di funzionamento continuo (`BASELINE_MIN_RUNTIME_S`) e ripristinata a ogni avvio, se ha meno di 7 giorni.
