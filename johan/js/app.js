// ── Johans dimensioneringsprogram — applikationslogik ────────────────────────
'use strict';

// Senaste beräkning (används av PDF-export)
let lastCalcData = null;

// ── Hjälpare ──────────────────────────────────────────────────────────────────
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];
const fmt1 = v => isFinite(v) ? v.toFixed(1) : '—';
const fmt2 = v => isFinite(v) ? v.toFixed(2) : '—';
const fmt3 = v => isFinite(v) ? v.toFixed(3) : '—';
const fmt0 = v => isFinite(v) ? Math.round(v).toString() : '—';

function fmtV(v) {
  if (!isFinite(v)) return '—';
  return v >= 10 ? fmt1(v) : fmt2(v);
}
function fmtDp(dp) {
  if (!isFinite(dp)) return '—';
  return dp >= 10 ? fmt1(dp) : dp >= 1 ? fmt2(dp) : dp.toFixed(3);
}

// ── Historik (förbättring 14 + 13) ───────────────────────────────────────────
const HIST_KEY = 'johan_dim_hist';
function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HIST_KEY) || '{}'); } catch { return {}; }
}
function saveHistory(h) {
  try { localStorage.setItem(HIST_KEY, JSON.stringify(h)); } catch {}
}
function addHistory(tab, text, result) {
  const h = loadHistory();
  if (!h[tab]) h[tab] = [];
  h[tab].unshift({ text, result, ts: Date.now() });
  h[tab] = h[tab].slice(0, 8);
  saveHistory(h);
  renderHistory(tab);
}
function renderHistory(tab) {
  const h = loadHistory();
  const list = $(`#hist-${tab}`);
  if (!list) return;
  const items = h[tab] || [];
  list.innerHTML = items.map(it =>
    `<div class="history-item" data-text="${encodeURIComponent(it.text)}">
      <span>${it.text}</span>
      <span class="hi-result">${it.result}</span>
    </div>`
  ).join('');
}
function initHistory(tab) {
  const btn = $(`#hist-btn-${tab}`);
  const list = $(`#hist-${tab}`);
  if (!btn || !list) return;
  btn.addEventListener('click', () => {
    btn.classList.toggle('open');
    list.classList.toggle('open');
  });
  renderHistory(tab);
}

// ── Kopiera resultat (förbättring 15) ─────────────────────────────────────────
function copyText(text) {
  navigator.clipboard.writeText(text).then(() => {
    // Flash feedback
    const el = document.activeElement;
    if (el) el.style.boxShadow = '0 0 0 3px #22c55a40';
    setTimeout(() => { if (el) el.style.boxShadow = ''; }, 600);
  }).catch(() => {});
}

// ── Flikbyte ──────────────────────────────────────────────────────────────────
function initTabs() {
  $$('nav.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('nav.tabs button').forEach(b => b.classList.remove('active'));
      $$('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      $(`#tab-${btn.dataset.tab}`).classList.add('active');
    });
  });
}

// ── Under-flikar (kanal) ──────────────────────────────────────────────────────
function initSubTabs(parentId) {
  const parent = document.getElementById(parentId);
  if (!parent) return;
  const btns = $$('.sub-tabs button', parent);
  btns.forEach(btn => {
    btn.addEventListener('click', () => {
      btns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      $$('.sub-section', parent).forEach(s => { s.style.display = 'none'; });
      const tgt = $(`#${btn.dataset.sub}`, parent);
      if (tgt) tgt.style.display = '';
    });
  });
}

// ── localStorage för senaste inmatning (förbättring 13) ──────────────────────
function saveInputs(formId) {
  const form = document.getElementById(formId);
  if (!form) return;
  const data = {};
  $$('input,select', form).forEach(el => { if (el.id) data[el.id] = el.value; });
  try { localStorage.setItem('form_' + formId, JSON.stringify(data)); } catch {}
}
function loadInputs(formId) {
  const form = document.getElementById(formId);
  if (!form) return;
  try {
    const data = JSON.parse(localStorage.getItem('form_' + formId) || '{}');
    Object.entries(data).forEach(([id, val]) => {
      const el = document.getElementById(id);
      if (el) el.value = val;
    });
  } catch {}
}

// ── Indikatorfärg ─────────────────────────────────────────────────────────────
function dotHtml(rating) {
  const cls = rating === 'green' ? 'green-dot' : rating === 'yellow' ? 'yellow-dot' : 'red-dot';
  return `<span class="indicator ${cls}"></span>`;
}

// ════════════════════════════════════════════════════════════════════════════════
// KANALBERÄKNING
// ════════════════════════════════════════════════════════════════════════════════
function getKanalSettings() {
  return {
    unit:    $('#k-unit').value,
    matKey:  $('#k-mat').value,
    tempAir: parseFloat($('#k-temp').value) || 20,
    aspect:  parseFloat($('#k-aspect').value) || 5,
    eps:     parseFloat($('#k-eps').value) || 0.09,
  };
}
function kanalLs(val, unit) {
  const q = sumExpr(String(val));
  if (!q) return null;
  return unit === 'm3h' ? q / 3.6 : q;
}

