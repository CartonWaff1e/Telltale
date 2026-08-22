/* Telltale - front end.
 * Talks to the REST endpoints exposed by python/main.py; no external libraries. */

const API = window.location.origin;
const $ = (id) => document.getElementById(id);

const STEP_LABELS = [
  'Freeze a frame to begin.',
  'Click the CENTRE of the dial.',
  'Click a point on the RIM of the dial.',
  'Click the needle tip position at the MINIMUM mark.',
  'Click the needle tip position at the MAXIMUM mark.',
  'All four points set — fill in the scale below, then Test and Save.',
];
const POINT_KEYS = ['center', 'rim', 'min_tip', 'max_tip'];
const POINT_COLORS = ['#e6e6e6', '#8a8a8a', '#bd9445', '#bc5f5b'];
const INK = {
  line: '#d0d0d0',
  grid: '#2b2b2b',
  axis: '#3a3a3a',
  text: '#6d6d6d',
  tag: '#bd9445',
  alarm: '#bc5f5b',
};
const CHANNELS = ['gauge', 'vibration', 'temperature'];
const CHANNEL_LABEL = { gauge: 'Gauge', vibration: 'Vibration', temperature: 'Temperature' };

let state = null;
let calFrame = null;
let calImage = null;
let calPoints = [];
let seriesCache = { points: [], bands: {}, unit: '' };

/* ---------------------------------------------------------------- helpers */

async function getJSON(path) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function postJSON(path, body) {
  const res = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3600);
}

