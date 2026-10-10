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
});

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

window.addEventListener('resize', drawCharts);
