// TO-Breath in cloud: prende il posto di libs/arduino.js quando la dashboard e' servita da Vercel.
// Stessa interfaccia della WebUI di Arduino (on_connect, on_disconnect, on_message, send_message), ma i dati
// arrivano dall'API su Vercel (/api/bins, /api/bin) che legge Supabase: app.js e index.html restano
// identici sulla scheda e in cloud.
//
// Apertura: https://<progetto>.vercel.app/?key=<chiave del pannello>[&bin=TO-0001]
// La chiave resta salvata nel browser e viene tolta dall'indirizzo.

(() => {
  const POLL_MS = 10_000;          // ogni quanto si rilegge il cassonetto
  const BINS_EVERY = 6;            // l'elenco dei cassonetti si rilegge ogni 6 giri (1 minuto)
  const HISTORY_SIZE = 600;        // come app.js: 10 minuti a 1 lettura al secondo
  const GAP_S = 30;                // buco nei dati: la linea del grafico si interrompe
  const KEY_STORE = 'tobreath.panelKey';
  const BIN_STORE = 'tobreath.bin';

  const store = {
    get(name) { try { return localStorage.getItem(name); } catch { return null; } },
    set(name, value) { try { localStorage.setItem(name, value); } catch { /* solo per questa visita */ } },
    remove(name) { try { localStorage.removeItem(name); } catch { /* niente da togliere */ } },
  };

  const timeFmt = new Intl.DateTimeFormat('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const dayTimeFmt = new Intl.DateTimeFormat('it-IT', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

  class AuthError extends Error {}

  // Le letture recenti arrivano ogni 10 s ([ts, tvoc, eco2]): si ricostruisce una lettura al secondo
  // per i grafici di app.js, interpolando tra due punti vicini e lasciando vuoti i buchi.
  function expandRecent(recent, sample) {
    const pts = (recent ?? []).filter((p) => Array.isArray(p) && p.length >= 3).sort((a, b) => a[0] - b[0]);
    const lerp = (a, b, f) => (a === null || b === null ? (f < 0.5 ? a : b) : a + (b - a) * f);
    const out = [];
    for (let i = 0; i + 1 < pts.length; i++) {
      const [t0, tvoc0, eco0] = pts[i];
      const [t1, tvoc1, eco1] = pts[i + 1];
      const span = Math.round(t1 - t0);
      if (span <= 0) continue;
      for (let k = 0; k < span; k++) {
        const f = k / span;
        const gap = span > GAP_S;
        out.push({ ts: t0 + k, tvoc_ppb: gap ? null : lerp(tvoc0, tvoc1, f), eco2_ppm: gap ? null : lerp(eco0, eco1, f) });
      }
    }
    if (pts.length) {
      const [t, tvoc, eco2] = pts[pts.length - 1];
      out.push({ ts: t, tvoc_ppb: tvoc, eco2_ppm: eco2 });
    }
    const history = out.slice(-(HISTORY_SIZE - 1));
    if (sample) history.push(sample);   // l'ultima lettura completa: valori, sensori, riscaldamento
    return history;
  }

  // La risposta di /api/bin nella forma del messaggio "snapshot" della scheda.
  function toSnapshot(s) {
    const now = Date.now() / 1000;
    return {
      bin: s.bin,
      history: expandRecent(s.recent, s.sample),
      events: (s.events ?? []).map((e) => ({
        ...e,
        time: (now - e.ts > 86_400 ? dayTimeFmt : timeFmt).format(e.ts * 1000),
      })),
      alarm: s.alarm ?? null,
      fill: s.fill ?? null,
      odor: s.odor ?? null,
    };
  }

  class WebUI {
    #handlers = {};
    #online = null;
    #key;
    #bin;
    #bins = [];
    #timer = null;
    #rounds = 0;

    constructor() {
      const url = new URL(window.location.href);
      const key = url.searchParams.get('key');
      if (key) {
        store.set(KEY_STORE, key);
        url.searchParams.delete('key');
        window.history.replaceState(null, '', url);
      }
      this.#key = key || store.get(KEY_STORE);
      this.#bin = (url.searchParams.get('bin') || store.get(BIN_STORE) || '').toUpperCase() || null;
      // app.js registra le sue funzioni subito dopo `new WebUI()`: si parte al giro successivo.
      setTimeout(() => this.#poll(), 0);
    }

    on_connect(callback) { this.#on('connect', callback); }
    on_disconnect(callback) { this.#on('disconnect', callback); }
    on_message(eventName, callback) { this.#on(eventName, callback); }

    send_message(eventName) {
      if (eventName === 'mark_emptied' && this.#bin) this.#command('mark_emptied');
    }

    #on(name, callback) {
      (this.#handlers[name] ??= []).push(callback);
    }

    #emit(name, data) {
      for (const callback of this.#handlers[name] ?? []) {
        try { callback(data); } catch (err) { console.error(err); }
      }
    }

    async #api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        cache: 'no-store',
        headers: { Authorization: `Bearer ${this.#key}`, 'Content-Type': 'application/json' },
      });
      if (response.status === 401) throw new AuthError('chiave');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }

    async #poll() {
      clearTimeout(this.#timer);
      if (!this.#key) {
        this.#problem('Chiave del pannello mancante: apri il link con ?key=…');
        return;
      }
      try {
        if (this.#rounds++ % BINS_EVERY === 0 || !this.#bin) await this.#loadBins();
        if (!this.#bin) {
          this.#problem('Nessun cassonetto registrato');
        } else {
          const snapshot = await this.#api(`/api/bin?code=${encodeURIComponent(this.#bin)}`);
          this.#emit('snapshot', toSnapshot(snapshot));
          this.#setOnline(Boolean(snapshot.online));
        }
      } catch (err) {
        if (err instanceof AuthError) {
          store.remove(KEY_STORE);
          this.#problem('Chiave del pannello non valida');
          return;   // inutile riprovare con la stessa chiave
        }
        console.error(err);
        this.#setOnline(false);
      }
      this.#timer = setTimeout(() => this.#poll(), POLL_MS);
    }

    async #loadBins() {
      const { bins } = await this.#api('/api/bins');
      this.#bins = bins;
      if (!bins.some((b) => b.code === this.#bin)) {
        this.#bin = (bins.find((b) => b.online) ?? bins[0])?.code ?? null;
      }
      if (this.#bin) store.set(BIN_STORE, this.#bin);
      this.#renderPicker();
    }

    async #command(command) {
      try {
        await this.#api(`/api/command?code=${encodeURIComponent(this.#bin)}`, {
          method: 'POST',
          body: JSON.stringify({ command }),
        });
      } catch (err) {
        console.error(err);
      }
      this.#poll();
    }

    // "Connesso" vuol dire che il cassonetto ha parlato con il cloud negli ultimi 2 minuti.
    #setOnline(online) {
      if (online === this.#online) return;
      this.#online = online;
      this.#emit(online ? 'connect' : 'disconnect');
    }

    #problem(text) {
      const badge = document.getElementById('connection');
      if (!badge) return;
      badge.textContent = text;
      badge.className = 'status status-destructive';
    }

    // Con piu' cassonetti, un menu nell'intestazione sceglie quale guardare.
    #renderPicker() {
      const actions = document.querySelector('.header-actions');
      let select = document.getElementById('bin-picker');
      if (this.#bins.length < 2 || !actions) {
        select?.remove();
        return;
      }
      if (!select) {
        select = document.createElement('select');
        select.id = 'bin-picker';
        select.className = 'btn btn-outline btn-sm';
        select.style.maxWidth = 'min(22rem, 42vw)';   // sul telefono lascia spazio al percorso
        select.style.textOverflow = 'ellipsis';
        select.setAttribute('aria-label', 'Cassonetto');
        select.addEventListener('change', () => {
          this.#bin = select.value;
          store.set(BIN_STORE, this.#bin);
          const url = new URL(window.location.href);
          url.searchParams.set('bin', this.#bin);
          window.history.replaceState(null, '', url);
          this.#online = null;
          this.#poll();
        });
        actions.prepend(select);
      }
      select.replaceChildren(...this.#bins.map((b) => {
        const option = document.createElement('option');
        option.value = b.code;
        option.textContent = `${b.code}${b.address ? ` · ${b.address}` : ''}${b.online ? '' : ' (offline)'}`;
        return option;
      }));
      select.value = this.#bin;
    }
  }

  window.WebUI = WebUI;
})();