const num = (v, digits = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '–' : Number(v).toFixed(digits);

function fmtHours(h) {
  if (h === null || h === undefined) return '–';
  if (h < 1) return `${(h * 60).toFixed(0)} min`;
  if (h < 48) return `${h.toFixed(1)} h`;
  return `${(h / 24).toFixed(1)} days`;
}

const fmtTime = (epoch) => new Date(epoch * 1000).toLocaleString();
const cell = (label, value) => `<div class="cell"><b>${label}</b><span>${value}</span></div>`;

function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function optionalNumber(id) {
  const raw = $(id).value.trim();
  return raw === '' ? null : Number(raw);
}

/* ---------------------------------------------------------------- preview */

function startPreview() {
  const img = $('preview');
  const fallback = $('preview-fallback');
  const tick = () => {
    img.src = `${API}/preview.jpg?t=${Date.now()}`;
  };
  img.addEventListener('load', () => {
    fallback.style.display = 'none';
    setTimeout(tick, 220);
  });
  img.addEventListener('error', () => {
    fallback.style.display = 'flex';
    setTimeout(tick, 1200);
  });
  tick();
}

/* ------------------------------------------------------------------ state */

async function pollState() {
  try {
    state = await getJSON('/state');
    $('conn-dot').className = 'dot live';
    renderState();
  } catch (err) {
    $('conn-dot').className = 'dot dead';
  } finally {
    setTimeout(pollState, 1000);
  }
}

function renderState() {
  const pill = $('status-pill');
  pill.textContent = state.status;
  pill.className = `pill ${state.status.toLowerCase()}`;

  $('asset-name').textContent = state.active_asset
    ? `${state.active_asset}${state.asset?.label ? ' · ' + state.asset.label : ''}`
    : 'no asset selected';
  $('backend-badge').textContent = state.sklearn
    ? 'IsolationForest'
    : 'robust-z (scikit-learn not installed)';

  renderSensorChips();
  renderTags();
  renderHint();
  renderChannelCards();
  renderTagOptions();
  renderSensorHelpers();
}

function renderSensorChips() {
  const s = state.sensors;
  const chip = (el, name, info) => {
    let text = `${name} –`;
    let cls = 'chip';
    if (!info.detected) {
      text = `${name} not detected`;
      cls += ' off';
    } else if (!info.reporting) {
      text = `${name} silent`;
      cls += ' warn';
    } else {
      text = `${name} ok`;
      cls += ' on';
    }
    el.textContent = text;
    el.className = cls;
  };
  chip($('chip-tof'), 'ToF', s.tof);
  chip($('chip-thermal'), 'Thermal', s.thermal);

  const tof = s.tof.latest;
  const th = s.thermal.latest;
  const buses = s.i2c_buses || {};
  const busNames = Object.keys(buses).sort();
  const busText = !s.i2c_scanned
    ? 'not scanned'
    : busNames.every((n) => !buses[n].length)
      ? 'all empty'
      : busNames.filter((n) => buses[n].length).map((n) => `${n}: ${buses[n].join(' ')}`).join('  ');
  $('sensor-kv').innerHTML = [
    cell('MCU', s.mcu_reporting ? 'reporting' : 'silent, is the sketch running?'),
    cell('I2C buses', busText),
    cell('Distance', tof ? `${num(tof.mean_mm, 0)} mm` : '–'),
    cell('Wobble RMS', tof ? `${num(tof.rms, 3)} mm` : '–'),
    cell('Wobble p-p', tof ? `${num(tof.peak_to_peak, 2)} mm` : '–'),
    cell('Dominant', tof ? `${num(tof.dominant_hz, 1)} Hz` : '–'),
    cell('Hottest', th ? `${num(th.max, 1)} °C` : '–'),
    cell('Frame mean', th ? `${num(th.mean, 1)} °C` : '–'),
    cell('Background', th ? `${num(th.min, 1)} °C` : '–'),
  ].join('');
}

function renderTags() {
  const tagList = $('tag-list');
  if (!state.tags.length) {
    tagList.innerHTML = '<span class="tagchip">no AprilTag in view</span>';
    return;
  }
  tagList.innerHTML = state.tags
    .map(
      (t) =>
        `<span class="tagchip ${t.assigned ? 'calibrated' : ''}">tag ${t.id} · ${t.edge_px}px · stable ${t.stable}/${state.stable_frames_required}${t.assigned ? ' · assigned' : ' · unassigned'}</span>`
    )
    .join('');
}

function renderHint() {
  const hint = $('capture-hint');
  if (!state.camera.ok) {
    hint.textContent = `Camera unavailable: ${state.camera.error ?? 'unknown error'}`;
  } else if (!state.tags.length) {
    hint.textContent = 'Scanning for an AprilTag.';
  } else if (!state.active_asset) {
    hint.textContent = 'Tag seen but not assigned to an asset yet — open the “Assign tag” tab.';
  } else if (state.next_capture_in_s) {
    hint.textContent = `Next automatic reading in ${state.next_capture_in_s.toFixed(0)} s. “Capture now” ignores the wait.`;
  } else {
    hint.textContent = `Locked on ${state.active_asset}. A reading is taken once the tag holds still for ${state.stable_frames_required} frames.`;
  }
}

function renderChannelCards() {
  const host = $('channel-cards');
  const channels = state.channels || {};
  const names = Object.keys(channels);
  if (!names.length) {
    host.innerHTML =
      '<p class="hint">No asset is active. Assign an AprilTag to an asset to start collecting readings.</p>';
    return;
  }

  host.innerHTML = names
    .map((name) => {
      const c = channels[name];
      const latest = c.latest;
      const a = c.assessment;
      const unit = c.limits.unit || '';
      // No assessment yet means a neutral tag, not a green one.
      const statusClass = (a?.status || '').toLowerCase();

      const header = `
        <div class="ch-head">
          <h3>${CHANNEL_LABEL[name] ?? name}</h3>
          ${c.sensor_ok ? '' : '<span class="chip off">sensor offline</span>'}
          <span class="pill ${statusClass}">${a?.status ?? 'no data'}</span>
        </div>`;

      if (!latest) {
        return `<div class="channel-card" data-status="">${header}
          <p class="hint">${escapeHTML(c.note || 'No reading stored yet.')}</p></div>`;
      }

      const detail = latest.detail || {};
      const extras = [];
      if (name === 'gauge') {
        extras.push(cell('Needle angle', `${num(detail.angle_deg, 1)}°`));
        extras.push(cell('On scale', detail.on_scale ? 'yes' : 'NO'));
      } else if (name === 'vibration') {
        extras.push(cell('Peak-to-peak', `${num(detail.peak_to_peak_mm, 2)} mm`));
        extras.push(cell('Stand-off', `${num(detail.mean_mm, 0)} mm`));
        extras.push(cell('Dominant', `${num(detail.dominant_hz, 1)} Hz`));
        extras.push(cell('Samples', detail.samples ?? '–'));
      } else if (name === 'temperature') {
        extras.push(cell('Frame max', `${num(detail.max_c, 1)} °C`));
        extras.push(cell('Frame mean', `${num(detail.mean_c, 1)} °C`));
        extras.push(cell('Background', `${num(detail.ambient_c, 1)} °C`));
        extras.push(cell('Hot spot', `x${detail.hot_x ?? '–'} y${detail.hot_y ?? '–'}`));
      }

      return `<div class="channel-card" data-status="${escapeHTML(a?.status ?? '')}">
        ${header}
        <div class="metric">${num(latest.value, 2)}<span class="unit">${escapeHTML(unit)}</span></div>
        <div class="subline">${new Date(latest.ts).toLocaleString()}${latest.valid ? '' : ' · rejected, not modelled'}</div>
        <div class="kv">
          ${cell('Range', a?.range_state ?? '–')}
          ${cell('Anomaly', a ? (a.is_anomaly ? 'YES' : 'no') : '–')}
          ${cell('Anomaly score', a && a.anomaly_score !== null ? num(a.anomaly_score, 3) : '–')}
          ${cell('Trend', a && a.trend_per_hour !== null ? `${num(a.trend_per_hour, 3)} ${unit}/h` : '–')}
          ${cell('Reaches limit in', a ? fmtHours(a.hours_to_limit) : '–')}
          ${cell('Model', a ? `${a.backend} · ${a.n_samples}${a.model_ready ? '' : ' (warming up)'}` : '–')}
          ${extras.join('')}
        </div>
        <ul class="reasons">${(a?.reasons ?? []).map((r) => `<li>${escapeHTML(r)}</li>`).join('')}</ul>
      </div>`;
    })
    .join('');
}

function renderTagOptions() {
  const select = $('f-tag-id');
  const ids = new Set(state.tags.map((t) => t.id));
  (calFrame?.tags ?? []).forEach((t) => ids.add(t.id));
  const wanted = [...ids].sort((a, b) => a - b);
  const current = select.value;
  const rendered = [...select.options].map((o) => Number(o.value));
  if (JSON.stringify(rendered) === JSON.stringify(wanted)) return;
  select.innerHTML = wanted.map((id) => `<option value="${id}">tag ${id}</option>`).join('');
  if (wanted.includes(Number(current))) select.value = current;
}

function renderSensorHelpers() {
  const tof = state.sensors.tof;
  $('vib-live').textContent = tof.reporting
    ? `Live: RMS ${num(tof.latest.rms, 3)} mm, peak-to-peak ${num(tof.latest.peak_to_peak, 2)} mm, stand-off ${num(tof.latest.mean_mm, 0)} mm, ${num(tof.latest.dominant_hz, 1)} Hz. Set the warn limit a bit above the healthy RMS.`
    : tof.detected
      ? 'The rangefinder was detected but is not reporting.'
      : 'No VL53L0X detected on the MCU I²C bus — this channel will be skipped until it is plugged in.';

  const th = state.sensors.thermal;
  $('temp-live').textContent = th.reporting
    ? `Live: hottest ${num(th.latest.max, 1)} °C, mean ${num(th.latest.mean, 1)} °C, background ${num(th.latest.min, 1)} °C.`
    : th.detected
      ? 'The thermal array was detected but is not reporting.'
      : 'No MLX90640 detected on the MCU I²C bus — this channel will be skipped until it is plugged in.';
}

/* ------------------------------------------------------------ gauge points */

async function freezeFrame() {
  try {
    const data = await getJSON('/calibration_frame');
    if (!data.ok) return toast(data.error, true);
    if (!data.tags.length) return toast('No AprilTag visible in that frame.', true);
    calFrame = data;
    calPoints = [];
    renderTagOptions();
    if (data.tags.length) $('f-tag-id').value = String(data.tags[0].id);
    calImage = new Image();
    calImage.onload = drawCalCanvas;
    calImage.src = `data:${data.image_type};base64,${data.image}`;
    updateSteps();
  } catch (err) {
    toast(`Could not freeze a frame: ${err.message}`, true);
  }
}

function selectedFrameTag() {
  if (!calFrame) return null;
  const id = Number($('f-tag-id').value);
  return calFrame.tags.find((t) => t.id === id) ?? null;
}

function drawCalCanvas() {
  if (!calImage) return;
  const canvas = $('cal-canvas');
  canvas.width = calImage.width;
  canvas.height = calImage.height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(calImage, 0, 0);

  const tag = selectedFrameTag();
  if (tag) {
    ctx.strokeStyle = INK.tag;
    ctx.lineWidth = Math.max(2, canvas.width / 400);
    ctx.beginPath();
    tag.corners.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.stroke();
  }

  calPoints.forEach(([x, y], i) => {
    const r = Math.max(5, canvas.width / 180);
    ctx.fillStyle = POINT_COLORS[i];
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#000';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });

  if (calPoints.length >= 2) drawLine(ctx, calPoints[0], calPoints[1], POINT_COLORS[1]);
  if (calPoints.length >= 3) drawLine(ctx, calPoints[0], calPoints[2], POINT_COLORS[2]);
  if (calPoints.length >= 4) drawLine(ctx, calPoints[0], calPoints[3], POINT_COLORS[3]);
}

function drawLine(ctx, a, b, color) {
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(2, ctx.canvas.width / 500);
  ctx.beginPath();
  ctx.moveTo(a[0], a[1]);
  ctx.lineTo(b[0], b[1]);
  ctx.stroke();
}

function onCanvasClick(event) {
  if (!calImage) return toast('Freeze a frame first.', true);
  if (calPoints.length >= 4) return toast('All four points are set — use “Clear points” to redo.');
  const canvas = $('cal-canvas');
  const rect = canvas.getBoundingClientRect();
  calPoints.push([
    ((event.clientX - rect.left) / rect.width) * canvas.width,
    ((event.clientY - rect.top) / rect.height) * canvas.height,
  ]);
  drawCalCanvas();
  updateSteps();
}

function updateSteps() {
  const step = calImage ? calPoints.length + 1 : 0;
  $('cal-hint').textContent = STEP_LABELS[Math.min(step, 5)];
  document.querySelectorAll('#cal-steps li').forEach((li) => {
    const n = Number(li.dataset.step);
    li.classList.toggle('done', n < step);
    li.classList.toggle('current', n === step);
  });
}

/* -------------------------------------------------------------- asset save */

function gaugePayload() {
  const tag = selectedFrameTag();
  if (!tag) {
    toast('Freeze a frame containing the selected tag before saving the gauge channel.', true);
    return null;
  }
  if (calPoints.length < 4) {
    toast('Set all four dial points first.', true);
    return null;
  }
  const points = {};
  POINT_KEYS.forEach((key, i) => (points[key] = calPoints[i]));
  return {
    enabled: true,
    tag_corners: tag.corners,
    points,
    unit: $('f-g-unit').value.trim(),
    value_min: Number($('f-g-vmin').value),
    value_max: Number($('f-g-vmax').value),
    needle_dark: $('f-g-dark').value === '1',
    r_inner_frac: Number($('f-g-rin').value),
    r_outer_frac: Number($('f-g-rout').value),
    limits: {
      unit: $('f-g-unit').value.trim(),
      warn_low: optionalNumber('f-g-wl'),
      warn_high: optionalNumber('f-g-wh'),
      alarm_low: optionalNumber('f-g-al'),
      alarm_high: optionalNumber('f-g-ah'),
    },
  };
}

function assetPayload() {
  const assetId = $('f-asset-id').value.trim();
  if (!assetId) {
    toast('Give the asset an id.', true);
    return null;
  }
  const tagId = Number($('f-tag-id').value);
  if (!Number.isFinite(tagId)) {
    toast('Pick an AprilTag id.', true);
    return null;
  }

  const payload = {
    asset_id: assetId,
    label: $('f-label').value.trim(),
    tag_id: tagId,
    gauge: null,
    vibration: null,
    temperature: null,
  };

  if ($('en-gauge').checked) {
    const gauge = gaugePayload();
    if (!gauge) return null;
    payload.gauge = gauge;
  }
  if ($('en-vibration').checked) {
    payload.vibration = {
      enabled: true,
      metric: $('f-v-metric').value,
      settle_s: Number($('f-v-settle').value),
      min_distance_mm: optionalNumber('f-v-dmin'),
      max_distance_mm: optionalNumber('f-v-dmax'),
      limits: {
        unit: 'mm',
        warn_high: optionalNumber('f-v-wh'),
        alarm_high: optionalNumber('f-v-ah'),
      },
    };
  }
  if ($('en-temperature').checked) {
    payload.temperature = {
      enabled: true,
      metric: $('f-t-metric').value,
      limits: {
        unit: $('f-t-metric').value === 'delta_ambient' ? 'K' : 'C',
        warn_low: optionalNumber('f-t-wl'),
        warn_high: optionalNumber('f-t-wh'),
        alarm_low: optionalNumber('f-t-al'),
        alarm_high: optionalNumber('f-t-ah'),
      },
    };
  }

  if (!payload.gauge && !payload.vibration && !payload.temperature) {
    toast('Enable at least one channel.', true);
    return null;
  }
  return payload;
}

async function testGauge() {
  const gauge = gaugePayload();
  if (!gauge) return;
  const box = $('cal-result');
  box.className = 'result';
  box.textContent = 'Testing…';
  try {
    const res = await postJSON('/asset/preview', { gauge });
    if (!res.ok) {
      box.className = 'result error';
      box.textContent = `Test failed: ${res.error}`;
      return;
    }
    const r = res.reading;
    box.className = `result ${r.on_scale ? 'good' : 'error'}`;
    box.innerHTML =
      `Reads <strong>${num(r.value, 2)} ${escapeHTML(gauge.unit)}</strong> at ${num(r.angle_deg, 1)}°, confidence ${num(r.confidence, 2)}` +
      (r.on_scale ? '' : ' — needle is outside the calibrated arc') +
      (r.ambiguous ? ' — more than one strong streak on the dial' : '') +
      (r.notes.length ? `<br><small>${escapeHTML(r.notes.join('; '))}</small>` : '') +
      (res.image ? `<img src="data:${res.image_type};base64,${res.image}" alt="dial" />` : '');
  } catch (err) {
    box.className = 'result error';
    box.textContent = `Test failed: ${err.message}`;
  }
}

async function saveAsset() {
  const payload = assetPayload();
  if (!payload) return;
  try {
    const res = await postJSON('/asset', payload);
    if (!res.ok) return toast(res.error, true);
    toast(`Saved ${res.asset.asset_id} — channels: ${res.asset.channels.join(', ')}`);
    loadAssets();
  } catch (err) {
    toast(`Save failed: ${err.message}`, true);
  }
}

async function loadAssets() {
  try {
    const { assets } = await getJSON('/assets');
    const list = $('asset-list');
    if (!assets.length) {
      list.innerHTML = '<div class="cal-item">No assets yet.</div>';
      return;
    }
    list.innerHTML = assets
      .map(
        (a) => `<div class="cal-item">
          <div>
            <strong>${escapeHTML(a.asset_id)}</strong> · tag ${a.tag_id} · ${a.channels.join(' + ')}
            <small>${escapeHTML(a.label || '')} updated ${escapeHTML((a.updated || '').slice(0, 19))}</small>
          </div>
          <button class="btn danger" data-delete="${escapeHTML(a.asset_id)}">Delete</button>
        </div>`
      )
      .join('');
    list.querySelectorAll('[data-delete]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        if (!confirm(`Delete asset ${btn.dataset.delete}? Stored readings are kept.`)) return;
        const res = await postJSON('/asset/delete', { asset_id: btn.dataset.delete });
        if (!res.ok) return toast(res.error, true);
        toast('Asset deleted.');
        loadAssets();
      })
    );
  } catch (err) {
    /* leave the panel as-is */
  }
}

/* ---------------------------------------------------------------- history */

async function loadHistory() {
  const hours = Number($('hist-hours').value);
  const channel = $('hist-channel').value;
  try {
    const [series, readings] = await Promise.all([
      getJSON(`/series?hours=${hours}&channel=${encodeURIComponent(channel)}`),
      getJSON(`/readings?limit=40&channel=${encodeURIComponent(channel)}`),
    ]);
    seriesCache = series;
    drawChart();
    renderReadingsTable(readings.readings);
  } catch (err) {
    toast(`Could not load history: ${err.message}`, true);
  }
}

function renderReadingsTable(rows) {
  const body = $('readings-table').querySelector('tbody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7">No readings stored yet.</td></tr>';
    return;
  }
  body.innerHTML = rows
    .map(
      (r) => `<tr>
        <td>${fmtTime(r.ts_epoch)}</td>
        <td>${escapeHTML(r.channel)}</td>
        <td class="num">${num(r.value, 2)} ${escapeHTML(r.unit || '')}</td>
        <td class="${(r.status || '').toLowerCase()}">${escapeHTML(r.status || '')}</td>
        <td class="${r.valid ? 'num' : 'flag'}">${num(r.confidence, 2)}${r.valid ? '' : ' rejected'}</td>
        <td class="${r.is_anomaly ? 'alarm' : ''}">${r.is_anomaly ? 'yes' : 'no'}</td>
        <td>${r.image_type ? `<a href="${API}/reading_image?id=${r.id}" target="_blank" rel="noopener">view</a>` : '–'}</td>
      </tr>`
    )
    .join('');
}

function drawChart() {
  const canvas = $('chart');
  const ctx = canvas.getContext('2d');
  const scale = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * scale;
  canvas.height = 220 * scale;
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  const w = canvas.width / scale;
  const h = canvas.height / scale;
  ctx.clearRect(0, 0, w, h);

  const points = (seriesCache.points || []).filter((p) => p.value !== null);
  if (points.length < 2) {
    ctx.fillStyle = INK.text;
    ctx.font = '12px ui-monospace, Menlo, Consolas, monospace';
    ctx.fillText('Not enough readings in this window yet.', 14, h / 2);
    return;
  }

  const pad = { l: 48, r: 12, t: 12, b: 24 };
  const bands = seriesCache.bands || {};
  const candidates = [
    ...points.map((p) => p.value),
    bands.warn_low, bands.warn_high, bands.alarm_low, bands.alarm_high,
  ].filter((v) => v !== null && v !== undefined && Number.isFinite(v));
  let lo = Math.min(...candidates);
  let hi = Math.max(...candidates);
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const margin = (hi - lo) * 0.1;
  lo -= margin; hi += margin;

  const t0 = points[0].ts_epoch;
  const t1 = points[points.length - 1].ts_epoch;
  const spanT = Math.max(1e-6, t1 - t0);
  const X = (t) => pad.l + ((t - t0) / spanT) * (w - pad.l - pad.r);
  const Y = (v) => h - pad.b - ((v - lo) / (hi - lo)) * (h - pad.t - pad.b);

  const band = (from, to, color) => {
    if (from === null || from === undefined || to === null || to === undefined) return;
    ctx.fillStyle = color;
    const y0 = Y(Math.max(from, to));
    const y1 = Y(Math.min(from, to));
    ctx.fillRect(pad.l, y0, w - pad.l - pad.r, y1 - y0);
  };
  band(bands.alarm_high, hi, 'rgba(188,95,91,.14)');
  band(lo, bands.alarm_low, 'rgba(188,95,91,.14)');
  band(bands.warn_high, bands.alarm_high ?? hi, 'rgba(189,148,69,.11)');
  band(bands.alarm_low ?? lo, bands.warn_low, 'rgba(189,148,69,.11)');

  ctx.strokeStyle = INK.axis;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.l, pad.t);
  ctx.lineTo(pad.l, h - pad.b);
  ctx.lineTo(w - pad.r, h - pad.b);
  ctx.stroke();

  ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    const y = Y(v);
    ctx.fillStyle = INK.text;
    ctx.fillText(v.toFixed(1), 6, y + 3);
    ctx.strokeStyle = INK.grid;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  }
  ctx.fillStyle = INK.text;
  ctx.fillText(new Date(t0 * 1000).toLocaleTimeString(), pad.l, h - 8);
  const endLabel = new Date(t1 * 1000).toLocaleTimeString();
  ctx.fillText(endLabel, w - pad.r - ctx.measureText(endLabel).width, h - 8);

  ctx.strokeStyle = INK.line;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  points.forEach((p, i) =>
    i ? ctx.lineTo(X(p.ts_epoch), Y(p.value)) : ctx.moveTo(X(p.ts_epoch), Y(p.value))
  );
  ctx.stroke();

  points.forEach((p) => {
    if (!p.is_anomaly) return;
    ctx.fillStyle = INK.alarm;
    ctx.beginPath();
    ctx.arc(X(p.ts_epoch), Y(p.value), 3.5, 0, Math.PI * 2);
    ctx.fill();
  });
}