// Cirkulär — lista
function calcCircList() {
  const s = getKanalSettings();
  const qLs = kanalLs($('#kc-q').value, s.unit);
  if (!qLs || qLs <= 0) return;
  const maxV  = parseFloat($('#kc-maxv').value)  || 8;    // standard max 8 m/s
  const maxDp = parseFloat($('#kc-maxdp').value) || Infinity;
  const eps = s.eps;
  const rows = CIRC_DUCTS.map(d => {
    const r = calcCircDuct(qLs, d, s.tempAir, eps);
    return { d, ...r };
  }).filter(r => r.v <= maxV && r.dp <= maxDp);

  const tbody = $('#kc-list-tbody');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="5">Inga dimensioner uppfyller kraven</td></tr>'; return; }

  let recIdx = rows.length > 1 ? 1 : 0;
  tbody.innerHTML = rows.map((r, i) => {
    const rating = getRating(r.v, r.dp, 'duct_supply');
    const cls = i === recIdx ? 'recommended' : '';
    return `<tr class="${cls}">
      <td>Ø${r.d}</td>
      <td>${dotHtml(rating)}${fmtV(r.v)}</td>
      <td>${fmtDp(r.dp)}</td>
      <td><span class="${r.regime.cls}">${r.regime.text}</span></td>
      <td>${fmt0(r.re)}</td>
    </tr>`;
  }).join('');

  const rec = rows[recIdx];
  lastCalcData = {
    type: 'circ_duct',
    inputs: [
      ['Luftmängd', `${fmt2(qLs)} l/s  (${fmt1(qLs*3.6)} m³/h)`],
      ['Kanaltyp / ytråhet', `${s.matKey}  (ε = ${s.eps} mm)`],
      ['Lufttemperatur', `${s.tempAir} °C`],
      ['Max hastighet', `${maxV} m/s`],
    ],
    results: [],
    tableHeaders: ['Dimension (mm)', 'Hastighet (m/s)', 'Tryckfall (Pa/m)', 'Strömningsregim', 'Re-tal'],
    table: rows.map((r, i) => ({
      rec: i === recIdx,
      cells: [`Ø${r.d}`, fmtV(r.v), fmtDp(r.dp), r.regime.text, fmt0(r.re)],
    })),
  };
  addHistory('kanal', `Ø${rec.d}mm, ${fmt2(qLs)} l/s`, `${fmtV(rec.v)} m/s, ${fmtDp(rec.dp)} Pa/m`);
  saveInputs('form-kanal-circ');
}

// Cirkulär — given dimension
function calcCircDim() {
  const s = getKanalSettings();
  const qLs = kanalLs($('#kc2-q').value, s.unit);
  const d = parseFloat($('#kc2-d').value);
  if (!qLs || !d) return;
  const r = calcCircDuct(qLs, d, s.tempAir, s.eps);
  showResult('kc2-result', [
    ['Hastighet', `${fmtV(r.v)} m/s`, getRating(r.v, r.dp, 'duct_supply')],
    ['Tryckfall/m', `${fmtDp(r.dp)} Pa/m`, getRating(r.v, r.dp, 'duct_supply')],
    ['Reynolds', fmt0(r.re)],
    ['Strömning', r.regime.text],
  ]);
  lastCalcData = {
    type: 'circ_duct',
    inputs: [
      ['Luftmängd', `${fmt2(qLs)} l/s  (${fmt1(qLs*3.6)} m³/h)`],
      ['Diameter', `Ø${d} mm`],
      ['Kanaltyp / ytråhet', `${s.matKey}  (ε = ${s.eps} mm)`],
      ['Lufttemperatur', `${s.tempAir} °C`],
    ],
    results: [
      ['Hastighet', `${fmtV(r.v)} m/s`],
      ['Tryckfall per meter', `${fmtDp(r.dp)} Pa/m`],
      ['Strömningsregim', r.regime.text],
      ['Reynolds-tal', fmt0(r.re)],
      ['Friktionsfaktor λ', r.lam.toFixed(4)],
    ],
  };
  addHistory('kanal', `Ø${d}mm, ${fmt2(qLs)} l/s`, `${fmtV(r.v)} m/s, ${fmtDp(r.dp)} Pa/m`);
  saveInputs('form-kanal-circ2');
}

// Cirkulär — max flöde
function calcCircMax() {
  const s = getKanalSettings();
  const d = parseFloat($('#kc3-d').value);
  const maxV  = parseFloat($('#kc3-maxv').value)  || 0;
  const maxDp = parseFloat($('#kc3-maxdp').value) || 0;
  if (!d || (!maxV && !maxDp)) return;
  let qLs;
  if (maxV > 0) {
    const A = Math.PI * (d/1000)**2 / 4;
    qLs = maxV * A * 1000;
  } else {
    qLs = findAirFlowForDp(maxDp, d, s.tempAir, s.eps);
  }
  const r = calcCircDuct(qLs, d, s.tempAir, s.eps);
  const disp = s.unit === 'm3h' ? `${fmt1(qLs*3.6)} m³/h` : `${fmt2(qLs)} l/s`;
  showResult('kc3-result', [
    ['Max luftflöde', disp],
    ['Hastighet', `${fmtV(r.v)} m/s`],
    ['Tryckfall/m', `${fmtDp(r.dp)} Pa/m`],
  ]);
}

