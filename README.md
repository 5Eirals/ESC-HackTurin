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
| `python/` | `depth_fill.py` (livello di riempimento: camera + modello di profondità + ToF; modalità scelta con `VISION_MODE` in `main.py`), `main.py` (logica, eventi, WebUI), `gas_alarm.py` (allarme relativo al valore a riposo), `baseline_store.py` (persistenza della baseline SGP30) |
| `assets/` | Dashboard: letture in tempo reale, grafici TVOC/eCO₂, centro di controllo simulato |

### Messaggi via Bridge

| Direzione | Nome | Contenuto |
| :--- | :--- | :--- |
| MCU → Python | `sensor_data` | `uptime_s, aht_ok, sgp_ok, temp_c, rh_pct, tvoc_ppb, eco2_ppm, h2_raw, ethanol_raw`, ogni 1 s |
| MCU → Python | `sgp30_started` | `uptime_s`, ogni volta che l'algoritmo SGP30 riparte |
| MCU → Python | `sgp30_baseline` | `uptime_s, eco2_base, tvoc_base`, ogni 60 s |
| Python → MCU | `sgp30_set_baseline` | `eco2_base, tvoc_base` → `bool` |
| Python → MCU | `set_flash` | `level` (0-255), luminosità dell'anello LED |
| Python → MCU | `read_tof` | → distanza in mm (mediana di 5), `-1` se non valida |

## Collegamento

1. Montare il Grove Base Shield v2 sulla UNO Q e mettere l'interruttore **VCC su 3,3 V**.
2. Collegare SGP30 e AHT20 a due porte **I2C** qualsiasi dello shield.
3. All'avvio lo sketch scansiona il bus e stampa nel Monitor gli indirizzi trovati: devono comparire `0x38` e `0x58`.

## Avvio

1. Aprire la cartella in **Arduino App Lab** e premere Run. Per i sensori gas non servono librerie esterne (solo `Wire` e `Arduino_RouterBridge`); la parte di visione richiede `Modulino` e `Adafruit_NeoPixel` nello sketch e `onnxruntime`, `opencv-python-headless`, `numpy` in Python. Se mancano i pacchetti Python, la visione si disattiva e i sensori gas continuano a funzionare.
2. Aprire la dashboard su `http://<ip-della-uno-q>:7000`.
3. Nei primi ~15 s l'SGP30 è in riscaldamento (eCO₂ 400, TVOC 0). Nei 2 minuti successivi l'allarme impara il valore "a riposo".
4. Test rapido: avvicinare un pennarello all'alcol al sensore. Dopo ~10 s compare l'evento `GAS_ALERT`.

## Parametri da tarare

- **Allarme gas:** `GasAlarm(...)` in `python/gas_alarm.py` (rapporto rispetto al riposo, delta minimo, tempi di conferma). **Il criterio definitivo è ancora da decidere.**
- **Cassonetto:** `BIN_ID` e `BIN_ADDRESS` in `python/main.py`.
- **Baseline SGP30:** viene salvata in `data/sgp30_baseline.json` dopo 10 minuti di funzionamento continuo (`BASELINE_MIN_RUNTIME_S`) e ripristinata a ogni avvio, se ha meno di 7 giorni.
