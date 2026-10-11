# TO-Breathe: il cassonetto che respira Torino

## Inspiration

Tornando a casa capita di fare quei dieci passi di fretta, perché i bidoni stracolmi sotto casa iniziano a essere maleodoranti. Non è sfortuna: la raccolta segue un calendario fisso, e il calendario non sa che cosa succede davvero dentro ai cassonetti. Così i camion passano da bidoni mezzi vuoti, mentre altri traboccano dal venerdì al lunedì.

Ci siamo chiesti come ottimizzare la raccolta per evitare queste spiacevoli evenienze. La nostra risposta è una città più connessa e digitale, in cui sia il cassonetto stesso a dire quando ha bisogno di essere svuotato.

## What it does

TO-Breathe trasforma un normale cassonetto in un sensore urbano. Raccoglie dati in tempo reale per l'azienda incaricata dei ritiri e la aiuta a organizzare i turni grazie alla previsione dei riempimenti fatta dall'AI.

- **Annusa:** un sensore di gas (SGP30) con temperatura e umidità (AHT20) rileva i gas della decomposizione e calcola un indice di odore percepito, confrontato con l'aria di riferimento registrata dopo l'ultimo svuotamento.
- **Guarda:** una camera fotografa l'interno del cassonetto, illuminato da un anello LED. Un modello di stima della profondità (Depth Anything V2) e un sensore di distanza ToF calcolano quanto è pieno.
- **Prevede:** dallo storico del riempimento degli ultimi 30 giorni impara il ritmo di ogni ora della settimana (di notte poco, la sera e nel weekend di più) e stima quando il cassonetto sarà pieno.
- **Avvisa:** invia eventi all'azienda di raccolta (quasi pieno, pieno, svuotato, odore anomalo, sensore guasto) e mostra tutto su una dashboard web locale.
- **Condivide:** tutti i cassonetti inviano i dati a un unico pannello di controllo in cloud, da cui l'operatore vede la situazione della città e può anche segnare un cassonetto come svuotato.

Il cassonetto, inoltre, compensa la mancanza di sensori hardware specifici per il livello: la stima del riempimento è fatta dall'AI e pensata per funzionare su ogni tipo di bidone, in modo plug and play. Il sistema si calibra da solo a cassonetto vuoto, senza misure o maschere disegnate a mano.

## How we built it

**Hardware.** Il cuore è una **Arduino UNO Q**, che unisce un microcontrollore STM32U585 e un computer Linux sulla stessa scheda. Il microcontrollore legge SGP30 e AHT20 ogni secondo, legge il ToF (Modulino Distance) e accende l'anello LED WS2813. La camera è un telefono con l'app IP Webcam.

**Software sulla scheda.** L'app Python sul lato Linux prende tutte le decisioni, in locale e anche senza rete:

- ogni 5 minuti scatta una foto con il flash e la passa a **Depth Anything V2** (ONNX Runtime, su CPU), che restituisce una mappa di profondità *relativa*;
- la converte in millimetri con due ancore di distanza nota, una fascia di parete e il punto misurato dal ToF, secondo la relazione

$$\frac{1}{Z} = a \cdot d + b$$

  dove $d$ è l'uscita del modello e $Z$ la distanza reale;
- confronta la profondità attuale con quella del cassonetto vuoto e ne ricava livello tipico (mediana) e picchi (90° percentile);
- salva ogni misura nello storico e aggiorna la previsione del pieno con un profilo settimanale, scalato sul ciclo in corso.

**Autocalibrazione.** A cassonetto vuoto l'AI riconosce da sola il fondo e le pareti. Due pareti opposte sono parallele, quindi le loro rette di profondità si incontrano sulla linea di fuga: da lì si ricava lo spostamento $b$, mentre il ToF fornisce la scala $a$. Nessuna misura manuale, nessuna maschera da disegnare.

**Dashboard e cloud.** La dashboard locale usa il design system di IAMAimpresa. Il cloud segue il blueprint IAMAimpresa Kit: un'API su **Vercel** valida i dati con zod e li salva su **Supabase**, con Row Level Security su ogni tabella e accesso solo tramite funzioni del database. Se la rete cade, i dati restano in una coda locale (SQLite) e partono appena torna la connessione. Il pannello in cloud riusa la stessa dashboard della scheda.

## Challenges we ran into

- **Stimare la profondità.** Depth Anything V2 sa che cosa è più vicino e che cosa è più lontano, ma non di quanto in millimetri, e la scala cambia a ogni foto. Abbiamo dovuto associare al modello un sistema di controllo, cioè le ancore fisiche del ToF e della fascia di riferimento, per verificare il riempimento con più accuratezza.
- **Integrare l'AI in locale.** Il modello doveva girare sulla CPU della UNO Q, insieme a tutto il resto, senza dipendere dalla rete.
- **Orchestrare sensori e camera.** Flash, scatto del telefono e lettura del ToF devono avvenire nello stesso momento, mentre il microcontrollore continua a leggere i gas ogni secondo senza che le chiamate I2C si sovrappongano.
- **La calibrazione.** Calibrare a mano ogni cassonetto (distanze, maschere, profondità a vuoto) era lento e fragile, e avrebbe reso impossibile il plug and play. Per questo l'abbiamo sostituita con l'autocalibrazione.

## Accomplishments that we're proud of

- Una stima del riempimento che non richiede un sensore di livello dedicato e si calibra da sola: sulla scatola della demo (fondo a 220 mm) l'AI trova la fascia di riferimento a circa 90 mm, con stime fra 86 e 94 mm.
- Un sistema che funziona interamente in locale e resta utile anche quando la rete cade, senza perdere dati.
- Una previsione del pieno che conosce le abitudini della settimana e non è una semplice retta.
- Un percorso completo dal sensore al cloud: dalla scheda al pannello di controllo multi-cassonetto, con un'architettura sicura fin dall'inizio.

## What we learned

- Un modello di AI da solo non basta: diventa affidabile quando lo si àncora a una misura fisica, anche un solo punto di un sensore ToF.
- Fare edge computing su una scheda come la UNO Q significa dividere bene i compiti: il microcontrollore per il lavoro "elettrico", Linux per le decisioni.
- I dati contano più dei singoli valori: confrontare ogni cassonetto con sé stesso (aria di riferimento, ritmo di riempimento) funziona meglio di soglie assolute uguali per tutti.
- Costruire il cloud con sicurezza e funzionamento offline fin dall'inizio costa poco in più e evita di rifare tutto dopo.

## What's next for TO-Breathe

Il prossimo passo è scalare da un'alpha sperimentale a veri cassonetti sul territorio, fino all'adozione da parte dell'azienda di raccolta:

- installare i primi prototipi su cassonetti reali e sostituire i dati simulati con uno storico vero;
- rendere l'hardware robusto per l'esterno (involucro, alimentazione, camera integrata al posto del telefono);
- integrare il pannello con i sistemi dell'azienda, per trasformare le previsioni in percorsi di raccolta ottimizzati.