// Rektangulär — lista
function calcRectList() {
  const s = getKanalSettings();
  const qLs = kanalLs($('#kr-q').value, s.unit);
  if (!qLs || qLs <= 0) return;
  const maxV  = parseFloat($('#kr-maxv').value)  || 8;    // standard max 8 m/s
  const maxDp = parseFloat($('#kr-maxdp').value) || Infinity;
  const maxAspect = s.aspect;

  const rows = [];
  for (const a of RECT_SIDES) {
    for (const b of RECT_SIDES) {
      if (b < a) continue;
      if (b/a > maxAspect) continue;
      const r = calcRectDuct(qLs, a, b, s.tempAir, s.eps);
      if (r.v <= maxV && r.dp <= maxDp) {
        rows.push({ a, b, ...r });
      }
    }
  }
  rows.sort((x,y) => x.a*x.b - y.a*y.b);

  const tbody = $('#kr-list-tbody');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="5">Inga dimensioner uppfyller kraven</td></tr>'; return; }
  // Rekommenderad = näst minst area (undviker den allra minsta med hög hastighet)
  let recIdx = rows.length > 1 ? 1 : 0;
  tbody.innerHTML = rows.map((r, i) => {
    const rating = getRating(r.v, r.dp, 'duct_supply');
    const cls = i === recIdx ? 'recommended' : '';
    return `<tr class="${cls}">
      <td>${r.a}×${r.b}</td>
      <td>${dotHtml(rating)}${fmtV(r.v)}</td>
      <td>${fmtDp(r.dp)}</td>
      <td><span class="${r.regime.cls}">${r.regime.text}</span></td>
      <td>${fmt1(r.Dh)}</td>
    </tr>`;
  }).join('');
  const rec = rows[recIdx];
  lastCalcData = {
    type: 'rect_duct',
    inputs: [
      ['Luftmängd', `${fmt2(qLs)} l/s  (${fmt1(qLs*3.6)} m³/h)`],
      ['Kanaltyp / ytråhet', `${s.matKey}  (ε = ${s.eps} mm)`],
      ['Lufttemperatur', `${s.tempAir} °C`],
      ['Max hastighet', `${maxV} m/s`],
      ['Max sidoförhållande', `1:${maxAspect}`],
    ],
    results: [],
    tableHeaders: ['Dim A×B (mm)', 'Hastighet (m/s)', 'Tryckfall (Pa/m)', 'Strömningsregim', 'Dh (mm)'],
    table: rows.map((r, i) => ({
      rec: i === recIdx,
      cells: [`${r.a}×${r.b}`, fmtV(r.v), fmtDp(r.dp), r.regime.text, fmt1(r.Dh)],
    })),
  };
  addHistory('kanal', `${rec.a}×${rec.b}mm, ${fmt2(qLs)} l/s`, `${fmtV(rec.v)} m/s, ${fmtDp(rec.dp)} Pa/m`);
  saveInputs('form-kanal-rect');
}

// Rektangulär — given dimension
function calcRectDim() {
  const s = getKanalSettings();
  const qLs = kanalLs($('#kr2-q').value, s.unit);
  const a = parseFloat($('#kr2-a').value);
  const b = parseFloat($('#kr2-b').value);
  if (!qLs || !a || !b) return;
  const r = calcRectDuct(qLs, a, b, s.tempAir, s.eps);
  showResult('kr2-result', [
    ['Hastighet', `${fmtV(r.v)} m/s`, getRating(r.v, r.dp, 'duct_supply')],
    ['Tryckfall/m', `${fmtDp(r.dp)} Pa/m`, getRating(r.v, r.dp, 'duct_supply')],
    ['Hydraulisk diameter', `${fmt1(r.Dh)} mm`],
    ['Reynolds', fmt0(r.re)],
    ['Strömning', r.regime.text],
  ]);
  lastCalcData = {
    type: 'rect_duct',
    inputs: [
      ['Luftmängd', `${fmt2(qLs)} l/s  (${fmt1(qLs*3.6)} m³/h)`],
      ['Dimension A×B', `${a} × ${b} mm`],
      ['Kanaltyp / ytråhet', `${s.matKey}  (ε = ${s.eps} mm)`],
      ['Lufttemperatur', `${s.tempAir} °C`],
    ],
    results: [
      ['Hastighet', `${fmtV(r.v)} m/s`],
      ['Tryckfall per meter', `${fmtDp(r.dp)} Pa/m`],
      ['Hydraulisk diameter', `${fmt1(r.Dh)} mm`],
      ['Strömningsregim', r.regime.text],
      ['Reynolds-tal', fmt0(r.re)],
      ['Friktionsfaktor λ', r.lam.toFixed(4)],
    ],
  };
  addHistory('kanal', `${a}×${b}mm, ${fmt2(qLs)} l/s`, `${fmtV(r.v)} m/s, ${fmtDp(r.dp)} Pa/m`);
  saveInputs('form-kanal-rect2');
}

// ── Kanälinställningar: uppdatera eps från material ───────────────────────────
function updateKanalEps() {
  const matKey = $('#k-mat').value;
  const mat = DUCT_MATS[matKey];
  const epsField = $('#k-eps');
  if (mat && mat.eps !== null) {
    epsField.value = mat.eps;
    epsField.readOnly = true;
  } else {
    epsField.readOnly = false;
  }
}

// ════════════════════════════════════════════════════════════════════════════════
// RÖRBERÄKNING
// ════════════════════════════════════════════════════════════════════════════════
let currentRorDN = null;
let currentRorMat = null;

function updateRorMaterial() {
  const mat = $('#r-mat').value;
  currentRorMat = mat;
  const info = PIPE_MATS[mat];
  const infoEl = $('#r-mat-info');
  if (info) {
    infoEl.innerHTML = `<strong>${mat}</strong> — Ytråhet ε = ${info.eps} mm &nbsp;|&nbsp; Livslängd: ${info.life}<br>${info.note}`;
    infoEl.classList.add('visible');
  }
  // Bygg DN-chips
  const dnContainer = $('#r-dn-row');
  const table = DN_TABLES[mat] || {};
  dnContainer.innerHTML = Object.keys(table).map(dn =>
    `<div class="dn-chip" data-dn="${dn}" data-id="${table[dn].id}" data-od="${table[dn].od}">DN${dn}</div>`
  ).join('');
  $$('.dn-chip', dnContainer).forEach(chip => {
    chip.addEventListener('click', () => {
      $$('.dn-chip', dnContainer).forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      currentRorDN = { dn: chip.dataset.dn, id: parseFloat(chip.dataset.id), od: parseFloat(chip.dataset.od) };
      $('#r-id').value = chip.dataset.id;
      const odEl = $('#r-od-display');
      if (odEl) odEl.textContent = `YD: ${chip.dataset.od} mm`;
    });
  });
  // Välj första automatiskt
  const first = $('.dn-chip', dnContainer);
  if (first) first.click();
}

