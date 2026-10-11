# TO-Breath

**Il cassonetto che respira Torino.** Progetto per hackTurin 2026, challenge **City Signals**.

TO-Breath trasforma un normale cassonetto in un sensore urbano. Sulla Arduino UNO Q, in locale (funziona anche senza rete), il cassonetto:

1. **annusa** i gas della decomposizione (SGP30) e misura temperatura e umidità (AHT20): se l'aria dentro il cassonetto cambia in modo anomalo (rifiuti organici in degrado, principio d'incendio) lancia un allarme;
2. **guarda** dentro di sé con una camera e un modello di stima della profondità (Depth Anything V2), aiutato da un sensore di distanza ToF, e calcola **quanto è pieno**;
3. **impara** dallo storico dell'ultimo mese a che ritmo si riempie e **prevede quando sarà pieno**;
4. **avvisa** il centro di controllo (simulato) dell'azienda di raccolta rifiuti e mostra tutto su una **dashboard web**;
5. **condivide** i dati con il cloud: un'API su **Vercel** li salva su **Supabase**, e il **pannello di controllo**, anch'esso su Vercel, mostra tutti i cassonetti da qualsiasi luogo.

L'obiettivo è passare dalla raccolta a calendario fisso a una raccolta guidata dai dati: svuotare i cassonetti quando servono, non quando capita.

---

## Hardware