async function loadEvents() {
  try {
    const { events } = await getJSON('/events?limit=50');
    const body = $('events-table').querySelector('tbody');
    body.innerHTML = events.length
      ? events
          .map(
            (e) => `<tr>
              <td>${fmtTime(e.ts_epoch)}</td>
              <td>${escapeHTML(e.asset_id)}</td>
              <td>${escapeHTML(e.channel)}</td>
              <td>${escapeHTML(e.severity)}</td>
              <td style="white-space:normal">${escapeHTML(e.message)}</td>
            </tr>`
          )
          .join('')
      : '<tr><td colspan="5">No events yet.</td></tr>';
  } catch (err) {
    /* leave the table as-is */
  }
}

/* ------------------------------------------------------------------- wire */

document.querySelectorAll('.tab').forEach((tab) =>
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.tabpane').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $(`tab-${tab.dataset.tab}`).classList.add('active');
    if (tab.dataset.tab === 'history') loadHistory();
    if (tab.dataset.tab === 'events') loadEvents();
    if (tab.dataset.tab === 'assign') loadAssets();
  })
);

CHANNELS.forEach((name) => {
  const box = $(`en-${name}`);
  const body = $(`body-${name}`);
  const sync = () => body.classList.toggle('open', box.checked);
  box.addEventListener('change', sync);
  sync();
});

