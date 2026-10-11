// Dashboard del cassonetto: letture in tempo reale, grafici e centro di controllo simulato.
// Aspetto e colori vengono dal design system IAMAimpresa (ds/tokens.css, ds/components.css):
// qui si usano solo i token, mai colori scritti a mano.

const HISTORY_SIZE = 600;
const MAX_EVENTS = 100;

const history = [];
let restTvoc = null;

const $ = (id) => document.getElementById(id);
const fmt = (v, digits = 0) => (v === null || v === undefined ? '—' : Number(v).toFixed(digits));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const chartFont = () => `11px ${getComputedStyle(document.body).fontFamily}`;
const clampPct = (v) => Math.max(0, Math.min(100, v));

// Stati della dashboard -> toni dello StatusBadge del design system
const TONES = { ok: 'success', warn: 'warning', bad: 'destructive', off: 'muted', info: 'info' };

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
  if (data.odor) renderOdor(data.odor);
});

ui.on_message('fill', renderFill);
ui.on_message('odor', renderOdor);

function setBadge(el, text, kind) {
  el.textContent = text;
  el.className = `status status-${TONES[kind] ?? 'muted'}`;
}

// Il numero di una KpiCard si colora solo negli stati di attenzione
function setTone(el, kind) {
  el.classList.remove('tone-warning', 'tone-destructive', 'tone-success');
  const tone = TONES[kind];
  if (tone === 'warning' || tone === 'destructive') el.classList.add(`tone-${tone}`);
}

// --- Odore percepito (odor_index.py) -------------------------------------------------------
const ODOR_LEVELS = {
  green: ['Nessun odore', 'ok'],
  yellow: ['Odore percepibile', 'warn'],
  red: ['Puzza: intervento', 'bad'],
};
const REF_SOURCES = { avvio: "dall'avvio", svuotamento: 'dopo lo svuotamento', manuale: 'segnalato a mano' };

function renderOdor(o) {
  const settling = o.state === 'collecting' || o.state === 'insufficient';
  if (o.state === 'collecting') setBadge($('odor-badge'), 'Misura del riferimento…', 'warn');
  else if (o.state === 'insufficient') setBadge($('odor-badge'), 'Dati in raccolta…', 'warn');
  else if (o.level) setBadge($('odor-badge'), ...ODOR_LEVELS[o.level]);

  const demo = o.profile === 'demo';
  const level = !settling && o.level ? ODOR_LEVELS[o.level] : null;
  setTone($('kpi-odor-value'), level ? level[1] : null);
  $('odor-level-text').textContent = level ? level[0]
    : `${demo ? 'Modalità demo · ' : ''}Rispetto all'aria dopo lo svuotamento`;
  if (o.window_label) {
    document.querySelector('#odor-title .info-dot').dataset.tip =
      `Sensore fuori dal cassonetto: mediana degli ultimi ${o.window_label} rispetto all'aria misurata dopo l'ultimo svuotamento.`
      + (demo ? ` Modalità demo: finestre accorciate, soglie ${o.thresholds.yellow_ratio}× / ${o.thresholds.red_ratio}×.` : '');
  }

  $('odor-ratio').textContent = o.ratio === null ? '—' : `${fmt(o.ratio, 1)}×`;
  const persistence = o.persistence === null ? null : o.persistence * 100;
  $('odor-persistence').textContent = persistence === null ? '—' : `${fmt(persistence)}%`;
  $('odor-persistence-fill').style.width = `${persistence === null ? 0 : clampPct(persistence)}%`;
  $('odor-split').textContent = o.delta_h2 === null ? '—'
    : `${fmt(Math.exp(o.delta_h2), 1)} / ${fmt(Math.exp(o.delta_ethanol), 1)}`;

  const parts = [];
  if (o.reference) parts.push(`${REF_SOURCES[o.reference.source] ?? ''}, ${fmt(o.reference.age_h, 1)} h fa`);
  if (o.collecting) parts.push(`nuovo in misura (${fmt(o.collecting.progress * 100)}%)`);
  $('odor-reference').textContent = parts.join(' · ') || 'Nessun riferimento';
  $('odor-reset').disabled = Boolean(o.collecting);
}