function getRorFluidProps() {
  const T = parseFloat($('#r-temp').value) || 20;
  const medium = $('#r-medium').value;
  if (medium === 'custom') {
    const nu = parseFloat($('#r-nu').value) || 0;
    const rho = parseFloat($('#r-rho').value) || 0;
    if (!nu || !rho) return null;
    return { nu: nu * 1e-6, rho };
  }
  return getFluidProps(medium, T);
}

function calcRorDim() {
  const id = parseFloat($('#r-id').value);
  const qLs = parseFloat($('#r-q').value);
  if (!id || !qLs) return;
  const mat = PIPE_MATS[currentRorMat || $('#r-mat').value];
  if (!mat) return;
  const fp = getRorFluidProps();
  if (!fp) return;
  const r = calcPipe(qLs, id, mat.eps, fp);
  const T = parseFloat($('#r-temp').value) || 20;
  const medium = $('#r-medium').value;
  const copyStr = `DN${currentRorDN?.dn||'?'} ${currentRorMat||'?'}, ${qLs} l/s → ${fmtV(r.v)} m/s, ${fmtDp(r.dp)} Pa/m`;
  showResult('r-result', [
    ['Hastighet', `${fmtV(r.v)} m/s`, getRating(r.v, r.dp, 'pipe_heat')],
    ['Tryckfall/m', `${fmtDp(r.dp)} Pa/m`, getRating(r.v, r.dp, 'pipe_heat')],
    ['Reynolds', fmt0(r.re)],
    ['Strömning', r.regime.text, r.regime.cls === 'regime-lam' ? 'green' : 'neutral'],
    ['Friktionsfaktor λ', r.lam.toFixed(4)],
  ], copyStr);
  lastCalcData = {
    type: 'pipe',
    inputs: [
      ['Material', `${currentRorMat||'?'}  (ε = ${mat.eps} mm)`],
      ['Dimension', `DN${currentRorDN?.dn||'?'}  (id = ${id} mm, yd = ${currentRorDN?.od||'?'} mm)`],
      ['Medium', `${medium}  vid ${T} °C`],
      ['Flöde', `${qLs} l/s  (${fmt1(qLs*3.6)} m³/h)`],
    ],
    results: [
      ['Hastighet', `${fmtV(r.v)} m/s`],
      ['Tryckfall per meter', `${fmtDp(r.dp)} Pa/m`],
      ['Strömningsregim', r.regime.text],
      ['Reynolds-tal', fmt0(r.re)],
      ['Friktionsfaktor λ', r.lam.toFixed(4)],
      ['Densitet ρ', `${fmt1(fp.rho)} kg/m³`],
    ],
  };
  addHistory('ror', `DN${currentRorDN?.dn||id}mm, ${qLs} l/s`, `${fmtV(r.v)} m/s, ${fmtDp(r.dp)} Pa/m`);
  calcHeatLoss();
  saveInputs('form-ror');
}

// Dimensionslista för rör (förbättring 6)
function calcRorList() {
  const qLs = parseFloat($('#r-q').value);
  const mat = PIPE_MATS[currentRorMat || $('#r-mat').value];
  if (!qLs || !mat) return;
  const fp = getRorFluidProps();
  if (!fp) return;
  const maxV  = parseFloat($('#r-maxv').value)  || Infinity;
  const maxDp = parseFloat($('#r-maxdp').value) || Infinity;
  const table = DN_TABLES[currentRorMat || $('#r-mat').value] || {};
  const rows = Object.entries(table).map(([dn, {id, od}]) => {
    const r = calcPipe(qLs, id, mat.eps, fp);
    return { dn, id, od, ...r };
  }).filter(r => r.v <= maxV && r.dp <= maxDp);

  const tbody = $('#r-list-tbody');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="5">Inga dimensioner uppfyller kraven</td></tr>'; return; }
  let recIdx = rows.length > 1 ? 1 : 0;
  tbody.innerHTML = rows.map((r, i) => {
    const rating = getRating(r.v, r.dp, 'pipe_heat');
    const cls = i === recIdx ? 'recommended' : '';
    return `<tr class="${cls}">
      <td>DN${r.dn}</td>
      <td>${dotHtml(rating)}${fmtV(r.v)}</td>
      <td>${fmtDp(r.dp)}</td>
      <td><span class="${r.regime.cls}">${r.regime.text}</span></td>
      <td>${fmt0(r.re)}</td>
    </tr>`;
  }).join('');
}

// Max flöde (sök l/s)
function calcRorMaxQ() {
  const id = parseFloat($('#r-id').value);
  const mat = PIPE_MATS[currentRorMat || $('#r-mat').value];
  if (!id || !mat) return;
  const fp = getRorFluidProps();
  if (!fp) return;
  const maxV  = parseFloat($('#r-maxv').value)  || 0;
  const maxDp = parseFloat($('#r-maxdp').value) || 0;
  let qLs;
  if (maxV > 0)  qLs = findFlowForV(maxV, id);
  else if (maxDp > 0) qLs = findFlowForDp(maxDp, id, mat.eps, fp);
  else return;
  const r = calcPipe(qLs, id, mat.eps, fp);
  showResult('r-result', [
    ['Max flöde', `${fmt2(qLs)} l/s  (${fmt1(qLs*3.6)} m³/h)`],
    ['Hastighet', `${fmtV(r.v)} m/s`],
    ['Tryckfall/m', `${fmtDp(r.dp)} Pa/m`],
    ['Reynolds', fmt0(r.re)],
  ]);
}