$('btn-capture').addEventListener('click', async () => {
  try {
    await postJSON('/capture', {});
    toast('Capture requested.');
  } catch (err) {
    toast(`Capture failed: ${err.message}`, true);
  }
});

$('btn-freeze').addEventListener('click', freezeFrame);
$('btn-reset-points').addEventListener('click', () => {
  calPoints = [];
  drawCalCanvas();
  updateSteps();
});
$('f-tag-id').addEventListener('change', drawCalCanvas);
$('cal-canvas').addEventListener('click', onCanvasClick);
$('btn-test').addEventListener('click', testGauge);
$('btn-save').addEventListener('click', saveAsset);
$('btn-refresh-history').addEventListener('click', loadHistory);
$('hist-hours').addEventListener('change', loadHistory);
$('hist-channel').addEventListener('change', loadHistory);
$('btn-clear-history').addEventListener('click', async () => {
  const asset = state?.active_asset;
  const channel = $('hist-channel').value;
  if (!asset) return toast('No active asset.', true);
  if (!confirm(`Delete every stored ${channel} reading for ${asset}? That channel's model restarts from empty.`))
    return;
  const res = await postJSON('/reset_history', { asset_id: asset, channel });
  if (!res.ok) return toast(res.error, true);
  toast(`${res.deleted} reading(s) deleted.`);
  loadHistory();
});

window.addEventListener('resize', () => {
  if ($('tab-history').classList.contains('active')) drawChart();
});

startPreview();
pollState();
loadAssets();
updateSteps();