$('odor-reset').addEventListener('click', () => {
  $('odor-reset').disabled = true;
  ui.send_message('mark_emptied', {});
});

// --- Letture in tempo reale e allarme gas --------------------------------------------------
const timeFmt = new Intl.DateTimeFormat('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

ui.on_message('reading', (data) => {
  history.push(data.sample);
  if (history.length > HISTORY_SIZE) history.shift();
  renderSample(data.sample);
  renderAlarm(data.alarm);
  drawCharts();
});

ui.on_message('event', addEvent);

function renderSample(s) {
  $('tvoc').textContent = fmt(s.tvoc_ppb);
  $('kpi-tvoc').textContent = fmt(s.tvoc_ppb);
  $('eco2').textContent = fmt(s.eco2_ppm);
  $('temp').textContent = fmt(s.temperature_c, 1);
  $('rh').textContent = fmt(s.humidity_pct, 1);
  // Volte l'aria di riferimento (piu' alto = piu' gas); il valore grezzo, che va al contrario, resta nel tooltip
  renderRatio($('h2'), s.h2_ratio, s.h2_raw);
  renderRatio($('ethanol'), s.ethanol_ratio, s.ethanol_raw);
  if (s.ts) $('last-update').textContent = `Lettura delle ${timeFmt.format(s.ts * 1000)}`;
  setBadge($('sensor-aht'), s.aht_ok ? 'AHT20 ok' : 'AHT20 assente', s.aht_ok ? 'ok' : 'bad');
  if (!s.sgp_ok) setBadge($('sensor-sgp'), 'SGP30 assente', 'bad');
  else if (s.sgp_warming_up) setBadge($('sensor-sgp'), 'SGP30 in riscaldamento', 'warn');
  else setBadge($('sensor-sgp'), 'SGP30 ok', 'ok');
}

function renderRatio(el, ratio, raw) {
  el.textContent = ratio === null || ratio === undefined ? '—' : `${fmt(ratio, ratio < 10 ? 1 : 0)}×`;
  el.title = raw === null || raw === undefined ? ''
    : `Segnale grezzo ${raw} (più basso = più gas)${ratio === null || ratio === undefined ? ': riferimento in misura' : ''}`;
}

function renderAlarm(a) {
  if (!a) return;
  restTvoc = a.rest_tvoc_ppb;
  const banner = $('alarm-banner');
  if (a.state === 'disabled') {
    banner.classList.add('hidden');
    setBadge($('alarm-state'), 'Allarme rapido disattivato (demo)', 'muted');
    setTone($('kpi-gas-value'), null);
    $('rest').textContent = "In demo conta l'indice di odore";
    return;
  }
  if (a.state === 'alert') {
    setBadge($('alarm-state'), 'Allarme gas', 'bad');
    $('alarm-text').textContent = `TVOC ${a.ratio}× il valore a riposo (soglia ${a.ratio_on}×). Evento inviato al centro di controllo.`;
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
    if (a.state === 'learning') setBadge($('alarm-state'), 'Apprendimento valore a riposo…', 'warn');
    else setBadge($('alarm-state'), 'Gas nella norma', 'ok');
  }
  setTone($('kpi-gas-value'), a.state === 'alert' ? 'bad' : null);
  $('rest').textContent = a.rest_tvoc_ppb === null ? 'Valore a riposo in apprendimento'
    : `A riposo ${a.rest_tvoc_ppb} ppb · ora ${a.ratio ?? '—'}×`;
}

// --- Centro di controllo -------------------------------------------------------------------
const SEVERITY = { info: ['Info', 'info'], warning: ['Avviso', 'warn'], critical: ['Critico', 'bad'] };

function textEl(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function updateEventCount() {
  const n = $('events').querySelectorAll('.evento').length;
  $('events-count').textContent = n === 1 ? '1 evento' : `${n} eventi`;
}

function renderEmptyEvents() {
  $('events').innerHTML = '<li class="empty">Nessun evento</li>';
  updateEventCount();
}

function addEvent(e) {
  const list = $('events');
  list.querySelector('.empty')?.remove();

  const [label, kind] = SEVERITY[e.severity] ?? SEVERITY.info;
  const badge = document.createElement('span');
  badge.dataset.dot = '';
  setBadge(badge, label, kind);

  const body = document.createElement('div');
  body.className = 'evento-testo';
  body.append(
    textEl('p', 'evento-titolo', e.title),
    textEl('p', 'evento-messaggio', e.message),
    textEl('p', 'evento-meta', `${e.time} · ${e.type} · ${e.bin_id}`),
  );

  const li = document.createElement('li');
  li.className = 'evento';
  li.append(badge, body);
  list.prepend(li);
  while (list.children.length > MAX_EVENTS) list.lastChild.remove();
  updateEventCount();
}

// --- Grafici (geometria del kit: tratto 2 px, riferimento tratteggiato 4-3, sfumatura 16% -> 0) --
function prepareCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function noData(ctx, text, x, y) {
  ctx.fillStyle = css('--muted-foreground');
  ctx.font = chartFont();
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x, y);
}

function hline(ctx, x0, x1, y, color, width = 1) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  ctx.moveTo(x0, Math.round(y) + 0.5);
  ctx.lineTo(x1, Math.round(y) + 0.5);
  ctx.stroke();
}