// Värmeförlust (förbättring 9)
function calcHeatLoss() {
  const od = currentRorDN?.od;
  if (!od) return;
  const Tf = parseFloat($('#r-temp').value) || 20;
  const Ta = parseFloat($('#r-tamb').value) || 20;
  if (Math.abs(Tf - Ta) < 0.5) return;
  const insThick = parseFloat($('#r-ins-thick').value) || 0;
  const insType  = $('#r-ins-type').value;
  const lambda   = insThick > 0 ? (INSUL_TYPES[insType]?.lambda || 0.040) : 0;

  let ql;
  if (insThick > 0) {
    ql = heatLossInsulated(Tf, Ta, od, insThick, lambda);
  } else {
    ql = heatLossUninsulated(Tf, Ta, od);
  }
  if (ql === null) return;
  $('#r-heatloss-val').textContent = `${fmt1(ql)} W/m`;
  $('#r-heatloss-block').classList.remove('hidden');
}

// Hantera medium-visning (glykol)
function updateRorMedium() {
  const med = $('#r-medium').value;
  const customRow = $('#r-glycol-custom');
  customRow.classList.toggle('hidden', med !== 'custom');
}

// ════════════════════════════════════════════════════════════════════════════════
// Kv-BERÄKNING
// ════════════════════════════════════════════════════════════════════════════════
function calcKv() {
  const seek   = $('input[name="kv-seek"]:checked')?.value;
  const feMode = $('#kv-fe-mode').checked;
  const unit_q = $('#kv-unit-q').value;
  const unit_p = $('#kv-unit-p').value;

  function getQ() {
    const v = parseFloat($('#kv-q').value);
    return unit_q === 'ls' ? v * 3.6 : v;  // → m³/h
  }
  function getP() {
    const v = parseFloat($('#kv-p').value);
    return unit_p === 'kpa' ? v/100 : unit_p === 'pa' ? v/100000 : v;  // → bar
  }
  function getP_mH2O() {
    const v = parseFloat($('#kv-p').value);
    return unit_p === 'mh2o' ? v : unit_p === 'kpa' ? v/9.807 : unit_p === 'pa' ? v/9807 : v*10.2;
  }

  if (feMode) {
    const Fe   = parseFloat($('#kv-fe').value);
    const d_mm = parseFloat($('#kv-fe-d').value);
    const Q_m3h = seek === 'q' ? null : getQ();
    const dp_mH2O = seek === 'p' ? null : (unit_p === 'mh2o' ? parseFloat($('#kv-p').value) : getP()*10.2);

    if (!Fe || !d_mm) return;
    if (seek === 'q') {
      if (!dp_mH2O) return;
      const r = calcFe({ Fe, d_mm, dp_mH2O });
      const Kv = feToKvCorr(Fe, d_mm);
      const disp = unit_q === 'ls' ? `${fmt2(r.Q_m3h/3.6)} l/s` : `${fmt2(r.Q_m3h)} m³/h`;
      showResult('kv-result', [
        ['Flöde', disp],
        ['Kv (omräknat)', fmt3(Kv)],
        ['Fe', Fe.toFixed(3)],
      ]);
    } else if (seek === 'p') {
      if (!Q_m3h) return;
      const r = calcFe({ Fe, d_mm, Q_m3h });
      showResult('kv-result', [
        ['Tryckfall', `${fmt2(r.dp_mH2O)} mH₂O`],
        ['Fe', Fe.toFixed(3)],
      ]);
    } else {  // kv-mode: beräkna Kv
      const Kv = feToKvCorr(Fe, d_mm);
      showResult('kv-result', [['Kv (från Fe)', fmt3(Kv)]]);
    }
    return;
  }

  // Standard Kv-beräkning
  if (seek === 'kv') {
    const Q_m3h = getQ(), dp_bar = getP();
    if (!Q_m3h || !dp_bar || dp_bar <= 0) return;
    const r = kvCalc({ Q_m3h, dp_bar });
    showResult('kv-result', [
      ['Kv', fmt3(r.Kv)],
      ['Kvs (minst)', fmt3(r.Kv * 1.25)],
    ]);
    addHistory('kv', `Q=${fmt2(Q_m3h)}m³/h, ΔP=${fmt2(dp_bar)}bar`, `Kv=${fmt3(r.Kv)}`);
  } else if (seek === 'q') {
    const Kv = parseFloat($('#kv-kv').value), dp_bar = getP();
    if (!Kv || !dp_bar || dp_bar <= 0) return;
    const r = kvCalc({ Kv, dp_bar });
    const disp = unit_q === 'ls' ? `${fmt2(r.Q_ls)} l/s` : `${fmt2(r.Q_m3h)} m³/h`;
    showResult('kv-result', [['Flöde', disp]]);
    addHistory('kv', `Kv=${Kv}, ΔP=${fmt2(dp_bar)}bar`, `Q=${disp}`);
  } else {
    const Kv = parseFloat($('#kv-kv').value), Q_m3h = getQ();
    if (!Kv || !Q_m3h) return;
    const r = kvCalc({ Kv, Q_m3h });
    const dp_disp = unit_p === 'kpa' ? `${fmt1(r.dp_bar*100)} kPa` :
                    unit_p === 'pa'  ? `${fmt0(r.dp_bar*100000)} Pa` :
                                       `${fmt3(r.dp_bar)} bar`;
    showResult('kv-result', [['Tryckfall', dp_disp]]);
    addHistory('kv', `Kv=${Kv}, Q=${fmt2(Q_m3h)}m³/h`, `ΔP=${dp_disp}`);
  }
  saveInputs('form-kv');
}

// Fe-mode toggle
function toggleFe() {
  const fe = $('#kv-fe-mode').checked;
  $$('.kv-fe-field').forEach(el => el.classList.toggle('hidden', !fe));
  $$('.kv-std-field').forEach(el => el.classList.toggle('hidden', fe));
}