| Componente | Collegamento | A cosa serve |
| :--- | :--- | :--- |
| **Arduino UNO Q** | — | MCU STM32U585 (sketch) + Linux (app Python e dashboard) |
| Grove Base Shield v2 | sopra la UNO Q, interruttore **VCC su 3,3 V** | porte Grove per i sensori |
| **SGP30** (0x58) | porta I2C Grove | TVOC, eCO₂, segnali grezzi H₂ ed etanolo |
| **AHT20** (0x38) | porta I2C Grove | temperatura e umidità (anche per compensare l'SGP30) |
| **Modulino Distance** (ToF) | Qwiic | distanza reale in un punto: ancora metrica per la profondità |
| Anello LED WS2813 (16 LED) | Grove D6 | flash per illuminare l'interno del cassonetto durante lo scatto |
| **Telefono** con app IP Webcam | Wi-Fi, `PHONE_IP:PORT` | camera che inquadra l'interno del cassonetto dall'alto |

## Architettura

```
                         ┌──────────────────────── Arduino UNO Q ────────────────────────┐
 SGP30 ─┐ I2C            │  MCU STM32U585 (sketch/)            Linux (python/)            │
 AHT20 ─┘ ──────────────►│  driver I2C, lettura ogni 1 s  ─►   main.py                    │
 Modulino Distance ─────►│  read_tof()                    ◄─►  ├─ gas_alarm.py            │
 Anello LED ◄────────────│  set_flash()                 Bridge ├─ baseline_store.py       │
                         │                                     ├─ depth_fill.py  (ONNX)   │
 Telefono (IP Webcam) ──────── HTTP(S), una foto ogni 5 min ──► ├─ fill_history.py         │
                         │                                     └─ simulate_fill.py        │
                         │                                          │ WebUI (socket.io)   │
                         └──────────────────────────────────────────┼─────────────────────┘
                                                                    ▼
                                         Dashboard web (assets/), http://<ip-uno-q>:7000
                                         livello di riempimento + previsione, gas,
                                         centro di controllo simulato
```

Il microcontrollore fa solo il lavoro "elettrico" (sensori, LED, ToF) e manda i dati grezzi. Tutte le decisioni (allarmi, profondità, previsioni, eventi) le prende l'app Python sul lato Linux.

**Cloud.** Ogni cassonetto, se configurato, invia i suoi dati al cloud; il pannello di controllo li legge da lì:

```
 UNO Q #1 ─┐  POST /api/ingest ogni 15 s              ┌──────── Vercel (cloud/) ────────┐        ┌──── Supabase ────┐
 UNO Q #2 ─┼─ (token del cassonetto) ───────────────► │ api/ingest.ts  → ingest_batch()  │ ─────► │ bins             │
 UNO Q #n ─┘ ◄── comandi del pannello nella risposta   │ api/bins.ts    → panel_bins()    │        │ bin_status       │
                                                       │ api/bin.ts     → panel_snapshot()│ ◄───── │ readings         │
 Browser ── https://<progetto>.vercel.app/?key=… ────► │ api/command.ts → panel_command() │        │ fill_measurements│
            stessa dashboard di assets/                │ public/ = assets/ (pannello)     │        │ bin_events       │
                                                       └──────────────────────────────────┘        │ bin_commands     │
                                                                                                    └──────────────────┘
```

La scheda resta il punto di verità per sé stessa: `data/fill_log.csv` è il suo dataset (previsione, dashboard locale) e la copia di sicurezza; Supabase raccoglie i dati di tutti i cassonetti per il pannello di controllo.

## Struttura del repository

```
ESC-HackTurin/                   (repository di TO-Breath)
├── app.yaml                     configurazione dell'app Arduino App Lab (nome, brick web_ui)
├── sketch/                      firmware del microcontrollore
│   ├── sketch.ino               loop a 1 Hz, invio dati via Bridge, flash LED, lettura ToF
│   ├── aht20.h / aht20.cpp      driver minimale AHT20 (solo Wire, controllo CRC)
│   ├── sgp30.h / sgp30.cpp      driver minimale SGP30 (IAQ, segnali grezzi, baseline, umidità)
│   ├── sensirion_crc.h          CRC-8 Sensirion
│   └── sketch.yaml              piattaforma e librerie (RouterBridge, Modulino, NeoPixel)
├── python/                      app Linux
│   ├── main.py                  punto d'ingresso: callback del Bridge, loop, eventi, dashboard
│   ├── gas_alarm.py             allarme gas relativo al valore "a riposo"
│   ├── baseline_store.py        salvataggio/ripristino della baseline SGP30
│   ├── depth_fill.py            foto -> profondità -> % di riempimento (Depth Anything V2 + ToF)
│   ├── fill_history.py          storico del riempimento, previsione del pieno, statistiche
│   ├── simulate_fill.py         genera un mese di misure inventate per demo e prove
│   ├── odor_index.py            indice di odore percepito (segnali grezzi SGP30 rispetto al riferimento)
│   ├── cloud_sync.py            invio dei dati al cloud con coda locale (funziona anche offline)
│   └── depth_anything_v2_vits_252.onnx   modello di profondità (ViT-S, ingresso 252×252)
├── assets/                      dashboard web servita dal brick WebUI
│   ├── index.html               struttura della pagina (shell, intestazione, bande)
│   ├── app.js                   rendering di letture, grafici, previsione ed eventi
│   ├── style.css                impaginazione propria della dashboard
│   ├── ds/                      design system IAMAimpresa (token e componenti, da non modificare)
│   └── libs/                    socket.io e client WebUI di Arduino
├── cloud/                       progetto Vercel (Root Directory = cloud)
│   ├── api/                     funzioni: ingest.ts (cassonetti), bins.ts, bin.ts, command.ts (pannello), health.ts
│   ├── lib/                     chiamate a Supabase, risposte HTTP, validazione (zod) e i suoi test
│   ├── panel/arduino.js         la dashboard in cloud: stessa interfaccia della WebUI, dati dall'API
│   ├── scripts/build-panel.mjs  build: copia assets/ in public/ e sostituisce libs/arduino.js
│   └── vercel.json, package.json, .env.example
├── supabase/
│   ├── migrations/              schema del database (tabelle, funzioni, RLS), da eseguire nel SQL Editor
│   ├── reset_to_breath.sql      cancella le tabelle di TO-Breath per ripartire da zero (es. dopo un tentativo a metà)
│   └── optional/kit_access.sql  solo con lo schema del kit: lettura delle tabelle per gli utenti con la funzione `bins`
├── AI_Depth_Analysis/           riferimento al repository originale di Depth-Anything-V2
│                                (non usato a runtime: l'app usa il modello ONNX in python/)
└── data/                        creata a runtime, non versionata (vedi "File generati")
```

---

## Come funziona

### 1. Microcontrollore (`sketch/sketch.ino`)

- All'avvio scansiona il bus I2C (nel Monitor devono comparire `0x38` e `0x58`), inizializza AHT20, SGP30, Modulino Distance e l'anello LED.
- **Ogni secondo** legge l'AHT20, passa l'umidità assoluta all'SGP30 (compensazione), legge TVOC/eCO₂ e i segnali grezzi H₂/etanolo e invia tutto a Python con `sensor_data`.
- **Ogni 60 secondi** invia la baseline dell'SGP30 (`sgp30_baseline`), che Python salva su file.
- Se un sensore fallisce 5 letture di fila viene reinizializzato; se manca, si riprova ogni 5 s.
- Espone a Python `set_flash` (luminosità dell'anello), `read_tof` (mediana di 5 letture) e `sgp30_set_baseline`. Le funzioni che usano l'I2C girano nel thread del loop (`provide_safe`), così non si sovrappongono alle letture dei sensori.

### 2. App Python (`python/main.py`)

L'app ha due flussi che girano in parallelo:

- **Callback del Bridge** (dati gas, ogni secondo): aggiornano l'allarme gas, controllano la salute dei sensori, inviano le letture alla dashboard.
- **Loop principale** (`App.run`): al primo giro ripristina la baseline SGP30 salvata, poi **ogni `CAPTURE_INTERVAL_S` (5 min)** esegue la parte di visione: foto, profondità, riempimento, storico, previsione, eventi.

Le chiamate verso lo sketch si fanno solo dal loop, mai dentro le callback del Bridge.

### 3. Gas: allarme relativo (`gas_alarm.py`, `baseline_store.py`)

Il valore assoluto del TVOC cambia molto da cassonetto a cassonetto, quindi l'allarme è **relativo al valore "a riposo"** di quel cassonetto:

1. dopo il riscaldamento dell'SGP30 (~15 s), per 2 minuti impara il TVOC a riposo;
2. poi il riposo segue lentamente l'ambiente (media mobile), ma solo quando non c'è allarme;
3. **`GAS_ALERT`** se il TVOC resta sopra 3× il riposo (e di almeno 100 ppb) per 10 s;
4. **`GAS_CLEAR`** quando torna sotto 1,5× per 30 s.

L'algoritmo dell'SGP30 impiega ore a stabilizzare la sua baseline: per non ripartire da zero a ogni riavvio, la baseline viene salvata in `data/sgp30_baseline.json` (dopo 10 minuti di funzionamento continuo) e rimandata al sensore all'avvio, se ha meno di 7 giorni.

Se un sensore non risponde per 3 letture di fila parte **`SENSOR_FAULT`**; quando torna a funzionare, **`SENSOR_OK`**.

### 4. Visione: dalla foto al livello di riempimento (`depth_fill.py`)

Ogni 5 minuti `main.py`:

1. accende l'anello LED, aspetta 1 s che l'esposizione del telefono si adatti, **scatta una foto** (`/video/snapshot`, oppure il primo fotogramma dello stream MJPEG) e spegne il LED;
2. **legge il ToF** nello stesso momento;
3. ridimensiona la foto a 640 px di larghezza e la passa a **Depth Anything V2** (ONNX Runtime, CPU), che restituisce una mappa di **profondità relativa**: sa cosa è più vicino e cosa più lontano, ma non in millimetri;
4. la converte in **millimetri** con due ancore di distanza nota, ricalcolate a ogni foto con la relazione `1/Z = a·d + b`:
   - il **bordo del cassonetto** (`rim_mask.png`), sempre visibile e a distanza fissa dalla camera (`rim_dist_mm`);
   - il **punto del ToF** (`tof_px`), dove il Modulino Distance misura la distanza vera.

   Se il ToF non è valido, o il cassonetto è così pieno che le due ancore sono alla stessa distanza, riusa lo spostamento `b` della calibrazione e ricalcola solo la scala sul bordo;
5. confronta la profondità attuale con quella del **cassonetto vuoto** (`empty_depth.npy`) dentro `bin_mask.png`: l'altezza dei rifiuti è `profondità a vuoto − profondità attuale`, da cui:
   - **riempimento** = mediana delle altezze / profondità del cassonetto (livello tipico);
   - **picchi** = 90° percentile (cumuli di sacchi);
   - **filtro**: mediana delle ultime 3 misure;
   - **svuotamento**: un calo di più del 30% rispetto alla misura precedente.

La modalità della visione si sceglie con `VISION_MODE` in `main.py`, oppure con un argomento (`python main.py calibrate`), la variabile d'ambiente `MODE` o un file `python/mode.txt`:

| Modalità | Cosa fa |
| :--- | :--- |
| `snapshot` | salva `calib_frame.jpg`, su cui disegnare le maschere |
| `calibrate` | con il cassonetto **vuoto** crea la mappa di riferimento, poi passa da sola a `run` |
| `run` | funzionamento normale: misura, salva, prevede (le vecchie `run` e `predict` unite) |
| `off` | visione disattivata, restano i sensori gas |

Se mancano `onnxruntime` o `opencv`, la visione si disattiva da sola e il resto continua a funzionare.

### 5. Storico e previsione del pieno (`fill_history.py`)

Ogni misura viene aggiunta a `data/fill_log.csv` (`timestamp, fill_pct, peak_pct, event, source`). Dopo ogni misura l'app stima **quando il cassonetto sarà pieno**:

- **con almeno 7 giorni di dati: profilo settimanale.** Per ciascuna delle 168 ore della settimana calcola il ritmo medio di riempimento (% all'ora) sugli ultimi 30 giorni: così sa che di notte il cassonetto si riempie poco, la sera e nel weekend di più. Il profilo viene poi **scalato sul ciclo in corso** (se questa settimana si riempie più in fretta del solito, la previsione si adegua) e proiettato in avanti, a passi di 15 minuti, dal livello attuale fino al 100%;
- **con meno dati: retta** sulle misure dall'ultimo svuotamento.

Calcola anche le **statistiche degli ultimi 30 giorni**: numero di svuotamenti, intervallo medio tra due raccolte, livello medio al momento della raccolta (se è basso, si svuota troppo spesso) e quante volte il cassonetto era pieno prima che passasse il camion (se succede, si svuota troppo poco).

**Dati simulati (`simulate_fill.py`).** Per mostrare la previsione senza aspettare un mese, con `SIMULATED_HISTORY = True` l'app genera all'avvio, se non c'è ancora uno storico, 30 giorni di misure inventate: raccolta lunedì, mercoledì e venerdì alle 6, più rifiuti la sera e nel weekend, così dal venerdì al lunedì il cassonetto a volte trabocca. Queste righe hanno `source = sim` e la dashboard le segnala con "Dati simulati". Per rigenerarle: `python python/simulate_fill.py`.

### 6. Dashboard e centro di controllo (`assets/`)

La dashboard (porta 7000) usa il design system di IAMAimpresa Kit: i token (`assets/ds/tokens.css`) e i componenti (`assets/ds/components.css`) sono riscritti in CSS semplice, perché il brick WebUI serve file statici e non un'app Next.js. Barra laterale con le sezioni (sui telefoni una barra in basso), tema chiaro e scuro, e dall'alto in basso:

- **Riepilogo**: riempimento, tempo al pieno, odore percepito e TVOC; il numero si colora di arancione o rosso negli stati di attenzione;
- **Riempimento, ultimi 30 giorni**: grafico con la previsione tratteggiata e la soglia di quasi pieno (valori leggibili col mouse o con le frecce della tastiera) e statistiche;
- **Livello** e **Odore percepito**: barre di livello e picchi (con la tacca dell'80%), ritmo previsto, livello medio alla raccolta, tabella giornaliera; quota di tempo sopra la soglia d'odore, rapporto H₂/etanolo, riferimento e il pulsante «Segna come svuotato»;
- **Letture in tempo reale**: TVOC, eCO₂, temperatura, umidità, H₂ ed etanolo grezzi, stato dei sensori e dell'allarme, grafici degli ultimi 10 minuti;
- **Centro di controllo (simulato)**: gli eventi che il cassonetto invia all'azienda di raccolta.

Il design system è di IAMAimpresa ed è concesso ai partecipanti solo per l'evento (IAMAimpresa Hackathon Source License 1.0): niente uso commerciale e niente pubblicazione dei derivati.

| Evento | Gravità | Quando |
| :--- | :--- | :--- |
| `GAS_ALERT` | critical | TVOC anomalo rispetto al riposo |
| `GAS_CLEAR` | info | TVOC rientrato |
| `BIN_ALMOST_FULL` | warning | riempimento ≥ 80%, con il tempo previsto al pieno |
| `BIN_FULL` | critical | riempimento ≥ 95% |
| `BIN_EMPTIED` | info | il cassonetto è stato svuotato |
| `SENSOR_FAULT` / `SENSOR_OK` | warning / info | un sensore smette o riprende a rispondere |

Gli eventi di riempimento partono una volta per ciclo e si riattivano dopo lo svuotamento.

### 7. Cloud: Vercel + Supabase (`cloud_sync.py`, `cloud/`, `supabase/`)

Il cloud segue il blueprint di [IAMAimpresa Kit](https://github.com/IAMA-impresa/iamaimpresa-kit) (Supabase + Vercel): migrazioni SQL, Row Level Security su ogni tabella, nessuna service role key, input validato con zod, intestazioni di sicurezza.

**Dalla scheda al cloud.** `cloud_sync.py` gira in un thread e ogni 15 s manda a `POST /api/ingest`:

| Cosa | Quanto | Note |
| :--- | :--- | :--- |
| `readings` | una riga al minuto | media di gas, temperatura e umidità (la scheda legge ogni secondo), più il TVOC massimo |
| `fill` | una riga per foto | ogni misura di riempimento; al primo avvio tutto lo storico del CSV (anche simulato) |
| `events` | uno per evento | con un id unico: reinviarli non crea doppioni |
| `status` | sostituito a ogni invio | allarme, odore, livello e previsione del pieno, statistiche, ultima lettura, ultimi 10 minuti a passi di 10 s |

Senza rete (o con il cloud giù) tutto resta in coda in `data/cloud_outbox.sqlite` e parte appena torna la connessione, a raffiche da 400 righe; dopo un errore i tentativi si diradano fino a uno ogni 5 minuti. La risposta porta i **comandi** del pannello: «Segna come svuotato» premuto in cloud arriva alla scheda entro ~15 s e fa esattamente quello che fa il pulsante locale.

**Il pannello di controllo** è la stessa dashboard di `assets/`: al build (`cloud/scripts/build-panel.mjs`) Vercel copia `assets/` e sostituisce solo `libs/arduino.js` con `cloud/panel/arduino.js`, che ha la stessa interfaccia della WebUI di Arduino ma legge l'API ogni 10 s. Con più cassonetti compare un menu nell'intestazione; il centro di controllo mostra gli eventi di tutti. "Connesso" vuol dire che il cassonetto ha parlato con il cloud negli ultimi 2 minuti.

**Dove stanno i dati**

| Dato | Sulla scheda | Su Supabase |
| :--- | :--- | :--- |
| Misure di riempimento | `data/fill_log.csv` (30 giorni in uso, file completo) | `fill_measurements`, tutte |
| Gas, temperatura, umidità | ultimi 10 minuti in memoria, a 1 Hz | `readings`, una riga al minuto, tutte |
| Eventi | ultimi 100 in memoria | `bin_events`, tutti |
| Previsione, statistiche, odore, allarme | calcolati dalla scheda | `bin_status`, l'ultimo stato |
| Baseline SGP30, riferimento d'odore, calibrazione | `data/`, `python/` | — |

**Sicurezza.** Nessuno legge o scrive le tabelle direttamente (RLS attiva, nessun permesso al ruolo anonimo). L'API chiama quattro funzioni del database con la chiave *publishable*, e ognuna controlla il proprio segreto:

| Endpoint | Chi | Autorizzazione | Funzione |
| :--- | :--- | :--- | :--- |
| `POST /api/ingest` | cassonetto | `Bearer <token del cassonetto>` | `ingest_batch` |
| `GET /api/bins` | pannello | `Bearer <chiave del pannello>` | `panel_bins` |
| `GET /api/bin?code=TO-0001` | pannello | `Bearer <chiave del pannello>` | `panel_snapshot` |
| `POST /api/command?code=TO-0001` | pannello | `Bearer <chiave del pannello>` | `panel_command` |
| `GET /api/health` | chiunque | — | configurazione presente |

Token e chiavi sono casuali, si vedono una volta sola e nel database c'è solo il loro sha256. Un cassonetto può scrivere solo i propri dati. Se il database ha anche lo schema del kit, `supabase/optional/kit_access.sql` permette agli utenti con la funzione `bins` di leggere le tabelle direttamente (RLS con `has_access`).

La migrazione è scritta per funzionare con qualsiasi client SQL: ogni funzione è una sola istruzione SQL (nessun `;` dentro i corpi `$$ … $$`, che alcuni editor spezzano), niente blocchi `DO`, niente tipi enum, e ogni istruzione si può rieseguire senza errori.

---

## Messaggi

### Bridge (MCU ↔ Python)

| Direzione | Nome | Contenuto |
| :--- | :--- | :--- |
| MCU → Python | `sensor_data` | `uptime_s, aht_ok, sgp_ok, temp_c, rh_pct, tvoc_ppb, eco2_ppm, h2_raw, ethanol_raw`, ogni 1 s |
| MCU → Python | `sgp30_started` | `uptime_s`, ogni volta che l'algoritmo SGP30 riparte |
| MCU → Python | `sgp30_baseline` | `uptime_s, eco2_base, tvoc_base`, ogni 60 s |
| Python → MCU | `sgp30_set_baseline` | `eco2_base, tvoc_base` → `bool` |
| Python → MCU | `set_flash` | `level` (0-255), luminosità dell'anello LED |
| Python → MCU | `read_tof` | → distanza in mm (mediana di 5), `-1` se non valida |
| Python → MCU | `camera_status` | `ok` (bool), esito dell'ultimo scatto, stampato sul Monitor |

### WebUI (Python → dashboard)

| Nome | Quando | Contenuto |
| :--- | :--- | :--- |
| `snapshot` | alla connessione | cassonetto, storico gas, eventi, allarme, stato del riempimento |
| `reading` | ogni 1 s | ultima lettura gas e stato dell'allarme |
| `fill` | dopo ogni misura | livello, previsione, statistiche, serie del grafico, diagnostica |
| `event` | a ogni evento | evento per il centro di controllo |

---

## Installazione e avvio

1. **Collegamenti:** Grove Base Shield sulla UNO Q con **VCC a 3,3 V**; SGP30 e AHT20 su due porte I2C; Modulino Distance sul Qwiic; anello LED su D6.
2. **Camera:** installare IP Webcam (o simile) sul telefono, fissarlo sopra il cassonetto puntato verso il fondo e impostare `PHONE_IP`, `PORT` e `USE_HTTPS` in `python/main.py`.
3. **Avvio:** aprire la cartella in **Arduino App Lab** e premere Run. Lo sketch usa `Arduino_RouterBridge`, `Arduino_Modulino` e `Adafruit NeoPixel` (elencate in `sketch.yaml`); la visione richiede in Python `onnxruntime`, `opencv-python-headless` e `numpy`.
4. **Dashboard:** `http://<ip-della-uno-q>:7000`.
5. **Prova gas:** nei primi ~15 s l'SGP30 è in riscaldamento, nei 2 minuti successivi l'allarme impara il valore a riposo. Avvicinando un pennarello all'alcol al sensore, dopo ~10 s compare `GAS_ALERT`.

### Cloud: Vercel + Supabase (una volta)

1. **Supabase.** Creare un progetto su [supabase.com](https://supabase.com) (va bene anche quello del kit). In *SQL Editor*, in una nuova query, incollare ed eseguire `supabase/reset_to_breath.sql` (toglie quello che un tentativo precedente può aver lasciato; su un database nuovo non fa nulla), poi in un'altra query `supabase/migrations/20261011120000_to_breath_cloud.sql`. Se il progetto ha anche lo schema del kit, eseguire infine `supabase/optional/kit_access.sql`. Poi, sempre nel SQL Editor, registrare i cassonetti e creare la chiave del pannello (ogni comando mostra il segreto una volta sola):
   ```sql
   select public.register_bin('TO-0001', 'Via Vincenzo Vela 17, Torino');   -- token del cassonetto
   select public.create_panel_key('Centro di controllo');                   -- chiave del pannello
   ```
   Il codice deve essere lo stesso di `BIN_ID` in `python/main.py`. Rieseguire `register_bin` dà un token nuovo (il vecchio smette di funzionare); una chiave si revoca con `update public.panel_keys set revoked_at = now() where name = '…'`.
2. **Vercel.** Importare il repository su [vercel.com](https://vercel.com) con **Root Directory = `cloud`** (lasciare attivo «Include files outside the root directory»: il build copia `assets/`). Variabili d'ambiente: `SUPABASE_URL` e `SUPABASE_PUBLISHABLE_KEY` (da *Project Settings → API Keys*; vanno bene anche i nomi `NEXT_PUBLIC_…` del kit). Dopo il deploy, `https://<progetto>.vercel.app/api/health` deve rispondere `{"ok":true}`.
3. **Scheda.** Creare sulla UNO Q il file `data/cloud.json` (non finisce su git):
   ```json
   {"url": "https://<progetto>.vercel.app", "token": "tbd_..."}
   ```
   oppure impostare `TOBREATH_CLOUD_URL` e `TOBREATH_DEVICE_TOKEN`. Al riavvio il log dice `Cloud: invio a … ogni 15 s`. Senza configurazione la scheda funziona come prima, solo in locale.
4. **Pannello.** Aprire `https://<progetto>.vercel.app/?key=tbp_...` (la chiave resta nel browser e sparisce dall'indirizzo); `&bin=TO-0002` apre un cassonetto preciso.

Per provare l'API in locale: `cd cloud && npm install && npx vercel dev` (con `.env.local` copiato da `.env.example`); `npm test` e `npm run typecheck` controllano validazione e tipi.

### Calibrazione della visione (una volta per cassonetto)

1. Avviare in `snapshot` (o in `run`: se mancano le maschere il fotogramma viene salvato comunque) e aprire `python/calib_frame.jpg`.
2. Disegnare in bianco su nero, alla stessa risoluzione, `python/bin_mask.png` (interno del cassonetto) e `python/rim_mask.png` (bordo, mai coperto dai rifiuti).
3. In `depth_fill.CFG` impostare `rim_dist_mm` (camera → bordo), `bin_depth_mm` (bordo → fondo) e `tof_px` (il pixel di `calib_frame.jpg` dove cade il raggio del ToF).
4. Con il cassonetto **vuoto**, avviare in `calibrate`: crea `empty_depth.npy` e `calib_ab.json` e passa da sola a `run`.
5. Sul cassonetto vero mettere `SIMULATED_HISTORY = False` e cancellare l'eventuale `data/fill_log.csv` simulato.

## Parametri da tarare

| Dove | Parametro | Default |
| :--- | :--- | :--- |
| `python/main.py` | `BIN_ID`, `BIN_ADDRESS` | `TO-0001`, Via Vincenzo Vela 17 |
| `python/main.py` | `VISION_MODE`, `CAPTURE_INTERVAL_S` | `run`, 300 s |
| `python/main.py` | `FILL_WARN_PCT`, `FILL_FULL_PCT` | 80%, 95% |
| `python/main.py` | `SIMULATED_HISTORY` | `True` (demo) |
| `python/main.py` | `PHONE_IP`, `PORT`, `USE_HTTPS`, `FLASH_LEVEL`, `FLASH_SETTLE_S` | camera e flash |
| `python/depth_fill.py` | `CFG`: `rim_dist_mm`, `bin_depth_mm`, `tof_px`, `emptied_drop_pct` | misure del cassonetto |
| `python/fill_history.py` | `WINDOW_DAYS`, `PROFILE_MIN_DAYS`, `TREND_CLAMP` | 30 g, 7 g, 0,5-2× |
| `python/gas_alarm.py` | `GasAlarm(...)`: rapporto, delta minimo, tempi di conferma | 3×, 100 ppb, 10 s / 1,5×, 30 s |
| `python/cloud_sync.py` | `SYNC_INTERVAL_S`, `MAX_ROWS_PER_BATCH`, `MAX_OUTBOX_ROWS` | 15 s, 400 righe, 100 000 righe |
| `cloud/panel/arduino.js` | `POLL_MS` (aggiornamento del pannello) | 10 s |

Il criterio definitivo dell'allarme gas è ancora da decidere: i valori attuali sono un punto di partenza da tarare sul campo.

## File generati a runtime

| File | Contenuto |
| :--- | :--- |
| `data/fill_log.csv` | storico delle misure di riempimento (anche quelle simulate) |
| `data/sgp30_baseline.json` | baseline SGP30 da ripristinare all'avvio |
| `python/last_frame.jpg` | ultima foto scattata |
| `python/calib_frame.jpg` | fotogramma su cui disegnare le maschere |
| `python/empty_depth.npy`, `python/calib_ab.json` | calibrazione a vuoto |
| `python/bin_mask.png`, `python/rim_mask.png` | maschere disegnate a mano |
| `data/cloud.json` | indirizzo del cloud e token del cassonetto (segreto) |
| `data/cloud_outbox.sqlite` | coda dei dati non ancora inviati al cloud |

`data/` e `last_frame.jpg` non sono versionati (`.gitignore`).