// Colore del token con trasparenza, per la sfumatura sotto la linea
function withAlpha(color, alpha) {
  const hex = color.replace('#', '');
  if (/^[0-9a-f]{3}$|^[0-9a-f]{6}$/i.test(hex)) {
    const full = hex.length === 3 ? [...hex].map((c) => c + c).join('') : hex;
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  return `color-mix(in srgb, ${color} ${alpha * 100}%, transparent)`;
}

function areaGradient(ctx, color, top, bottom) {
  const gradient = ctx.createLinearGradient(0, top, 0, bottom);
  gradient.addColorStop(0, withAlpha(color, 0.16));
  gradient.addColorStop(1, withAlpha(color, 0));
  return gradient;
}

function drawCharts() {
  drawSeries($('chart-tvoc'), history.map((s) => s.tvoc_ppb), css('--chart-1'), restTvoc);
  drawSeries($('chart-eco2'), history.map((s) => s.eco2_ppm), css('--chart-slate'), null);
}

function drawSeries(canvas, values, color, refLine) {
  const { ctx, width, height } = prepareCanvas(canvas);
  const pad = { left: 44, right: 8, top: 10, bottom: 10 };
  const valid = values.filter((v) => v !== null && v !== undefined);
  if (valid.length < 2) {
    noData(ctx, 'In attesa di dati…', pad.left, height / 2);
    return;
  }

  let max = Math.max(...valid, refLine ?? 0);
  let min = Math.min(...valid, refLine ?? Infinity);
  if (max === min) { max += 1; min = Math.max(0, min - 1); }
  const x = (i) => pad.left + (i / (HISTORY_SIZE - 1)) * (width - pad.left - pad.right);
  const y = (v) => pad.top + (1 - (v - min) / (max - min)) * (height - pad.top - pad.bottom);
  const offset = HISTORY_SIZE - values.length;

  ctx.font = chartFont();
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const v of [max, min]) {
    hline(ctx, pad.left, width - pad.right, y(v), css('--chart-grid'));
    ctx.fillStyle = css('--chart-axis');
    ctx.fillText(String(Math.round(v)), pad.left - 6, y(v));
  }

  if (refLine !== null && refLine !== undefined) {
    ctx.setLineDash([4, 3]);
    hline(ctx, pad.left, width - pad.right, y(refLine), css('--chart-reference'), 1.25);
    ctx.setLineDash([]);
  }

  // Tratti continui (i valori mancanti spezzano la linea), ciascuno con la sua sfumatura
  const segments = [];
  let current = null;
  values.forEach((v, i) => {
    if (v === null || v === undefined) { current = null; return; }
    if (!current) { current = []; segments.push(current); }
    current.push([x(i + offset), y(v)]);
  });
  const fill = areaGradient(ctx, color, pad.top, height - pad.bottom);
  for (const seg of segments) {
    if (seg.length < 2) continue;
    ctx.beginPath();
    ctx.moveTo(seg[0][0], height - pad.bottom);
    seg.forEach(([px, py]) => ctx.lineTo(px, py));
    ctx.lineTo(seg[seg.length - 1][0], height - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    seg.forEach(([px, py], i) => (i ? ctx.lineTo(px, py) : ctx.moveTo(px, py)));
    ctx.stroke();
  }
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
  const kind = LEVELS[level][1];
  const pct = last ? clampPct(last.fill_pct) : 0;

  $('fill-pct').textContent = last ? num(last.fill_pct, 0) : '—';
  setTone($('kpi-fill-value'), kind);
  $('fill-updated').textContent = last ? `Misura di ${dateTimeFmt.format(last.ts * 1000)}` : 'Nessuna misura';

  const meter = $('fill-meter');
  meter.setAttribute('aria-valuenow', String(Math.round(pct)));
  meter.setAttribute('aria-valuetext', last ? `${num(last.fill_pct, 0)}%, ${LEVELS[level][0]}` : 'nessuna misura');
  const bar = $('fill-meter-fill');
  bar.style.width = `${pct}%`;
  setTone(bar, kind);
  $('fill-meter-warn').style.left = `${f.thresholds.warn}%`;
  $('fill-meter-value').textContent = last ? `${num(last.fill_pct, 0)}%` : '—';
  setBadge($('fill-badge'), LEVELS[level][0], kind);
  $('fill-peak').textContent = last ? `${num(last.peak_pct, 0)}%` : '—';
  $('peak-meter-fill').style.width = `${last ? clampPct(last.peak_pct) : 0}%`;

  const p = f.prediction;
  $('fill-rate').textContent = p ? num(p.rate_pct_day, 0) : '—';
  $('fill-method').textContent = p ? `%/giorno · ${METHODS[p.method] ?? p.method}` : '%/giorno';

  const st = f.stats;
  $('fill-at-empty').textContent = st.avg_fill_at_emptying_pct == null ? '—' : `${num(st.avg_fill_at_emptying_pct, 0)}%`;
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
  const { ctx, width, height } = prepareCanvas(canvas);

  const pts = fill?.chart ?? [];
  const pad = { left: 44, right: 16, top: 12, bottom: 26 };
  if (pts.length < 2) {
    fillGeom = null;
    noData(ctx, 'In attesa di misure…', pad.left, height / 2);
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

  // Griglia e assi con i token chart-grid e chart-axis
  ctx.font = chartFont();
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const v of [0, 50, 100]) {
    hline(ctx, pad.left, width - pad.right, y(v), css('--chart-grid'));
    ctx.fillStyle = css('--chart-axis');
    ctx.fillText(`${v}%`, pad.left - 6, y(v));
  }
  // Soglia "quasi pieno": uno stato, quindi il token warning
  const warn = fill.thresholds.warn;
  hline(ctx, pad.left, width - pad.right, y(warn), css('--warning'), 1.25);

  // Asse dei tempi: una tacca a mezzanotte ogni 7 giorni
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.fillStyle = css('--chart-axis');
  const first = new Date(t0 * 1000);
  first.setHours(24, 0, 0, 0);
  for (let d = new Date(first); d.getTime() / 1000 < tEnd; d.setDate(d.getDate() + 7)) {
    const tx = x(d.getTime() / 1000);
    if (tx < pad.left + 16 || tx > width - pad.right - 16) continue;
    ctx.fillText(shortDayFmt.format(d), tx, height - pad.bottom + 6);
  }

  const color = css('--chart-1');
  // Sfumatura sotto la linea (16% -> 0)
  ctx.beginPath();
  ctx.moveTo(x(pts[0][0]), y(0));
  pts.forEach(([t, v]) => ctx.lineTo(x(t), y(v)));
  ctx.lineTo(x(tLast), y(0));
  ctx.closePath();
  ctx.fillStyle = areaGradient(ctx, color, pad.top, y(0));
  ctx.fill();

  // Linea misurata (2 px)
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
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(x(tLast), y(lastV));
    ctx.lineTo(x(p.full_at), y(100));
    ctx.stroke();
    ctx.setLineDash([]);
    dot(ctx, x(p.full_at), y(100), color);
    // Etichetta solo se c'e' spazio: su schermi stretti la data resta nella KPI "Pieno tra".
    if (width >= 560) {
      label(ctx, `pieno ${dateTimeFmt.format(p.full_at * 1000)}`, x(p.full_at) - 12, y(100),
        'right', 'middle', css('--foreground'));
    }
  }
  dot(ctx, x(tLast), y(lastV), color);
  // Etichetta della soglia dopo i dati, cosi' resta leggibile sopra la linea
  label(ctx, `quasi pieno (${warn}%)`, pad.left + 4, y(warn) - 2, 'left', 'bottom', css('--chart-axis'));

  // Evidenziazione (mouse o tastiera): linea verticale sul punto piu' vicino
  if (fillHover !== null) {
    const isProj = fillHover === 'proj';
    const [t, v] = isProj ? [p.full_at, 100] : pts[fillHover];
    ctx.strokeStyle = css('--chart-context');
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x(t)) + 0.5, pad.top);
    ctx.lineTo(Math.round(x(t)) + 0.5, height - pad.bottom);
    ctx.stroke();
    dot(ctx, x(t), y(v), color);
  }
}