// ════════════════════════════════════════════════════════════════════════════════
// EFFEKT / FLÖDE
// ════════════════════════════════════════════════════════════════════════════════
function calcEffektTab() {
  const seek   = $('input[name="eff-seek"]:checked')?.value;
  const P_raw  = parseFloat($('#eff-p').value);
  const Q_raw  = parseFloat($('#eff-q').value);
  const dT     = parseFloat($('#eff-dt').value) || 0;
  const unitP  = $('#eff-unit-p').value;
  const unitQ  = $('#eff-unit-q').value;
  const T      = parseFloat($('#eff-temp').value) || 20;
  const cpCustom = parseFloat($('#eff-cp').value) || 4186;

  if (!dT || dT <= 0) return;
  const rho = waterDensity(T);

  if (seek === 'p') {
    const Q_ls = unitQ === 'lh' ? Q_raw / 3600 : Q_raw;
    if (!Q_ls) return;
    const r = calcEffekt({ Q_ls, dT, cp: cpCustom, rho });
    const disp = unitP === 'kw' ? `${fmt1(r.P_kW)} kW` : `${fmt0(r.P_W)} W`;
    showResult('eff-result', [
      ['Effekt', disp],
      ['Massflöde', `${fmt2(Q_ls*rho/1000)} kg/s`],
    ]);
    lastCalcData = {
      type: 'effekt',
      inputs: [
        ['Flöde', `${fmt2(Q_raw)} ${unitQ}  (${fmt2(Q_ls)} l/s)`],
        ['Temperaturskillnad ΔT', `${dT} °C`],
        ['Mediumtemperatur', `${T} °C`],
        ['Spec. värmekapacitet cp', `${cpCustom} J/(kg·K)`],
        ['Densitet ρ(T)', `${fmt1(rho)} kg/m³`],
      ],
      results: [
        ['Värmeeffekt', disp],
        ['Massflöde', `${fmt2(Q_ls*rho/1000)} kg/s`],
        ['Energiflöde', `${fmt1(Q_ls*rho/1000*cpCustom*dT/1000)} kJ/s`],
      ],
    };
    addHistory('effekt', `Q=${fmt2(Q_raw)}${unitQ}, ΔT=${dT}°C`, disp);
  } else {
    const P_W = unitP === 'kw' ? P_raw*1000 : P_raw;
    if (!P_W) return;
    const r = calcEffekt({ P_W, dT, cp: cpCustom, rho });
    const disp = unitQ === 'lh' ? `${fmt1(r.Q_ls*3600)} l/h` : `${fmt2(r.Q_ls)} l/s`;
    showResult('eff-result', [
      ['Flöde', disp],
      ['Massflöde', `${fmt2(r.Q_ls*rho/1000)} kg/s`],
    ]);
    lastCalcData = {
      type: 'effekt',
      inputs: [
        ['Effekt', `${fmt1(P_raw)} ${unitP}  (${fmt0(P_W)} W)`],
        ['Temperaturskillnad ΔT', `${dT} °C`],
        ['Mediumtemperatur', `${T} °C`],
        ['Spec. värmekapacitet cp', `${cpCustom} J/(kg·K)`],
        ['Densitet ρ(T)', `${fmt1(rho)} kg/m³`],
      ],
      results: [
        ['Flöde', disp],
        ['Massflöde', `${fmt2(r.Q_ls*rho/1000)} kg/s`],
      ],
    };
    addHistory('effekt', `P=${fmt1(P_raw)}${unitP}, ΔT=${dT}°C`, disp);
  }
  saveInputs('form-effekt');
}

// ΔT-förinställningar
function setDT(val) { $('#eff-dt').value = val; calcEffektTab(); }

