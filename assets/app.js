// Dashboard del cassonetto: letture in tempo reale, grafici e centro di controllo simulato.

const HISTORY_SIZE = 600;
const MAX_EVENTS = 100;

const history = [];
let restTvoc = null;

const $ = (id) => document.getElementById(id);
const fmt = (v, digits = 0) => (v === null || v === undefined ? '—' : Number(v).toFixed(digits));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const ui = new WebUI();
ui.on_connect(() => setBadge($('connection'), 'Connesso', 'ok'));
ui.on_disconnect(() => setBadge($('connection'), 'Disconnesso', 'off'));

ui.on_message('snapshot', (data) => {
  $('bin-id').textContent = data.bin.id;
  $('bin-address').textContent = data.bin.address;
  history.length = 0;
  history.push(...data.history);
  $('events').innerHTML = '';
  if (data.events.length === 0) renderEmptyEvents();
  data.events.forEach(addEvent);
  const last = history[history.length - 1];
  if (last) renderSample(last);
  renderAlarm(data.alarm);
  drawCharts();
  if (data.fill) renderFill(data.fill);
});

ui.on_message('fill', renderFill);

ui.on_message('reading', (data) => {
  history.push(data.sample);
  if (history.length > HISTORY_SIZE) history.shift();
  renderSample(data.sample);
  renderAlarm(data.alarm);
  drawCharts();
});

ui.on_message('event', addEvent);

function setBadge(el, text, kind) {
  el.textContent = text;
  el.className = `badge badge-${kind}`;
}

function renderSample(s) {
  $('tvoc').textContent = fmt(s.tvoc_ppb);
  $('eco2').textContent = fmt(s.eco2_ppm);
  $('temp').textContent = fmt(s.temperature_c, 1);
  $('rh').textContent = fmt(s.humidity_pct, 1);
  $('h2').textContent = fmt(s.h2_raw);
  $('ethanol').textContent = fmt(s.ethanol_raw);
  setBadge($('sensor-aht'), s.aht_ok ? 'AHT20 ok' : 'AHT20 assente', s.aht_ok ? 'ok' : 'bad');
  if (!s.sgp_ok) setBadge($('sensor-sgp'), 'SGP30 assente', 'bad');
  else if (s.sgp_warming_up) setBadge($('sensor-sgp'), 'SGP30 in riscaldamento', 'warn');
  else setBadge($('sensor-sgp'), 'SGP30 ok', 'ok');
}

function renderAlarm(a) {
  if (!a) return;
  restTvoc = a.rest_tvoc_ppb;
  const banner = $('alarm-banner');
  if (a.state === 'alert') {
    setBadge($('alarm-state'), 'ALLARME GAS', 'bad');
    banner.textContent = `Allarme gas: TVOC ${a.ratio}× il valore a riposo (soglia ${a.ratio_on}×). Evento inviato al centro di controllo.`;
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
    if (a.state === 'learning') setBadge($('alarm-state'), 'Apprendimento valore a riposo…', 'warn');
    else setBadge($('alarm-state'), 'Gas nella norma', 'ok');
  }
  $('rest').textContent = a.rest_tvoc_ppb === null ? '' : `A riposo: ${a.rest_tvoc_ppb} ppb · ora ${a.ratio ?? '—'}×`;
}

function renderEmptyEvents() {
  $('events').innerHTML = '<li class="empty muted">Nessun evento.</li>';
}