// Testo nel grafico con uno sfondo del colore della carta, cosi' le linee non lo attraversano.
function label(ctx, text, lx, ly, align, baseline, color) {
  ctx.font = chartFont();
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  const w = ctx.measureText(text).width;
  const left = align === 'right' ? lx - w : align === 'center' ? lx - w / 2 : lx;
  const top = baseline === 'bottom' ? ly - 13 : baseline === 'middle' ? ly - 7 : ly - 1;
  ctx.fillStyle = css('--card');
  ctx.fillRect(left - 3, top, w + 6, 14);
  ctx.fillStyle = color;
  ctx.fillText(text, lx, ly);
}

// Punto attivo del kit: raggio 3,5 px con alone di 2 px nel colore della carta
function dot(ctx, cx, cy, color) {
  ctx.beginPath();
  ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
  ctx.fillStyle = css('--card');
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx, cy, 3.5, 0, Math.PI * 2);
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
      key.className = fillHover === 'proj' ? 'key key-dash key-serie' : 'key';
      line.append(key);
    }
    const node = document.createElement(tag);
    node.textContent = text;
    line.append(node);
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

// --- Tema chiaro/scuro (ThemeToggle del kit) ------------------------------------------------
const themeButton = $('theme-toggle');

function syncThemeButton() {
  const dark = document.documentElement.classList.contains('dark');
  themeButton.setAttribute('aria-label', dark ? 'Tema chiaro' : 'Tema scuro');
  themeButton.querySelector('use').setAttribute('href', dark ? '#i-sun' : '#i-moon');
}