// ════════════════════════════════════════════════════════════════════════════════
// PDF-EXPORT
// ════════════════════════════════════════════════════════════════════════════════
const FORMULAS = {
  circ_duct: {
    title: 'Kanalberäkning — Cirkulär kanal',
    method: 'Darcy-Weisbach med Churchill (1977) friktionsfaktor',
    lines: [
      'Genomströmningsarea:  A = π × D² / 4',
      'Hastighet:            v = Q / A                              [m/s]',
      'Tryckfall per meter:  ΔP/L = λ × ρ × v² / (2 × D)         [Pa/m]',
      'Friktionsfaktor (Churchill 1977, explicit för alla Re-tal):',
      '  Laminärt Re < 2300: λ = 64 / Re',
      '  Turbulent:          λ = 8 × [ (8/Re)¹² + (A+B)^(−3/2) ]^(1/12)',
      '    A = [−2,457 × ln((7/Re)^0,9 + 0,27×ε/D)]^16',
      '    B = (37530/Re)^16',
      'Reynolds-tal:         Re = v × D / ν',
      'Luftdensitet:         ρ = 353,05 / (273,15 + T)             [kg/m³]',
      'Kinematisk viskositet: Sutherland-approximation (Tk-beroende)',
    ],
    standard: 'EN 1505 (dimensioner), EN 12237 (kanalstyrka)',
  },
  rect_duct: {
    title: 'Kanalberäkning — Rektangulär kanal',
    method: 'Darcy-Weisbach med hydraulisk diameter (Dh)',
    lines: [
      'Genomströmningsarea:  A = a × b',
      'Hydraulisk diameter:  Dh = 2 × a × b / (a + b)             [m]',
      'Hastighet:            v = Q / A                              [m/s]',
      'Tryckfall per meter:  ΔP/L = λ × ρ × v² / (2 × Dh)        [Pa/m]',
      'Friktionsfaktor:      Churchill (1977) — se cirkulär kanal',
      'Reynolds-tal:         Re = v × Dh / ν',
      'Not: Rektangulär kanal ger ca 5–15% högre tryckfall än',
      '     ekvivalent cirkulär kanal vid samma area.',
    ],
    standard: 'EN 1505 (dimensioner), EN 13403 (icke-metalliska kanaler)',
  },
  pipe: {
    title: 'Rörberäkning',
    method: 'Darcy-Weisbach med temperaturkorrigerade fluidegenskaper',
    lines: [
      'Genomströmningsarea:  A = π × d² / 4                        [m²]',
      'Hastighet:            v = Q / A                              [m/s]',
      'Tryckfall per meter:  ΔP/L = λ × ρ × v² / (2 × d)         [Pa/m]',
      'Friktionsfaktor:      Churchill (1977) — explicit för alla Re-tal',
      'Reynolds-tal:         Re = v × d / ν',
      'Vatten ρ(T):          Polynomapproximation (IAPWS-IF97)',
      'Vatten ν(T):          Vogel-ekvation: μ = 2,414×10⁻⁵ × 10^(247,8/(T+133,15))',
      'Glykol (EG/PG):       Tabellinterpolation vid 20, 30, 40, 50 vol%',
    ],
    standard: 'EN 1057 (koppar), EN 10255 (stål), EN ISO 21003 (AluPEX)',
  },
  kv: {
    title: 'Kv-beräkning',
    method: 'Ventilflödeskoefficient enligt IEC 60534',
    lines: [
      'Flöde:                Q [m³/h] = Kv × √(ΔP [bar])',
      'Kv ur flöde/tryck:    Kv = Q / √(ΔP)',
      'Tryckfall:            ΔP [bar] = (Q / Kv)²',
      'Fe → Kv omvandling:   Kv = Fe × d² × 0,000313     (d i mm)',
      'Fe-formel (äldre):    Q [m³/h] = Fe × d² × √(ΔP [mH₂O])',
      'Enhetssamband:        1 bar = 10,197 mH₂O  |  1 mH₂O = 0,09807 bar',
      'Kvs-rekommendation:   Välj ventil med Kvs ≥ 1,25 × Kv (25% marginal)',
    ],
    standard: 'IEC 60534-1, EN 1267, SS-EN 215',
  },
  effekt: {
    title: 'Effekt- och flödesberäkning',
    method: 'Termodynamisk energibalans',
    lines: [
      'Värmeeffekt:          P = ṁ × cp × ΔT                       [W]',
      'Massflöde:            ṁ = Q × ρ                             [kg/s]',
      'Volymsflöde:          Q_ls [l/s] → Q_m3s = Q / 1000        [m³/s]',
      'Vattendensitet:       ρ(T) polynomapproximation (IAPWS)',
      'Spec. värmekapacitet: cp anges av användaren',
      '                      Vatten vid 60°C ≈ 4185 J/(kg·K)',
      '                      EG 30% vid 40°C ≈ 3900 J/(kg·K)',
      'Omvändning:           Q = P / (ρ × cp × ΔT)',
    ],
    standard: 'SS-EN ISO 9346, BBR (Boverkets Byggregler)',
  },
};

function getReportMeta() {
  return {
    company:   localStorage.getItem('meta_company')   || '',
    project:   localStorage.getItem('meta_project')   || '',
    ref:       localStorage.getItem('meta_ref')       || '',
    prepBy:    localStorage.getItem('meta_prepby')    || '',
    checkedBy: localStorage.getItem('meta_checkedby') || '',
  };
}
function saveReportMeta() {
  [['company','meta-company'],['project','meta-project'],['ref','meta-ref'],
   ['prepby','meta-prepby'],['checkedby','meta-checkedby']].forEach(([k, id]) => {
    const el = document.getElementById(id);
    if (el) localStorage.setItem('meta_' + k, el.value);
  });
}
function loadReportMeta() {
  [['company','meta-company'],['project','meta-project'],['ref','meta-ref'],
   ['prepby','meta-prepby'],['checkedby','meta-checkedby']].forEach(([k, id]) => {
    const el = document.getElementById(id);
    if (el) el.value = localStorage.getItem('meta_' + k) || '';
  });
}

function exportPDF() {
  saveReportMeta();
  if (!lastCalcData) { alert('Gör en beräkning först — välj sedan Exportera PDF.'); return; }
  const meta    = getReportMeta();
  const fd      = lastCalcData;
  const fmla    = FORMULAS[fd.type] || {};
  const now     = new Date();
  const dateStr = now.toLocaleDateString('sv-SE');
  const timeStr = now.toLocaleTimeString('sv-SE', { hour:'2-digit', minute:'2-digit' });
  const company = meta.company || 'Konsult AB';
  const initials = company.split(/\s+/).map(w => w[0]||'').join('').slice(0,3).toUpperCase();

  const inputRows = (fd.inputs||[]).map(([l,v]) =>
    `<tr><td>${l}</td><td>${v}</td></tr>`).join('');

  const formulaLines = (fmla.lines||[]).map(l =>
    `<div class="formula-line">${l}</div>`).join('');

  const resultRows = (fd.results||[]).map(([l,v]) =>
    `<tr><td>${l}</td><td class="res-val">${v}</td></tr>`).join('');

  const tableHtml = fd.table ? `
    <table class="rep-dim-table">
      <thead><tr>${fd.tableHeaders.map(h=>`<th>${h}</th>`).join('')}</tr></thead>
      <tbody>${fd.table.map(r =>
        `<tr class="${r.rec?'rec-row':''}">` +
        r.cells.map(c=>`<td>${c}</td>`).join('') +
        `</tr>`).join('')}
      </tbody>
    </table>` : '';

  document.getElementById('print-area').innerHTML = `
    <div class="rep-page">
      <div class="rep-header">
        <div class="rep-logo-box">${initials}</div>
        <div class="rep-title-block">
          <div class="rep-doc-type">Teknisk beräkning</div>
          <div class="rep-doc-title">${fmla.title || fd.type}</div>
          <div class="rep-company-name">${company}</div>
        </div>
        <div class="rep-meta-block">
          <table class="rep-meta-table">
            <tr><td>Projekt</td><td>${meta.project||'—'}</td></tr>
            <tr><td>Referens</td><td>${meta.ref||'—'}</td></tr>
            <tr><td>Datum</td><td>${dateStr}</td></tr>
            <tr><td>Upprättad av</td><td>${meta.prepBy||'—'}</td></tr>
            <tr><td>Granskad av</td><td>${meta.checkedBy||'—'}</td></tr>
          </table>
        </div>
      </div>

      <div class="rep-section">
        <div class="rep-section-title">1. Indata</div>
        <table class="rep-data-table">
          <thead><tr><th>Parameter</th><th>Värde</th></tr></thead>
          <tbody>${inputRows}</tbody>
        </table>
      </div>

      <div class="rep-section">
        <div class="rep-section-title">2. Beräkningsmetod</div>
        <div class="rep-formula-block">
          <div class="rep-method-name">${fmla.method||''}</div>
          <div class="rep-formula-code">${formulaLines}</div>
          ${fmla.standard ? `<div class="rep-standard">Tillämpad standard: ${fmla.standard}</div>` : ''}
        </div>
      </div>

      <div class="rep-section">
        <div class="rep-section-title">3. Resultat</div>
        ${resultRows ? `<table class="rep-data-table rep-result-table" style="margin-bottom:.6rem">
          <thead><tr><th>Storhet</th><th>Värde</th></tr></thead>
          <tbody>${resultRows}</tbody>
        </table>` : ''}
        ${tableHtml}
      </div>

      <div class="rep-footer">
        <span>${company}</span>
        <span>${fmla.title||''}</span>
        <span>Utskrivet ${dateStr} ${timeStr}</span>
      </div>
    </div>`;

  window.print();
}