function addEvent(e) {
  const list = $('events');
  list.querySelector('.empty')?.remove();

  const li = document.createElement('li');
  li.className = `event ${e.severity}`;
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${e.time} · ${e.bin_id} · ${e.address} · ${e.type}`;
  const title = document.createElement('div');
  title.className = 'title';
  title.textContent = e.title;
  const message = document.createElement('p');
  message.className = 'message';
  message.textContent = e.message;
  li.append(meta, title, message);

  list.prepend(li);
  while (list.children.length > MAX_EVENTS) list.lastChild.remove();
}

function drawCharts() {
  drawSeries($('chart-tvoc'), history.map((s) => s.tvoc_ppb), css('--tvoc'), restTvoc);
  drawSeries($('chart-eco2'), history.map((s) => s.eco2_ppm), css('--eco2'), null);
}

function drawSeries(canvas, values, color, refLine) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 44, right: 8, top: 8, bottom: 8 };
  const valid = values.filter((v) => v !== null && v !== undefined);
  if (valid.length < 2) {
    ctx.fillStyle = css('--muted');
    ctx.font = '12px system-ui';
    ctx.fillText('In attesa di dati…', pad.left, height / 2);
    return;
  }

  let max = Math.max(...valid, refLine ?? 0);
  let min = Math.min(...valid, refLine ?? Infinity);
  if (max === min) { max += 1; min = Math.max(0, min - 1); }
  const x = (i) => pad.left + (i / (HISTORY_SIZE - 1)) * (width - pad.left - pad.right);
  const y = (v) => pad.top + (1 - (v - min) / (max - min)) * (height - pad.top - pad.bottom);
  const offset = HISTORY_SIZE - values.length;

  ctx.fillStyle = css('--muted');
  ctx.font = '11px system-ui';
  ctx.textAlign = 'right';
  ctx.fillText(String(Math.round(max)), pad.left - 6, pad.top + 10);
  ctx.fillText(String(Math.round(min)), pad.left - 6, height - pad.bottom);

  if (refLine !== null && refLine !== undefined) {
    ctx.strokeStyle = css('--muted');
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, y(refLine));
    ctx.lineTo(width - pad.right, y(refLine));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  let drawing = false;
  values.forEach((v, i) => {
    if (v === null || v === undefined) { drawing = false; return; }
    if (drawing) ctx.lineTo(x(i + offset), y(v));
    else { ctx.moveTo(x(i + offset), y(v)); drawing = true; }
  });
  ctx.stroke();
}

window.addEventListener('resize', () => { drawCharts(); drawFillChart(); });

// --- Livello di riempimento -------------------------------------------------------------
let fill = null;        // ultimo stato inviato da Python (fill_status in main.py)
let fillHover = null;   // indice del punto evidenziato nel grafico (mouse o tastiera)
let fillGeom = null;    // geometria dell'ultimo disegno, per il tooltip

const LEVELS = {
  ok: ['Nella norma', 'ok'],
  warn: ['Quasi pieno', 'warn'],
  full: ['Pieno', 'bad'],
  unknown: ['Nessuna misura', 'off'],
};
const METHODS = { profile: 'profilo settimanale', linear: "tendenza dall'ultimo svuotamento" };
const dateTimeFmt = new Intl.DateTimeFormat('it-IT', { weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const dayFmt = new Intl.DateTimeFormat('it-IT', { weekday: 'short', day: '2-digit', month: '2-digit' });
const shortDayFmt = new Intl.DateTimeFormat('it-IT', { day: '2-digit', month: '2-digit' });
const num = (v, digits = 1) => (v === null || v === undefined ? '—'
  : Number(v).toLocaleString('it-IT', { maximumFractionDigits: digits }));

function renderFill(f) {
  fill = f;
  const last = f.last;
  const level = last ? last.level : 'unknown';
  const pct = last ? Math.max(0, Math.min(100, last.fill_pct)) : 0;

  $('fill-pct').textContent = last ? num(last.fill_pct, 0) : '—';
  const meter = $('fill-meter');
  meter.className = `bin-meter ${level}`;
  meter.setAttribute('aria-valuenow', String(Math.round(pct)));
  meter.setAttribute('aria-valuetext', last ? `${num(last.fill_pct, 0)}%, ${LEVELS[level][0]}` : 'nessuna misura');
  $('fill-meter-fill').style.height = `${pct}%`;
  $('fill-meter-warn').style.bottom = `${f.thresholds.warn}%`;
  setBadge($('fill-badge'), LEVELS[level][0], LEVELS[level][1]);
  $('fill-updated').textContent = last ? `Ultima misura: ${dateTimeFmt.format(last.ts * 1000)}` : 'Nessuna misura';
  $('fill-peak').textContent = last ? num(last.peak_pct, 0) : '—';

  const p = f.prediction;
  $('fill-rate').textContent = p ? num(p.rate_pct_day, 0) : '—';
  $('fill-method').textContent = p ? `%/giorno · ${METHODS[p.method] ?? p.method}` : '%/giorno';

  const st = f.stats;
  $('fill-at-empty').textContent = num(st.avg_fill_at_emptying_pct, 0);
  $('fill-sim').classList.toggle('hidden', !st.simulated);
  const parts = [];
  if (st.emptyings) {
    parts.push(`${st.emptyings} svuotamenti in ${num(st.days, 0)} giorni`);
    if (st.avg_interval_days) parts.push(`uno ogni ${num(st.avg_interval_days)} giorni`);
    parts.push(st.full_before_collection === 1 ? '1 volta pieno prima della raccolta'
      : `${st.full_before_collection} volte pieno prima della raccolta`);
  }
  parts.push(`${st.measurements} misure`);
  $('fill-stats').textContent = parts.join(' · ');

  renderEta();
  renderFillTable();
  drawFillChart();
}

function formatDuration(hours) {
  const totalMin = Math.round(hours * 60);
  const d = Math.floor(totalMin / 1440);
  const h = Math.floor((totalMin % 1440) / 60);
  const m = totalMin % 60;
  if (d > 0) return `${d} g ${h} h`;
  if (h > 0) return `${h} h ${m} min`;
  return `${m} min`;
}

// Il conto alla rovescia si aggiorna da solo: Python manda l'istante previsto, non le ore.
function renderEta() {
  const p = fill?.prediction;
  const last = fill?.last;
  let eta = '—';
  let when = 'servono più misure';
  if (last && last.fill_pct >= 100) {
    eta = 'Pieno';
    when = 'serve una raccolta';
  } else if (p && p.full_at === null) {
    eta = '> 30 g';
    when = 'non si riempie nel prossimo mese';
  } else if (p) {
    const hours = (p.full_at * 1000 - Date.now()) / 3.6e6;
    eta = hours > 0 ? formatDuration(hours) : 'Ora';
    when = hours > 0 ? dateTimeFmt.format(p.full_at * 1000) : 'dovrebbe essere già pieno';
  }
  $('fill-eta').textContent = eta;
  $('fill-eta-date').textContent = when;
}
setInterval(renderEta, 60000);

function renderFillTable() {
  const days = new Map();
  for (const [ts, pct, , emptied] of fill?.chart ?? []) {
    const d = new Date(ts * 1000);
    const key = d.toDateString();
    const day = days.get(key) ?? { date: d, max: 0, end: 0, emptied: false };
    day.max = Math.max(day.max, pct);
    day.end = pct;
    day.emptied ||= emptied;
    days.set(key, day);
  }
  const body = $('fill-table');
  body.replaceChildren();
  [...days.values()].reverse().forEach((day) => {
    const tr = document.createElement('tr');
    [dayFmt.format(day.date), `${num(day.max, 0)}%`, `${num(day.end, 0)}%`, day.emptied ? 'sì' : '—']
      .forEach((text) => {
        const td = document.createElement('td');
        td.textContent = text;
        tr.append(td);
      });
    body.append(tr);
  });
}

function drawFillChart() {
  const canvas = $('chart-fill');
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const pts = fill?.chart ?? [];
  const pad = { left: 44, right: 16, top: 12, bottom: 26 };
  if (pts.length < 2) {
    fillGeom = null;
    ctx.fillStyle = css('--muted');
    ctx.font = '12px system-ui';
    ctx.fillText('In attesa di misure…', pad.left, height / 2);
    return;
  }

  const p = fill.prediction;
  const t0 = pts[0][0];
  const tLast = pts[pts.length - 1][0];
  const projecting = p && p.full_at !== null && p.full_at > tLast;
  let tEnd = projecting ? p.full_at : tLast;
  tEnd += (tEnd - t0) * 0.02;
  const x = (t) => pad.left + ((t - t0) / (tEnd - t0)) * (width - pad.left - pad.right);
  const y = (v) => pad.top + (1 - v / 100) * (height - pad.top - pad.bottom);
  fillGeom = { x, y, t0, tEnd, tLast, pad, width, height, projecting };

  // Griglia: linee sottili e continue; etichette con i colori del testo.
  ctx.font = '11px system-ui';
  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const v of [0, 50, 100]) {
    ctx.strokeStyle = css('--grid');
    ctx.beginPath();
    ctx.moveTo(pad.left, Math.round(y(v)) + 0.5);
    ctx.lineTo(width - pad.right, Math.round(y(v)) + 0.5);
    ctx.stroke();
    ctx.fillStyle = css('--muted');
    ctx.fillText(`${v}%`, pad.left - 6, y(v));
  }
  // Soglia "quasi pieno"
  const warn = fill.thresholds.warn;
  ctx.strokeStyle = css('--warning');
  ctx.beginPath();
  ctx.moveTo(pad.left, Math.round(y(warn)) + 0.5);
  ctx.lineTo(width - pad.right, Math.round(y(warn)) + 0.5);
  ctx.stroke();

  // Asse dei tempi: una tacca a mezzanotte ogni 7 giorni
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const first = new Date(t0 * 1000);
  first.setHours(24, 0, 0, 0);
  for (let d = new Date(first); d.getTime() / 1000 < tEnd; d.setDate(d.getDate() + 7)) {
    const tx = x(d.getTime() / 1000);
    if (tx < pad.left + 16 || tx > width - pad.right - 16) continue;
    ctx.fillText(shortDayFmt.format(d), tx, height - pad.bottom + 6);
  }

  const color = css('--fill');
  // Velatura sotto la linea (10%)
  ctx.beginPath();
  ctx.moveTo(x(pts[0][0]), y(0));
  pts.forEach(([t, v]) => ctx.lineTo(x(t), y(v)));
  ctx.lineTo(x(tLast), y(0));
  ctx.closePath();
  ctx.globalAlpha = 0.1;
  ctx.fillStyle = color;
  ctx.fill();
  ctx.globalAlpha = 1;

  // Linea misurata (2px)
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();
  pts.forEach(([t, v], i) => (i ? ctx.lineTo(x(t), y(v)) : ctx.moveTo(x(t), y(v))));
  ctx.stroke();

  const lastV = pts[pts.length - 1][1];
  if (projecting) {
    // Previsione: tratteggio dall'ultima misura fino al 100%
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(x(tLast), y(lastV));
    ctx.lineTo(x(p.full_at), y(100));
    ctx.stroke();
    ctx.setLineDash([]);
    dot(ctx, x(p.full_at), y(100), color);
    // Etichetta solo se c'e' spazio: su schermi stretti la data resta nel riquadro "Pieno tra".
    if (width >= 560) {
      label(ctx, `pieno ${dateTimeFmt.format(p.full_at * 1000)}`, x(p.full_at) - 12, y(100),
        'right', 'middle', css('--text'));
    }
  }
  dot(ctx, x(tLast), y(lastV), color);
  // Etichetta della soglia dopo i dati, cosi' resta leggibile sopra la linea
  label(ctx, `quasi pieno (${warn}%)`, pad.left + 4, y(warn) - 2, 'left', 'bottom', css('--muted'));

  // Evidenziazione (mouse o tastiera): linea verticale sul punto piu' vicino
  if (fillHover !== null) {
    const isProj = fillHover === 'proj';
    const [t, v] = isProj ? [p.full_at, 100] : pts[fillHover];
    ctx.strokeStyle = css('--muted');
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x(t)) + 0.5, pad.top);
    ctx.lineTo(Math.round(x(t)) + 0.5, height - pad.bottom);
    ctx.stroke();
    dot(ctx, x(t), y(v), color);
  }
}

// Testo nel grafico con uno sfondo del colore della superficie, cosi' le linee non lo attraversano.
function label(ctx, text, lx, ly, align, baseline, color) {
  ctx.font = '11px system-ui';
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  const w = ctx.measureText(text).width;
  const left = align === 'right' ? lx - w : align === 'center' ? lx - w / 2 : lx;
  const top = baseline === 'bottom' ? ly - 13 : baseline === 'middle' ? ly - 7 : ly - 1;
  ctx.fillStyle = css('--surface');
  ctx.fillRect(left - 3, top, w + 6, 14);
  ctx.fillStyle = color;
  ctx.fillText(text, lx, ly);
}

function dot(ctx, cx, cy, color) {
  ctx.beginPath();
  ctx.arc(cx, cy, 6, 0, Math.PI * 2);   // anello di 2px nel colore della superficie
  ctx.fillStyle = css('--surface');
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx, cy, 4, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
}

function showFillTooltip() {
  const tip = $('fill-tooltip');
  if (fillHover === null || !fillGeom) {
    tip.classList.add('hidden');
    return;
  }
  const g = fillGeom;
  const rows = [];
  let t;
  if (fillHover === 'proj') {
    const p = fill.prediction;
    t = p.full_at;
    rows.push(['strong', 'Pieno (previsione)']);
    rows.push(['span', dateTimeFmt.format(t * 1000)]);
    rows.push(['span', `${num(p.rate_pct_day, 0)}%/giorno · ${METHODS[p.method] ?? p.method}`]);
  } else {
    const [ts, pct, peak, emptied] = fill.chart[fillHover];
    t = ts;
    rows.push(['strong', `${num(pct)}%`]);
    rows.push(['span', `picchi ${num(peak)}%${emptied ? ' · svuotato' : ''}`]);
    rows.push(['span', dateTimeFmt.format(ts * 1000)]);
  }
  tip.replaceChildren();
  rows.forEach(([tag, text], i) => {
    const line = document.createElement('div');
    if (i === 0) {
      const key = document.createElement('span');
      key.className = fillHover === 'proj' ? 'key key-dash' : 'key';
      line.append(key);
    }
    const el = document.createElement(tag);
    el.textContent = text;
    line.append(el);
    tip.append(line);
  });
  tip.classList.remove('hidden');
  const px = g.x(t);
  const left = px > g.width / 2 ? px - tip.offsetWidth - 12 : px + 12;
  tip.style.left = `${Math.max(0, left)}px`;
}

function nearestFillIndex(t) {
  const pts = fill.chart;
  let lo = 0;
  let hi = pts.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (pts[mid][0] < t) lo = mid; else hi = mid;
  }
  return t - pts[lo][0] < pts[hi][0] - t ? lo : hi;
}

function setFillHover(h) {
  fillHover = h;
  drawFillChart();
  showFillTooltip();
}

const fillCanvas = $('chart-fill');
fillCanvas.addEventListener('pointermove', (ev) => {
  if (!fillGeom) return;
  const g = fillGeom;
  const rect = fillCanvas.getBoundingClientRect();
  const px = ev.clientX - rect.left;
  const t = g.t0 + ((px - g.pad.left) / (g.width - g.pad.left - g.pad.right)) * (g.tEnd - g.t0);
  if (g.projecting && t > g.tLast + (fill.prediction.full_at - g.tLast) / 2) setFillHover('proj');
  else setFillHover(nearestFillIndex(t));
});
fillCanvas.addEventListener('pointerleave', () => setFillHover(null));
fillCanvas.addEventListener('blur', () => setFillHover(null));
fillCanvas.addEventListener('focus', () => {
  if (fill?.chart?.length) setFillHover(fill.chart.length - 1);
});
fillCanvas.addEventListener('keydown', (ev) => {
  const n = fill?.chart?.length ?? 0;
  if (!n) return;
  const cur = fillHover === null ? n - 1 : fillHover === 'proj' ? n : fillHover;
  const step = ev.shiftKey ? 24 : 1;
  const max = fillGeom?.projecting ? n : n - 1;   // l'ultimo passo e' la previsione
  let next = cur;
  if (ev.key === 'ArrowLeft') next = Math.max(0, cur - step);
  else if (ev.key === 'ArrowRight') next = Math.min(max, cur + step);
  else if (ev.key === 'Home') next = 0;
  else if (ev.key === 'End') next = max;
  else if (ev.key === 'Escape') { setFillHover(null); return; }
  else return;
  ev.preventDefault();
  setFillHover(next === n ? 'proj' : next);
});