function applyTheme(dark) {
  document.documentElement.classList.toggle('dark', dark);
  syncThemeButton();
  drawCharts();
  drawFillChart();
}

themeButton.addEventListener('click', () => {
  const dark = !document.documentElement.classList.contains('dark');
  try { localStorage.setItem('tema', dark ? 'scuro' : 'chiaro'); } catch (e) { /* solo per questa visita */ }
  applyTheme(dark);
});

matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (ev) => {
  let saved = null;
  try { saved = localStorage.getItem('tema'); } catch (e) { /* nessuna scelta salvata */ }
  if (!saved) applyTheme(ev.matches);
});
syncThemeButton();

// --- Sezione attiva nella navigazione (binario laterale e barra in basso) --------------------
const SECTIONS = ['riempimento', 'odore', 'sensori', 'eventi'];
const navLinks = [...document.querySelectorAll('[data-nav]')];

// Attiva l'ultima sezione il cui inizio ha superato il 40% dello schermo; in fondo, l'ultima.
function updateActiveNav() {
  const line = window.innerHeight * 0.4;
  let current = SECTIONS[0];
  for (const id of SECTIONS) if ($(id).getBoundingClientRect().top <= line) current = id;
  const root = document.documentElement;
  if (window.innerHeight + window.scrollY >= root.scrollHeight - 2) current = SECTIONS[SECTIONS.length - 1];
  navLinks.forEach((a) => a.setAttribute('aria-current', String(a.dataset.nav === current)));
}

window.addEventListener('scroll', updateActiveNav, { passive: true });
window.addEventListener('resize', updateActiveNav);
updateActiveNav();

// I grafici si ridisegnano quando arriva il carattere Geist, per usarlo anche nelle etichette
document.fonts?.ready.then(() => { drawCharts(); drawFillChart(); });