// ════════════════════════════════════════════════════════════════════════════════
// GEMENSAM RESULTATVISNING
// ════════════════════════════════════════════════════════════════════════════════
function showResult(blockId, rows, copyStr = null) {
  const block = document.getElementById(blockId);
  if (!block) return;
  block.classList.remove('hidden');
  const copyBtn = copyStr ? `<button class="btn-copy" onclick="copyText(${JSON.stringify(copyStr)})">Kopiera</button>` : '';
  block.innerHTML = copyBtn + rows.map(([label, value, rating]) => {
    const cls = rating === 'green' ? 'ok' : rating === 'yellow' ? 'warn' : rating === 'red' ? 'danger' : '';
    return `<div class="result-row">
      <span class="label">${label}</span>
      <span class="value ${cls}">${value}</span>
    </div>`;
  }).join('');
}

// ════════════════════════════════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initSubTabs('tab-kanal');

  // Fyll material-selects
  const matSelectKanal = $('#k-mat');
  Object.keys(DUCT_MATS).forEach(k => matSelectKanal.add(new Option(k, k)));
  matSelectKanal.addEventListener('change', updateKanalEps);
  updateKanalEps();

  const matSelectRor = $('#r-mat');
  Object.keys(PIPE_MATS).forEach(k => matSelectRor.add(new Option(k, k)));
  matSelectRor.addEventListener('change', updateRorMaterial);
  updateRorMaterial();

  // Medium-dropdown (rör)
  const medSel = $('#r-medium');
  const medOptions = [
    ['Vatten','Vatten'],
    ['──────','','disabled'],
    ['EG 20%','EG 20%'], ['EG 30%','EG 30%'], ['EG 40%','EG 40%'], ['EG 50%','EG 50%'],
    ['PG 20%','PG 20%'], ['PG 30%','PG 30%'], ['PG 40%','PG 40%'], ['PG 50%','PG 50%'],
    ['──────','','disabled'],
    ['Anpassa (ange ν och ρ)','custom'],
  ];
  medOptions.forEach(([text, val, dis]) => {
    const o = new Option(text, val);
    if (dis) o.disabled = true;
    medSel.add(o);
  });
  medSel.addEventListener('change', updateRorMedium);
  updateRorMedium();

  // Händelselyssnare — kanaler
  $('#btn-kc-list')?.addEventListener('click', calcCircList);
  $('#btn-kc2')?.addEventListener('click', calcCircDim);
  $('#btn-kc3')?.addEventListener('click', calcCircMax);
  $('#btn-kr-list')?.addEventListener('click', calcRectList);
  $('#btn-kr2')?.addEventListener('click', calcRectDim);

  // Rör
  $('#btn-r-dim')?.addEventListener('click', () => { calcRorDim(); calcRorList(); });
  $('#btn-r-maxq')?.addEventListener('click', calcRorMaxQ);
  $('#r-temp')?.addEventListener('input', calcHeatLoss);
  $('#r-tamb')?.addEventListener('input', calcHeatLoss);
  $('#r-ins-thick')?.addEventListener('input', calcHeatLoss);
  $('#r-ins-type')?.addEventListener('change', calcHeatLoss);

  // Kv
  $('#btn-kv')?.addEventListener('click', calcKv);
  $('#kv-fe-mode')?.addEventListener('change', toggleFe);
  toggleFe();

  // Effekt
  $('#btn-eff')?.addEventListener('click', calcEffektTab);
  $$('.dt-preset').forEach(btn =>
    btn.addEventListener('click', () => setDT(btn.dataset.dt))
  );

  // PDF-export
  $('#btn-export-pdf')?.addEventListener('click', exportPDF);
  loadReportMeta();
  // Spara meta automatiskt vid ändring
  $$('#meta-bar input').forEach(el => el.addEventListener('change', saveReportMeta));

  // Historik-paneler
  ['kanal','ror','kv','effekt'].forEach(initHistory);

  // Ladda senaste inmatning
  ['form-kanal-circ','form-kanal-circ2','form-kanal-rect','form-kanal-rect2',
   'form-ror','form-kv','form-effekt'].forEach(loadInputs);

  // Lustiq snabb-summering
  $$('input.sum-expr').forEach(el => {
    el.addEventListener('blur', () => {
      const val = sumExpr(el.value);
      if (val !== null && el.value.includes('+')) {
        el.title = `= ${val}`;
      }
    });
  });
});
