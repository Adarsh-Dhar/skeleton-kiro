/* WATTWISE LAB single-page app (no build step). All dynamic text is escaped with esc(). */
const esc = Charts.esc, fmt = Charts.f, clock = Charts.clock;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const view = () => $('#view');
let META = null, pollTimer = null, HH = null, HSTATE = null; // HH = saved household settings; HSTATE = the whole state (readiness, current schedule, backup)

class ApiError extends Error { constructor(status, issues, baselineCheck = null) { super(issues.map((i) => i.message).join('; ')); this.status = status; this.issues = issues; this.baselineCheck = baselineCheck; } }
async function api(method, path, body) {
  const res = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(res.status, Array.isArray(data.detail) ? data.detail : [{ field: 'request', message: String(data.detail || res.statusText) }], data.baseline_check || null);
  return data;
}
function toast(msg, ok = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (ok ? ' ok' : ''); t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 4500);
}
// the baseline overlap check record (also stored with the study): status, the appliances involved and what was clipped
const baselineCheckBox = (c) => {
  if (!c) return '';
  const list = (a) => (a && a.length ? a.map(esc).join(', ') : 'none');
  const clipped = c.clipped?.count ? `; ${c.clipped.count} step(s) clipped to 0 kW at ${(c.clipped.times || []).map(esc).join(', ')}` : '';
  return `<div class="note info"><b>Baseline check: ${esc(String(c.status || '').replaceAll('_', ' '))}</b><ul>
    ${c.overlapping?.length ? `<li>Appliances found in the baseline data: ${list(c.overlapping)}</li>` : ''}
    <li>Checked: ${list(c.appliances_checked)}</li><li>Subtracted: ${list(c.appliances_subtracted)}${clipped}</li>
    ${c.skipped_by_user_confirmation ? '<li>Check skipped because you confirmed the baseline excludes the modelled appliances</li>' : ''}</ul></div>`;
};
const errorsBox = (e) => e instanceof ApiError
  ? `<div class="errors" role="alert"><b>That was not accepted</b><ul>${e.issues.map((i) => `<li><code>${esc(i.field)}</code> ${esc(i.message)}</li>`).join('')}</ul></div>${baselineCheckBox(e.baselineCheck)}`
  : `<div class="errors" role="alert">${esc(e.message || e)}</div>`;
const poll = (fn, ms = 1000) => { clearInterval(pollTimer); pollTimer = setInterval(fn, ms); };
const statusChip = (s) => `<span class="chip ${esc(s)}">${esc(s.replaceAll('_', ' '))}</span>`;
const progress = (c, t, cls = '') => `<div class="progress ${cls}"><i style="width:${t ? (100 * c) / t : 0}%"></i></div>`;
const isActive = (d) => d.counts.running > 0 || d.counts.queued > 0;
const money = (v, d = 2) => fmt(v, d);
const label = (n) => META?.factors[n]?.label || n;
const fmtFactor = (n, v) => (META?.factors[n]?.clock ? clock(v) : `${fmt(v, 1)} ${META?.factors[n]?.unit || ''}`.trim());
const methodName = (m) => String(m).replaceAll('_', ' ');
const num = (sel, root = document) => parseFloat($(sel, root).value);
const toHours = (hhmm) => { const [h, m] = String(hhmm || '0:0').split(':').map(Number); return (h || 0) + (m || 0) / 60; };
const toTime = (h) => { const m = Math.round(((h % 24) + 24) % 24 * 60); return `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`; };
const toAbs = (h) => (h >= 16 ? h : h + 24); // the model's 24 h window starts at 16:00

function setNav(key) { $$('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.nav === key)); }
function setLed(state, text) { $('#api-status').innerHTML = `<i class="led ${state}"></i><span>${esc(text)}</span>`; }
const kpi = (value, name, cls = '', note = '') => `<div class="kpi ${cls}"><b>${value}</b><span>${esc(name)}</span>${note ? `<small>${esc(note)}</small>` : ''}</div>`;

/* ------------------------------------------------------------ modal */
function openModal(html, mount) {
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}</div>`;
  const close = () => { bg.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  bg.addEventListener('mousedown', (e) => { if (e.target === bg) close(); });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(bg);
  $$('[data-close]', bg).forEach((b) => b.addEventListener('click', close));
  if (mount) mount(bg, close);
  (bg.querySelector('input,select,textarea,button') || bg).focus?.();
  return close;
}

/* ------------------------------------------------------------ household (stored on the server: GET/PUT /api/household) */
const LEGACY_HH_KEY = 'wattwise.household'; // read once to migrate settings saved by older versions, then removed
function legacyHousehold() {
  try {
    const s = JSON.parse(localStorage.getItem(LEGACY_HH_KEY) || 'null');
    return s && Array.isArray(s.appliances) ? s : null;
  } catch (_) { return null; }
}
async function loadHousehold() {
  let state = await api('GET', '/api/household');
  const legacy = legacyHousehold();
  if (legacy) {
    if (!state.saved) {
      // values the owner saved in this browser; the server labels them (no source yet) and fills in nothing
      const own = { source_type: 'user_provided', source_name: 'household owner', verification: 'user_supplied', notes: 'saved in this browser before source tracking; confirm it' };
      const body = { ...state.settings, ...legacy, ev: legacy.ev ? { ...legacy.ev, source: own } : null, appliances: legacy.appliances.slice(0, 6).map((a) => ({ ...a, source: own })),
        base_load_source: legacy.base_load_kw !== undefined && legacy.base_load_kw !== null ? own : null, migrated_from: 'localStorage' };
      if (!META.tariffs.some((t) => t.id === body.tariff_id)) body.tariff_id = state.settings.tariff_id;
      try { state = await api('PUT', '/api/household', body); toast('Household settings moved from this browser to the server', true); }
      catch (e) { console.warn('legacy household settings were not accepted', e); }
    }
    try { localStorage.removeItem(LEGACY_HH_KEY); } catch (_) { /* storage unavailable */ }
  }
  HSTATE = state;
  return state.settings; // nothing is chosen on the owner's behalf: an unknown or missing tariff stays unselected
}
async function saveHousehold(next = HH) {
  const state = await api('PUT', '/api/household', next); // the server validates; nothing is stored unless it accepts the settings
  HSTATE = state;
  HH = state.settings;
  return HH;
}
// the saved household as a projection request (the household calls the charger field charger_power_kw)
const hhBody = (extra = {}) => ({ tariff_id: HH.tariff_id, ev: HH.ev, appliances: HH.appliances, base_load_kw: HH.base_load_kw, base_load_source: HH.base_load_source,
  ...(HH.charger_power_kw !== null && HH.charger_power_kw !== undefined && HH.ev ? { charger_capacity_kw: HH.charger_power_kw, charger_source: HH.charger_source } : {}),
  ...(HH.baseline_import_id ? { baseline_import_id: HH.baseline_import_id } : {}), ...extra });
const isReady = () => !!HSTATE?.readiness?.ready;
const notReady = (what) => `<div class="empty"><h2>Configuration required</h2><p>${esc(what)} needs your own inputs first. Nothing is filled in with example values.</p>
  <ul class="warnlist" style="text-align:left;display:inline-block">${(HSTATE?.readiness?.missing || []).map((m) => `<li>${esc(m.message)}</li>`).join('')}</ul>
  <p><a class="btn primary" href="#/household">Open household setup</a></p></div>`;
const EV_ONLY = ['charge_start', 'charge_power_kw']; // schedule factors that only exist when the household has a car
const carKpi = (m, ok, sub) => (HH.ev ? kpi(fmt(m.departure_charge_pct, 0) + '%', 'car charge at departure', ok ? 'good' : 'bad', sub) : '');
const tariffOf = (id) => META.tariffs.find((t) => t.id === id);
async function refreshTariffs() { META.tariffs = await api('GET', '/api/tariffs'); }
// the whole tariff (incl. optional fixed charges, tiers, taxes, seasonal/day-type periods), minus its library id
const inlineTariff = (t) => { const { id, source_status, ...rest } = JSON.parse(JSON.stringify(t)); return { ...rest, demand_charge_per_kw: rest.demand_charge_per_kw || 0 }; };
const tariffSelect = (id, sel) => `<select id="${id}">${META.tariffs.length ? '' : '<option value="">no tariff configured</option>'}${META.tariffs.filter((t) => t.source_status?.usable !== false).map((t) => `<option value="${esc(t.id)}" ${t.id === sel ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select>`;
function bandBar(t) {
  const steps = []; for (let k = 0; k < 96; k++) steps.push(k * 0.25);
  const prices = t.periods.map((p) => p.price), lo = Math.min(...prices), hi = Math.max(...prices);
  const col = (p) => { const x = hi > lo ? (p - lo) / (hi - lo) : 0.5, a = [58, 90, 155], b = [209, 53, 31]; return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * x)).join(',')})`; };
  return `<div class="bandbar" role="img" aria-label="tariff over 24 hours">${steps.map((s) => { const p = Charts.periodAt(t, s); return `<i style="flex:1;background:${p ? col(p.price) : '#ccc'}" title="${esc(clock(s))} ${p ? esc(p.label || '') + ' ' + fmt(p.price, 3) : 'no price'}"></i>`; }).join('')}</div>`;
}

/* ------------------------------------------------------------ router */
const TABS = [['design', 'Design'], ['runs', 'Run monitor'], ['analysis', 'Analysis'], ['optimise', 'Optimisation'], ['report', 'Report']];
async function route() {
  clearInterval(pollTimer);
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  try {
    if (!META) { META = await api('GET', '/api/meta'); HH = await loadHousehold(); }
    if (!parts.length) { setNav('dash'); return await viewDashboard(); }
    if (parts[0] === 'household') { setNav('household'); return await viewHousehold(); }
    if (parts[0] === 'new') { setNav('new'); return await viewNew(); }
    if (parts[0] === 'simulator') { setNav('simulator'); return await viewSimulator(); }
    if (parts[0] === 'meter') { setNav('meter'); return await viewMeter(); }
    if (parts[0] === 'usage') { setNav('usage'); return await viewUsage(); }
    if (['analysis', 'optimise', 'reports'].includes(parts[0])) { setNav(parts[0]); return await viewPicker(parts[0]); }
    if (parts[0] === 'e' && parts[1]) {
      const tab = parts[2] || 'design';
      setNav({ analysis: 'analysis', optimise: 'optimise', report: 'reports' }[tab] || 'dash');
      const fn = { design: viewDesign, runs: viewRuns, analysis: viewAnalysis, optimise: viewOptimise, report: viewReport }[tab];
      if (fn) return await fn(parts[1]);
    }
    view().innerHTML = '<div class="empty"><h2>Page not found</h2><a href="#/">Back to the dashboard</a></div>';
  } catch (e) { view().innerHTML = errorsBox(e) + '<p><a href="#/">Back to the dashboard</a></p>'; }
}
window.addEventListener('hashchange', route);

function expHeader(d, tab) {
  return `<div class="row"><div><h1>${esc(d.name)}</h1><p class="sub">${esc(d.hypothesis || 'No hypothesis stated')}</p></div><div class="spacer"></div><div>${statusChip(d.status)}</div></div>
    <div class="row"><div style="flex:1">${progress(d.counts.completed, d.runs_total)}</div><span class="dim">${d.counts.completed} of ${d.runs_total} schedules projected &middot; budget ${d.runs_total}/${d.run_budget}</span></div>
    ${d.legacy_inputs ? `<div class="errors" role="status"><b>Legacy experiment.</b> This experiment was ${esc(d.legacy_notice || '')}</div>` : ''}
    <div class="tabs">${TABS.map(([k, n]) => `<a href="#/e/${esc(d.id)}/${k}" class="${k === tab ? 'on' : ''}">${n}</a>`).join('')}</div>`;
}
const downloadLink = (href, text) => `<a class="btn" href="${esc(href)}" download>${esc(text)}</a>`;

/* ------------------------------------------------------------ 1. energy dashboard */
async function viewDashboard() {
  view().innerHTML = `<div class="row"><div><h1>Energy dashboard</h1><p class="sub">What a day costs in your household, and what your experiments have found so far.</p></div><div class="spacer"></div><a class="btn primary" href="#/new">New experiment</a></div><div id="err"></div><div id="kpis" class="kpis"></div>
    <div class="panel"><h2>A typical day with today's habits (projected)</h2><div id="day">Loading&hellip;</div></div><div class="panel"><h2>Experiments</h2><div id="dash">Loading&hellip;</div></div>`;
  const cur = HSTATE?.current_schedule || {};
  const usual = (HH.appliances || []).map((a) => `${a.name} at ${clock(a.fixed_start)}`).join(', ');
  const drawDay = async () => {
    if (!isReady()) { $('#day').innerHTML = notReady('A projected day'); return; }
    const sim = await api('POST', '/api/simulate/day', hhBody());
    const m = sim.metrics;
    $('#day').innerHTML = `<div class="kpis">${kpi(money(m.energy_cost), 'cost per day', '', tariffOf(HH.tariff_id)?.name || '')}${kpi(fmt(m.energy_kwh, 1) + ' kWh', 'energy used')}${kpi(fmt(m.peak_power_kw, 1) + ' kW', 'peak demand')}
      ${HH.ev ? carKpi(m, m.departure_charge_pct >= HH.ev.desired_charge_pct, `needs ${fmt(HH.ev.desired_charge_pct, 0)}%`) : ''}</div>
      ${Charts.dayProfile({ steps: sim.steps, tariff: sim.tariff, departure: HH.ev?.departure_hour })}<p class="hint">Projected from your inputs: ${HH.ev ? `plugging in at ${clock(cur.charge_start)} at ${fmt(cur.charge_power_kw, 1)} kW; ` : ''}${esc(usual || 'no appliances')}. Background load: ${HH.baseline_import_id ? 'your measured meter profile' : 'the always-on load you entered'}. <a href="#/simulator">Try other schedules</a> &middot; <a href="#/household">Edit household</a></p>`;
  };
  const draw = async () => {
    const ov = await api('GET', '/api/overview');
    if (!$('#dash')) return ov;
    const k = ov.kpis;
    $('#kpis').innerHTML = kpi(k.experiments, 'experiments') + kpi(k.runs, 'schedules projected') + kpi(k.best_saving_pct === null ? '&mdash;' : fmt(k.best_saving_pct, 0) + '%', 'best saving found', k.best_saving_pct > 0 ? 'good' : '') + kpi(fmt(k.simulated_kwh, 0) + ' kWh', 'projected energy');
    const list = ov.experiments;
    $('#dash').innerHTML = list.length ? `<div class="scroll"><table><thead><tr><th>Experiment</th><th>Status</th><th>Progress</th><th>Design</th><th class="num">Best cost / day</th><th class="num">Saving</th><th>Created</th><th></th></tr></thead><tbody>
      ${list.map((e) => `<tr class="clickable" data-id="${esc(e.id)}"><td><b>${esc(e.name)}</b>${e.legacy_inputs ? ' <span class="chip prov-s" title="built on example inputs from an earlier version">EXAMPLE INPUTS</span>' : ''}<div class="dim mono">${esc(e.id)}</div></td><td>${statusChip(e.status)}</td>
      <td style="min-width:150px">${progress(e.counts.completed, e.runs_total, 'sm')}<span class="dim">${e.counts.completed}/${e.runs_total}${e.counts.failed ? ` &middot; <span class="badc">${e.counts.failed} failed</span>` : ''}</span></td>
      <td>${esc(methodName(e.design_method))}</td><td class="num">${e.best ? money(e.best.cost) : '&mdash;'}</td><td class="num">${e.best ? fmt(e.best.saving_pct, 0) + '%' : '&mdash;'}</td>
      <td class="dim">${esc(e.created_at.slice(0, 16).replace('T', ' '))}</td><td><a class="btn small" href="#/e/${esc(e.id)}/design">Open</a></td></tr>`).join('')}</tbody></table></div>`
      : `<div class="empty"><h2>No experiments yet</h2><p>Pick the variables you can control (when the car starts charging, how fast, when the dishwasher runs), project each combination from your inputs and let the optimiser find the cheapest schedule that still leaves the car ready.</p>
      <p>${isReady() ? '<button class="btn primary" id="mine">Create experiment from my configuration</button> &nbsp; <a class="btn" href="#/new">Choose the details</a>' : '<a class="btn primary" href="#/household">Configure your household first</a>'}</p></div>`;
    $$('tr[data-id]').forEach((tr) => tr.addEventListener('click', (ev) => { if (!ev.target.closest('a')) location.hash = `#/e/${tr.dataset.id}/design`; }));
    $('#mine')?.addEventListener('click', async () => {
      try { const d = await api('POST', '/api/experiments', { ...hhBody(), name: 'My schedule', hypothesis: '' }); location.hash = `#/e/${d.id}/design`; } catch (e) { $('#err').innerHTML = errorsBox(e); }
    });
    return ov;
  };
  try { await Promise.all([drawDay(), draw()]); } catch (e) { $('#err').innerHTML = errorsBox(e); }
  poll(async () => { try { const ov = await draw(); if (!ov.experiments.some((e) => isActive(e))) clearInterval(pollTimer); } catch (_) { /* transient */ } }, 2000);
}

/* ------------------------------------------------------------ 2. household configuration: see household.js */
/* ------------------------------------------------------------ 4. energy schedule simulator */
async function viewSimulator() {
  if (!isReady()) { view().innerHTML = `<h1>Schedule simulator</h1>${notReady('The schedule simulator')}`; return; }
  // S holds only the values the user moved; everything else stays at today's schedule from the household's own inputs
  const S = {}; let token = 0, baseSteps = null;
  const cur = HSTATE.current_schedule || {};
  const firstUsual = (HH.appliances || []).find((a) => a.flexible)?.fixed_start;
  const shown = (k) => (k in S ? S[k] : k === 'appliance_start' ? firstUsual : cur[k]);
  const factorKeys = () => Object.keys(META.factors).filter((k) => (HH.ev || !EV_ONLY.includes(k)) && (k !== 'appliance_start' || firstUsual !== undefined));
  const limit = (k, v) => (k === 'charge_power_kw' && HH.charger_power_kw ? { ...v, high: HH.charger_power_kw } : v);
  const ctl = factorKeys().map((k) => { const v = limit(k, META.factors[k]); return `<div class="field"><label for="s-${k}">${esc(v.label)} <output id="o-${k}"></output></label>
    <input id="s-${k}" type="range" min="${v.low}" max="${v.high}" step="${v.clock ? 0.25 : 0.1}" value="${shown(k) ?? v.low}"></div>`; }).join('');
  view().innerHTML = `<div class="row"><div><h1>Schedule simulator</h1><p class="sub">Move the sliders to see how one projected day changes: when the car charges, how fast, and when your movable appliances run.</p></div><div class="spacer"></div><button class="btn" id="reset">Back to today's schedule</button></div><div id="err"></div>
    <div class="panel"><div class="simctl">${ctl}</div><p class="hint">Today's schedule comes from your household setup${HH.ev ? ` (plug in ${clock(cur.charge_start)}, ${fmt(cur.charge_power_kw, 1)} kW charger)` : ''}; appliances run at their usual start until you move the appliance slider.</p></div><div id="out" class="panel">Loading&hellip;</div>`;
  const labels = () => factorKeys().forEach((k) => { $(`#o-${k}`).textContent = k === 'appliance_start' && !(k in S) ? 'usual times' : fmtFactor(k, shown(k)); });
  const run = async () => {
    const my = ++token; labels();
    try {
      if (!baseSteps) baseSteps = (await api('POST', '/api/simulate/day', hhBody())).steps;
      const sim = await api('POST', '/api/simulate/day', hhBody({ setup: S }));
      if (my !== token) return; // a newer slider move is already in flight
      const m = sim.metrics, ok = !HH.ev || m.departure_charge_pct >= HH.ev.desired_charge_pct - 1e-9;
      $('#err').innerHTML = '';
      $('#out').innerHTML = `<div class="kpis">${kpi(money(m.energy_cost), 'cost per day', '', m.demand_charge ? `includes ${money(m.demand_charge)} demand charge` : '')}${kpi(fmt(m.energy_kwh, 1) + ' kWh', 'energy used')}${kpi(fmt(m.peak_power_kw, 1) + ' kW', 'peak demand')}
        ${HH.ev ? carKpi(m, ok, ok ? `meets the ${fmt(HH.ev.desired_charge_pct, 0)}% needed` : `below the ${fmt(HH.ev.desired_charge_pct, 0)}% needed`) : ''}</div>
        ${Charts.dayProfile({ steps: sim.steps, tariff: sim.tariff, departure: HH.ev?.departure_hour, compare: baseSteps.map((s) => ({ t: s.t, kw: s.kw })) })}
        <h3>Where the money goes</h3><div class="scroll"><table><thead><tr><th>Price period</th><th class="num">kWh</th><th class="num">Cost</th></tr></thead><tbody>${Object.entries(sim.cost_by_period).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v.kwh, 2)}</td><td class="num">${money(v.cost, 3)}</td></tr>`).join('')}</tbody></table></div>`;
    } catch (e) { if (my === token) $('#err').innerHTML = errorsBox(e); }
  };
  let t = null;
  factorKeys().forEach((k) => $(`#s-${k}`).addEventListener('input', () => { S[k] = parseFloat($(`#s-${k}`).value); labels(); clearTimeout(t); t = setTimeout(run, 120); }));
  $('#reset').addEventListener('click', () => { Object.keys(S).forEach((k) => delete S[k]); factorKeys().forEach((k) => { $(`#s-${k}`).value = shown(k); }); run(); });
  run();
}

/* ------------------------------------------------------------ smart-meter CSV */
const readFileText = (file) => new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result)); r.onerror = () => rej(new Error('could not read the file')); r.readAsText(file); });
function meterSummary(p) {
  const s = p.summary, q = p.quality;
  return `<div class="kpis">${kpi(money(s.energy_cost), 'cost of this period', '', p.tariff.name)}${kpi(fmt(s.energy_kwh, 1) + ' kWh', 'energy')}${kpi(fmt(s.peak_power_kw, 1) + ' kW', 'peak demand')}${kpi(fmt(s.hours_covered, 1) + ' h', 'covered', '', `${s.readings} readings every ${fmt(s.interval_minutes, 0)} min`)}</div>
    ${(p.warnings || []).length ? `<ul class="warnlist">${p.warnings.slice(0, 8).map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    <p class="hint">Quality: ${q.duplicates} duplicate, ${q.out_of_order} out of order, ${q.unit_converted} converted from watts, ${q.invalid} rejected.</p>
    ${q.interpretation ? `<p class="hint">Read as <b>${esc(q.interpretation.reading_kind)}</b> in <b>${esc(q.interpretation.unit)}</b> from <code>${esc(q.interpretation.timestamp_column)}</code> / <code>${esc(q.interpretation.value_column)}</code>, time zone <b>${esc(q.timezone)}</b>; ${q.summary.rows_used} of ${q.summary.rows_total} rows used, ${q.summary.rows_skipped} skipped.</p>
      ${q.row_log.length ? `<details><summary>Skipped and transformed rows (${q.row_log.length})</summary><div class="scroll"><table><tbody>${q.row_log.map((l) => `<tr><td class="num">${l.line ?? ''}</td><td>${esc(l.action)}</td><td>${esc(l.reason)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}` : ''}
    ${Charts.hourly({ profile: s.hourly_profile, tariff: p.tariff })}`;
}
async function viewMeter() {
  view().innerHTML = `<h1>Meter import</h1><p class="sub">Preview a smart-meter CSV with columns <code>timestamp</code> and <code>power_kw</code> (or <code>power_w</code>). Readings are priced with the tariff you choose. To use a file as the result of an experiment run, attach it from that experiment's Run monitor.</p><div id="err"></div>
    <div class="grid g21"><div class="panel"><h2>CSV data</h2><div class="field"><label for="file">Choose a file</label><input id="file" type="file" accept=".csv,text/csv"></div>
      <div class="field"><label for="csv">or paste CSV text</label><textarea id="csv" spellcheck="false" placeholder="timestamp,power_kw&#10;2026-03-03T16:00:00,0.40"></textarea></div>
      <p class="hint">Use your own meter export: download it from your utility's consumer portal or smart-meter app. No demo data is provided.</p></div>
    <div class="panel"><h2>Options</h2><div class="field"><label for="unit">Power unit for a plain "power" column</label><select id="unit"><option>kW</option><option>W</option></select></div>
      <div class="field"><label for="mt">Tariff</label>${tariffSelect('mt', HH.tariff_id)}</div><button class="btn primary" id="prev" style="width:100%">Preview readings</button></div></div><div id="res"></div>`;
  let filename = 'meter.csv';
  $('#file').addEventListener('change', async () => { const f = $('#file').files[0]; if (!f) return; filename = f.name; try { $('#csv').value = await readFileText(f); } catch (e) { $('#err').innerHTML = errorsBox(e); } });
  $('#prev').addEventListener('click', async () => {
    $('#err').innerHTML = ''; $('#res').innerHTML = '';
    try { const p = await api('POST', '/api/meter/preview', { csv: $('#csv').value, unit: $('#unit').value, tariff_id: $('#mt').value, filename }); $('#res').innerHTML = `<div class="panel"><h2>Preview</h2>${meterSummary(p)}</div>`; }
    catch (e) { $('#err').innerHTML = errorsBox(e); }
  });
}

/* ------------------------------------------------------------ 3. experiment designer */
async function viewNew() {
  if (!isReady()) { view().innerHTML = `<h1>Experiment designer</h1>${notReady('An experiment')}`; return; }
  const fdef = META.default_factors, t = tariffOf(HH.tariff_id);
  view().innerHTML = `<h1>Experiment designer</h1><p class="sub">Choose which parts of the schedule to vary, set the range of each, and generate the set of combinations to measure.</p><div id="err"></div>
  <form id="f" class="grid g21"><div>
    <div class="panel"><h2>Experiment</h2><div class="field"><label for="name">Name</label><input id="name" value="Schedule experiment" maxlength="80"></div>
      <div class="field"><label for="hyp">Hypothesis</label><textarea id="hyp" style="min-height:60px;font-family:var(--sans)" placeholder="What you expect, e.g. charging after 23:00 costs less and the car is still ready"></textarea></div></div>
    <div class="panel"><h2>Schedule variables</h2><div class="scroll"><table><thead><tr><th>Vary</th><th>Variable</th><th>Lowest</th><th>Highest</th><th>Allowed range</th></tr></thead><tbody>
      ${Object.entries(META.factors).map(([k, v]) => `<tr><td><input type="checkbox" data-en="${k}" checked aria-label="Vary ${esc(v.label)}"></td><td>${esc(v.label)}<div class="dim mono">${k}</div></td>
      <td><input type="number" step="${v.clock ? 0.25 : 0.1}" data-lo="${k}" value="${fdef[k].low}" aria-label="${esc(v.label)} lowest"><div class="hint" data-lc="${k}"></div></td>
      <td><input type="number" step="${v.clock ? 0.25 : 0.1}" data-hi="${k}" value="${fdef[k].high}" aria-label="${esc(v.label)} highest"><div class="hint" data-hc="${k}"></div></td>
      <td class="dim" data-rng="${k}">${esc(fmtFactor(k, v.low))} &ndash; ${esc(fmtFactor(k, v.high))}</td></tr>`).join('')}</tbody></table></div>
      <p class="hint">Charging and appliance start times are hours on a 24 h clock that begins at 16:00, so 31.5 means 07:30 the next morning. Variables you switch off stay at today's schedule.</p></div>
    <div class="panel"><h2>Household for this experiment</h2><div class="grid g2">
      <div class="field"><label for="cap">Charger capacity, kW</label><input id="cap" type="number" readonly value="${HH.ev ? esc(HH.charger_power_kw ?? '') : ''}" placeholder="no EV configured"><div class="hint">From your household setup (charger nameplate). Limits the charging power the experiment can try.</div></div>
      <div class="field"><label class="inline"><input type="checkbox" id="noev" ${HH.ev ? '' : 'checked disabled'}> Leave the EV out of this experiment</label><div class="hint">Then only appliance start times are varied; at least one appliance must be movable.${HH.ev ? '' : ' No EV is configured in your household.'}</div></div>
      <div class="field"><label for="asm">Appliance start times</label><select id="asm"><option value="shared">one shared start time</option><option value="per_appliance">a separate start time per movable appliance</option></select></div>
      <div class="field"><label for="dtype">Baseline day type</label><select id="dtype"><option value="">all days</option><option value="weekday">weekday</option><option value="weekend">weekend</option></select><div class="hint">Which meter-data profile is the background load.</div></div>
      <div class="field"><label for="wdate">Start date of the projected window</label><input id="wdate" type="date"><div class="hint">Optional. The window runs 16:00 to 16:00; steps after midnight are priced on the next date.</div></div>
      <div class="field"><label for="bill">Billing cycle consumption so far, kWh</label><input id="bill" type="number" step="0.001" min="0" max="1000000"><div class="hint" id="billnote">For tiered tariffs. Leave empty to start at 0.</div></div></div>
      <div class="scroll"><table><thead><tr><th>Appliance</th><th>Id</th><th>Already in baseline</th></tr></thead><tbody id="appids">
      ${HH.appliances.map((a, i) => `<tr data-ai="${i}"><td>${esc(a.name)}</td><td><input data-f="id" value="${esc(a.id || '')}" maxlength="39" placeholder="from name" aria-label="Id of ${esc(a.name)}"></td>
        <td><input data-f="in_baseline" type="checkbox" ${a.in_baseline ? 'checked' : ''} aria-label="${esc(a.name)} is already in the baseline"></td></tr>`).join('')}
      ${HH.ev ? `<tr id="evrow"><td>Car charging</td><td class="dim">&ndash;</td><td><input id="ev-inb" type="checkbox" ${HH.ev.in_baseline ? 'checked' : ''} aria-label="Car charging is already in the baseline"></td></tr>` : ''}</tbody></table></div>
      <p class="hint">Tick "already in baseline" for loads your meter data already contains; they are subtracted from it instead of counted twice. Ids must be lowercase letters, digits, "_" or "-".</p>
      ${HH.baseline_import_id ? `<label class="inline"><input type="checkbox" id="bexcl"> my baseline data does not contain the modelled appliances (skips the overlap check)</label>
        <p class="hint">Without this, the meter data is checked for the modelled appliances and the experiment is blocked if they are found.</p>` : ''}</div>
    <div class="panel"><h2>Goal and constraint</h2><p style="margin:0 0 8px">Minimise the cost of one day on <b>${esc(t?.name || HH.tariff_id)}</b>.</p>
      <p style="margin:0 0 8px" id="constraint">${HH.ev ? `Constraint: the car must reach at least <b>${fmt(HH.ev.desired_charge_pct, 0)}%</b> by ${esc(toTime(HH.ev.departure_hour))}. Schedules below that are marked infeasible and never recommended.` : 'No EV, so there is no charge constraint.'} <a href="#/household">Change household settings</a></p>
      <label class="inline"><input type="checkbox" id="peak"> also reduce peak demand (adds a second objective)</label></div>
  </div><div>
    <div class="panel"><h2>Design</h2><div class="field"><label for="method">Method</label><select id="method">${META.methods.map((m) => `<option value="${m.id}">${esc(methodName(m.id))}</option>`).join('')}</select><div class="hint" id="mdesc"></div></div>
      <div class="grid g2"><div class="field"><label for="levels">Levels per variable</label><input id="levels" type="number" min="2" max="5" value="3"></div><div class="field"><label for="reps">Replicates</label><input id="reps" type="number" min="1" max="10" value="1"></div>
      <div class="field"><label for="cp">Centre points</label><input id="cp" type="number" min="0" max="10" value="2"></div><div class="field"><label for="nruns">Runs (random only)</label><input id="nruns" type="number" min="3" max="200" value="16"></div>
      <div class="field"><label for="budget">Run budget</label><input id="budget" type="number" min="4" max="300" value="60"></div><div class="field"><label for="seed">Random seed</label><input id="seed" type="number" min="0" value="1"></div></div></div>
    <div class="panel"><h2>Simulator</h2><div class="grid g2"><div class="field"><label for="lat">Seconds per run</label><input id="lat" type="number" step="any" min="0" max="5" value="0.3"></div><div class="field"><label for="retries">Retries on failure</label><input id="retries" type="number" min="0" max="5" value="1"></div>
      <div class="field"><label for="fault">Injected fault rate, for testing (0&ndash;1)</label><input id="fault" type="number" step="any" min="0" max="1" value="0"></div></div><p class="hint">A fault rate above zero makes some first attempts fail, to show how retries and failures are handled.</p></div>
    <button class="btn primary" type="submit" style="width:100%;padding:12px">Generate experiment combinations</button></div></form>`;
  const md = () => { $('#mdesc').textContent = META.methods.find((m) => m.id === $('#method').value).description; };
  const hints = () => Object.entries(META.factors).forEach(([k, v]) => { if (v.clock) { $(`[data-lc="${k}"]`).textContent = clock(num(`[data-lo="${k}"]`) || 0); $(`[data-hc="${k}"]`).textContent = clock(num(`[data-hi="${k}"]`) || 0); } });
  $('#method').addEventListener('change', md); $('#f').addEventListener('input', hints); md(); hints();
  const EV_FACTORS = ['charge_start', 'charge_power_kw'];
  const showNoEv = () => {
    const off = $('#noev').checked;
    EV_FACTORS.filter((k) => META.factors[k]).forEach((k) => { $(`[data-en="${k}"]`).disabled = off; $(`[data-lo="${k}"]`).disabled = off; $(`[data-hi="${k}"]`).disabled = off; $(`[data-en="${k}"]`).closest('tr').style.opacity = off ? 0.5 : ''; });
    if ($('#evrow')) $('#evrow').hidden = off;
  };
  const showMode = () => {
    const per = $('#asm').value === 'per_appliance';
    $('[data-en="appliance_start"]')?.closest('tr')?.querySelector('.dim.mono')?.replaceChildren(per ? 'appliance_start (range used for every movable appliance)' : 'appliance_start');
  };
  const capRange = () => { // keep the charging power range inside the charger capacity
    const cap = num('#cap'), lo = $('[data-lo="charge_power_kw"]'), hi = $('[data-hi="charge_power_kw"]'), cell = $('[data-rng="charge_power_kw"]');
    if (!lo || !hi) return;
    if (Number.isFinite(cap)) {
      if (num('[data-hi="charge_power_kw"]') > cap) hi.value = cap;
      if (num('[data-lo="charge_power_kw"]') >= num('[data-hi="charge_power_kw"]')) lo.value = Math.min(1.4, num('[data-hi="charge_power_kw"]') / 2);
      cell.textContent = `${fmt(1.4, 1)} \u2013 ${fmt(cap, 1)} kW`;
    } else cell.textContent = `${fmtFactor('charge_power_kw', META.factors.charge_power_kw.low)} \u2013 ${fmtFactor('charge_power_kw', META.factors.charge_power_kw.high)}`;
  };
  // default billing-cycle consumption from the chosen meter baseline (editable; never overwrites what the user typed)
  let billTouched = false;
  const loadBilling = async () => {
    if (!HH.baseline_import_id || billTouched) return;
    const day = $('#wdate').value || new Date().toISOString().slice(0, 10);
    try {
      const r = await api('GET', `/api/meter/imports/${encodeURIComponent(HH.baseline_import_id)}/billing-cycle-default?day=${encodeURIComponent(day)}`);
      if (!billTouched) $('#bill').value = r.kwh;
      $('#billnote').textContent = r.notice || `Default from your meter data for the month before ${day}; you can change it.`;
    } catch (e) { $('#billnote').textContent = e.message; }
  };
  $('#noev').addEventListener('change', showNoEv); $('#asm').addEventListener('change', showMode); $('#cap').addEventListener('change', capRange);
  $('#bill').addEventListener('input', () => { billTouched = true; }); $('#wdate').addEventListener('change', loadBilling);
  showNoEv(); showMode(); capRange(); loadBilling();
  $('#f').addEventListener('submit', async (ev) => {
    ev.preventDefault(); $('#err').innerHTML = '';
    const noEv = $('#noev').checked;
    const factors = {};
    Object.keys(META.factors).forEach((k) => {
      if (noEv && EV_FACTORS.includes(k)) return; // the server skips EV factors for a household without a car
      factors[k] = { enabled: $(`[data-en="${k}"]`).checked, low: num(`[data-lo="${k}"]`), high: num(`[data-hi="${k}"]`) };
    });
    // ids and in_baseline start from the saved household and change only when the form was edited
    const appliances = HH.appliances.map((a, i) => {
      const tr = $(`#appids tr[data-ai="${i}"]`), out = { ...a };
      const id = $('[data-f=id]', tr).value.trim(), inb = $('[data-f=in_baseline]', tr).checked;
      if (id !== (a.id || '')) out.id = id || null;
      if (inb !== !!a.in_baseline) out.in_baseline = inb;
      return out;
    });
    const extra = { appliances };
    if (noEv) { extra.ev = null; extra.charger_capacity_kw = null; extra.charger_source = null; }
    else if ($('#ev-inb').checked !== !!HH.ev.in_baseline) extra.ev = { ...HH.ev, in_baseline: $('#ev-inb').checked };
    if ($('#asm').value !== 'shared') extra.appliance_start_mode = $('#asm').value;
    if ($('#dtype').value) extra.day_type = $('#dtype').value;
    if ($('#wdate').value) extra.window_start_date = $('#wdate').value;
    const bill = num('#bill'); if (Number.isFinite(bill)) extra.billing_cycle_consumption_kwh = bill;
    if ($('#bexcl')?.checked) extra.baseline_excludes_modelled_appliances = true;
    const body = { ...hhBody(extra), name: $('#name').value, hypothesis: $('#hyp').value, factors, also_minimise_peak: $('#peak').checked,
      design: { method: $('#method').value, levels: +$('#levels').value, replicates: +$('#reps').value, center_points: +$('#cp').value, n_runs: +$('#nruns').value, run_budget: +$('#budget').value, seed: +$('#seed').value },
      simulator: { latency_seconds: num('#lat'), max_retries: +$('#retries').value, fault_rate: num('#fault') } };
    try { const d = await api('POST', '/api/experiments', body); toast('Design generated', true); location.hash = `#/e/${d.id}/design`; }
    catch (e) { $('#err').innerHTML = errorsBox(e); window.scrollTo(0, 0); }
  });
}

/* ------------------------------------------------------------ experiment pickers for Analysis / Optimisation / Reports */
async function viewPicker(kind) {
  const tab = { analysis: 'analysis', optimise: 'optimise', reports: 'report' }[kind];
  const title = { analysis: 'Analysis', optimise: 'Optimisation', reports: 'Reports' }[kind];
  const blurb = { analysis: 'Compare measured schedules, costs, factor effects, constraints and peak demand.', optimise: 'Find the cheapest schedule that still charges the car, and measure what the optimiser proposes.', reports: 'Download results and recommendations.' }[kind];
  const list = await api('GET', '/api/experiments');
  view().innerHTML = `<h1>${title}</h1><p class="sub">${blurb} Choose an experiment.</p><div class="panel">${list.length ? `<div class="scroll"><table><thead><tr><th>Experiment</th><th>Status</th><th class="num">Measured</th><th></th></tr></thead><tbody>
    ${list.map((e) => `<tr><td><b>${esc(e.name)}</b><div class="dim mono">${esc(e.id)}</div></td><td>${statusChip(e.status)}</td><td class="num">${e.counts.completed}/${e.runs_total}</td><td><a class="btn small" href="#/e/${esc(e.id)}/${tab}">Open ${esc(title.toLowerCase())}</a></td></tr>`).join('')}</tbody></table></div>`
    : '<div class="empty"><h2>No experiments yet</h2><p>Generate one first, run it, then come back here.</p><a class="btn primary" href="#/new">Design an experiment</a></div>'}</div>`;
}

/* ------------------------------------------------------------ experiment: design */
async function viewDesign(id) {
  const d = await api('GET', `/api/experiments/${encodeURIComponent(id)}`);
  const queued = d.counts.queued + d.counts.created;
  const keep = d.design.limitations || [];
  view().innerHTML = expHeader(d, 'design') + `<div id="err"></div><div class="grid g21"><div class="panel"><h2>Combinations to measure</h2><div class="scroll"><table><thead><tr><th>#</th><th>Kind</th>${d.factors.map((f) => `<th>${esc(f.label)}</th>`).join('')}<th>Levels</th></tr></thead><tbody>
    ${d.runs.map((r) => `<tr><td>${r.order + 1}</td><td class="dim">${esc(r.point_kind || (r.iteration ? 'optimiser' : 'design'))}</td>${d.factors.map((f) => `<td class="num">${esc(fmtFactor(f.name, r.configuration[f.name]))}</td>`).join('')}
    <td>${d.factors.map((f) => { const c = r.coded?.[f.name]; return c === undefined ? '' : `<span class="coded ${c < -0.01 ? 'm1' : c > 0.01 ? 'p1' : 'z'}">${fmt(c, 1)}</span>`; }).join(' ')}</td></tr>`).join('')}</tbody></table></div></div>
  <div><div class="panel"><h2>Design</h2><p style="margin:0">${esc(methodName(d.design.method))} &middot; ${d.design.runs_planned} schedules &middot; seed ${esc(d.design.seed)}</p>${d.design.resolution ? `<p class="hint">Resolution ${esc(d.design.resolution)}</p>` : ''}
    ${d.design.generators ? `<p class="hint">Generators: ${esc([].concat(d.design.generators).join(', '))}</p>` : ''}${keep.length ? `<ul class="warnlist">${keep.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    <p class="hint">Run order is randomised by the SDK, so the order above is the order they will be measured.</p></div>
  <div class="panel"><h2>Household</h2><p style="margin:0">Tariff: <b>${esc(d.tariff)}</b></p>${d.household.ev ? `<p style="margin:4px 0 0">Car needs <b>${fmt(d.desired_charge_pct, 0)}%</b> by ${esc(toTime(d.household.ev.departure_hour))}</p>` : '<p style="margin:4px 0 0">No EV in this household</p>'}<p class="hint">${d.baseline_cost === null ? 'No baseline cost: this experiment was built on example inputs.' : `Today's schedule (projected): ${money(d.baseline_cost)} per day.`}</p>${baselineCheckBox(d.baseline_check)}${(d.notes || []).length ? `<ul class="warnlist">${d.notes.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>
  <div class="panel"><h2>Inputs and their sources</h2><p class="hint">Snapshot taken when this experiment was created. Every outcome below is projected from these inputs.</p>${Sources.table(d.input_sources)}</div>
  <button class="btn primary" id="go" style="width:100%;padding:12px" ${queued && !d.legacy_inputs ? '' : 'disabled'}>${d.legacy_inputs ? 'Legacy experiment: cannot be run' : queued ? `Project ${queued} schedules` : 'All schedules projected'}</button></div></div>`;
  $('#go').addEventListener('click', async () => { try { await api('POST', `/api/experiments/${encodeURIComponent(id)}/execute`); location.hash = `#/e/${id}/runs`; } catch (e) { $('#err').innerHTML = errorsBox(e); } });
}

/* ------------------------------------------------------------ experiment: runs */
const outcomeCells = (r) => r.outcomes && r.outcomes.energy_cost !== undefined
  ? `<td class="num">${money(r.outcomes.energy_cost)}</td><td class="num">${fmt(r.outcomes.energy_kwh, 1)}</td><td class="num">${fmt(r.outcomes.peak_power_kw, 1)}</td><td class="num">${fmt(r.outcomes.departure_charge_pct, 0)}%</td><td>${r.feasible ? '<span class="good">yes</span>' : '<span class="badc">no</span>'}</td>`
  : '<td></td><td></td><td></td><td></td><td></td>';
async function viewRuns(id) {
  const eid = encodeURIComponent(id);
  const draw = async () => {
    const d = await api('GET', `/api/experiments/${eid}`);
    if (!$('#runs-root')) return d;
    const act = isActive(d), failed = d.counts.failed;
    $('#runs-root').innerHTML = expHeader(d, 'runs') + `<div class="row" style="margin-bottom:12px"><button class="btn primary" id="exec" ${d.counts.queued + d.counts.created > 0 && !act ? '' : 'disabled'}>Run queued schedules</button>
      <button class="btn danger" id="cancel" ${act ? '' : 'disabled'}>Cancel</button><button class="btn" id="retry" ${failed && !act ? '' : 'disabled'}>Retry ${failed || ''} failed</button><span class="spacer"></span>
      <span class="dim">${d.counts.running} running &middot; ${d.counts.queued} queued &middot; ${d.counts.failed} failed &middot; ${d.counts.cancelled} cancelled</span></div>
    <div class="panel scroll"><table><thead><tr><th>#</th><th>Status</th>${d.factors.map((f) => `<th>${esc(f.label)}</th>`).join('')}<th class="num">Cost</th><th class="num">kWh</th><th class="num">Peak kW</th><th class="num">Charge</th><th>Feasible</th><th></th></tr></thead><tbody>
    ${d.runs.map((r) => `<tr><td>${r.order + 1}</td><td>${statusChip(r.status)}${r.attempts > 1 ? `<span class="tag">attempt ${r.attempts}</span>` : ''}${r.imported ? '<span class="chip imported" style="margin-left:4px">meter data</span>' : ''}${r.iteration ? '<span class="chip opt" style="margin-left:4px">optimiser</span>' : ''}${r.error ? `<div class="badc" style="font-size:12px">${esc(r.error)}</div>` : ''}</td>
      ${d.factors.map((f) => `<td class="num">${esc(fmtFactor(f.name, r.configuration[f.name]))}</td>`).join('')}${outcomeCells(r)}
      <td style="white-space:nowrap">${r.status === 'completed' ? `<button class="btn small" data-prof="${esc(r.run_id)}">Day</button> ` : ''}${['completed', 'failed'].includes(r.status) ? `<button class="btn small" data-meter="${esc(r.run_id)}">Meter CSV</button>` : ''}</td></tr>`).join('')}</tbody></table></div>`;
    $('#exec')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${eid}/execute`); toast('Running', true); draw(); } catch (e) { toast(e.message); } });
    $('#cancel')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${eid}/cancel`); draw(); } catch (e) { toast(e.message); } });
    $('#retry')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${eid}/retry`); toast('Retrying failed schedules', true); draw(); } catch (e) { toast(e.message); } });
    $$('[data-prof]').forEach((b) => b.addEventListener('click', () => showProfile(d, b.dataset.prof)));
    $$('[data-meter]').forEach((b) => b.addEventListener('click', () => attachMeter(d, b.dataset.meter, draw)));
    return d;
  };
  view().innerHTML = '<div id="runs-root">Loading&hellip;</div>';
  const first = await draw();
  poll(async () => { try { const d = await draw(); if (!isActive(d)) clearInterval(pollTimer); } catch (_) { /* transient */ } }, 1000);
  return first;
}
async function showProfile(d, runId) {
  try {
    const p = await api('GET', `/api/experiments/${encodeURIComponent(d.id)}/runs/${encodeURIComponent(runId)}/profile`);
    const run = d.runs.find((r) => r.run_id === runId), m = p.metrics;
    openModal(`<div class="row"><h2 style="margin:0">Schedule ${run ? run.order + 1 : ''}</h2><span class="spacer"></span><button class="btn small" data-close>Close</button></div>
      <p class="hint">${Object.entries(p.setup).map(([k, v]) => `${esc(label(k))}: <b>${esc(fmtFactor(k, v))}</b>`).join(' &middot; ')}</p>
      <div class="note info">This chart is the household model's PROJECTION for the schedule, from your configured inputs. A row marked "meter data" holds readings you imported instead.</div>
      <div class="kpis">${kpi(money(m.energy_cost), 'cost per day')}${kpi(fmt(m.energy_kwh, 1) + ' kWh', 'energy')}${kpi(fmt(m.peak_power_kw, 1) + ' kW', 'peak')}${d.household.ev ? kpi(fmt(m.departure_charge_pct, 0) + '%', 'charge at departure') : ''}</div>
      ${Charts.dayProfile({ steps: p.steps, tariff: p.tariff, departure: d.household.ev?.departure_hour })}`);
  } catch (e) { toast(e.message); }
}
function attachMeter(d, runId, done) {
  const run = d.runs.find((r) => r.run_id === runId);
  openModal(`<div class="row"><h2 style="margin:0">Attach meter readings to schedule ${run ? run.order + 1 : ''}</h2><span class="spacer"></span><button class="btn small" data-close>Close</button></div>
    <p class="hint">The readings replace the projected outcome for this schedule and are marked as imported. Priced with this experiment's own tariff (${esc(d.tariff)}).</p><div id="m-err"></div>
    <div class="field"><label for="m-file">CSV file</label><input id="m-file" type="file" accept=".csv,text/csv"></div><div class="field"><label for="m-csv">or paste CSV</label><textarea id="m-csv" spellcheck="false"></textarea></div>
    <div class="grid g2"><div class="field"><label for="m-unit">Unit of a plain "power" column</label><select id="m-unit"><option>kW</option><option>W</option></select></div>
      <div class="field"><label for="m-soc">Car charge when it left (%)</label><input id="m-soc" type="number" min="0" max="100" step="1" placeholder="read it from the car or its app"></div></div>
    <details id="m-adv"><summary>Other file formats (same reader as My usage)</summary>
      <p class="hint">Use this for your supplier's export as-is: other column names, local times, energy per interval or a cumulative meter register. Leave it closed for a plain <code>timestamp,power_kw</code> file.</p>
      <label class="row" style="text-transform:none;letter-spacing:0"><input type="checkbox" id="m-advon" style="width:auto"> Read the file with these settings</label>
      <div class="grid g2">
        <div class="field"><label for="m-tscol">Timestamp column</label><input id="m-tscol" placeholder="auto"></div>
        <div class="field"><label for="m-valcol">Value column</label><input id="m-valcol" placeholder="auto"></div>
        <div class="field"><label for="m-kind">Readings are</label><select id="m-kind"><option value="">auto</option><option value="power">average power</option><option value="interval_energy">energy per interval</option><option value="cumulative">cumulative meter register</option></select></div>
        <div class="field"><label for="m-aunit">Unit</label><select id="m-aunit"><option value="">auto</option><option>kW</option><option>W</option><option>kWh</option><option>Wh</option></select></div>
        <div class="field"><label for="m-tz">Time zone of timestamps without an offset</label><input id="m-tz" value="UTC" placeholder="e.g. Asia/Kolkata"></div>
        <div class="field"><label for="m-marks">Timestamp marks the interval</label><select id="m-marks"><option value="start">start</option><option value="end">end</option></select></div>
        <div class="field"><label for="m-dec">Decimal separator</label><select id="m-dec"><option value=".">. (1.5)</option><option value=",">, (1,5)</option></select></div>
        <div class="field"><label for="m-fmt">Timestamp format (if not ISO-8601)</label><input id="m-fmt" placeholder="e.g. %d/%m/%Y %H:%M"></div>
        <div class="field"><label for="m-roll">Register rollover (kWh)</label><input id="m-roll" type="number" min="1" placeholder="none"></div></div></details>
    <div id="m-prev"></div><div class="row"><button class="btn" id="m-pv">Preview</button><button class="btn primary" id="m-go">Attach to schedule</button></div>`, (bg, close) => {
    let filename = 'meter.csv';
    const advOptions = () => {
      if (!$('#m-advon', bg).checked) return null;
      const v = (id) => $(id, bg).value.trim(), o = { timezone: v('#m-tz') || 'UTC', timestamp_marks: v('#m-marks'), decimal: v('#m-dec') };
      if (v('#m-tscol')) o.timestamp_column = v('#m-tscol'); if (v('#m-valcol')) o.value_column = v('#m-valcol');
      if (v('#m-kind')) o.reading_kind = v('#m-kind'); if (v('#m-aunit')) o.unit = v('#m-aunit');
      if (v('#m-fmt')) o.timestamp_format = v('#m-fmt'); if (v('#m-roll')) o.rollover_kwh = parseFloat(v('#m-roll'));
      return o;
    };
    const body = () => ({ csv: $('#m-csv', bg).value, unit: $('#m-unit', bg).value, tariff: inlineTariff(d.household.tariff), tariff_id: null, filename,
      departure_charge_pct: num('#m-soc', bg), options: advOptions() });
    $('#m-file', bg).addEventListener('change', async () => { const f = $('#m-file', bg).files[0]; if (f) { filename = f.name; $('#m-csv', bg).value = await readFileText(f); } });
    $('#m-pv', bg).addEventListener('click', async () => { $('#m-err', bg).innerHTML = ''; try { $('#m-prev', bg).innerHTML = meterSummary(await api('POST', '/api/meter/preview', body())); } catch (e) { $('#m-err', bg).innerHTML = errorsBox(e); } });
    $('#m-go', bg).addEventListener('click', async () => {
      $('#m-err', bg).innerHTML = '';
      try { await api('POST', `/api/experiments/${encodeURIComponent(d.id)}/runs/${encodeURIComponent(runId)}/meter`, body()); toast('Meter readings attached', true); close(); done(); } catch (e) { $('#m-err', bg).innerHTML = errorsBox(e); }
    });
  });
}

/* ------------------------------------------------------------ experiment: analysis */
const schedLabel = (r) => Object.entries(r.schedule || {}).map(([k, v]) => `${label(k)} ${v}`).join(', ');
async function viewAnalysis(id) {
  const eid = encodeURIComponent(id);
  const d = await api('GET', `/api/experiments/${eid}`);
  view().innerHTML = expHeader(d, 'analysis') + `<div id="err"></div><div class="row" style="margin-bottom:12px"><label for="alpha" style="margin:0">Significance level</label><input id="alpha" type="number" step="0.01" min="0.001" max="0.49" value="0.05" style="width:90px">
    <label class="inline"><input type="checkbox" id="boot"> bootstrap intervals (slower)</label><button class="btn" id="rerun">Re-run analysis</button></div><div id="an">Analysing&hellip;</div>`;
  const run = async () => {
    $('#err').innerHTML = '';
    try {
      const a = await api('POST', `/api/experiments/${eid}/analysis`, { alpha: num('#alpha'), bootstrap: $('#boot').checked });
      const rec = a.recommendation, runs = Object.values(a.runs).filter((r) => r.status === 'completed' && r.outcomes.energy_cost !== undefined);
      const base = rec.baseline?.energy_cost ?? a.summary.baseline_cost;
      const eff = a.effects.effects.map((e) => ({ label: label(e.term), value: e.low_to_high ?? e.coefficient, lo: e.ci_95?.[0], hi: e.ci_95?.[1], sig: e.significant, p: e.p_value }));
      const good = rec.status === 'recommended';
      $('#an').innerHTML = `<div class="reco ${good ? '' : 'bad'}"><h3>${esc(rec.headline)}</h3>${good ? `<div class="sched">${Object.entries(rec.setup_text).map(([k, v]) => `<div><b>${esc(v)}</b><span>${esc(label(k))}</span></div>`).join('')}
        <div><b>${money(rec.expected.energy_cost)}</b><span>cost per day (today ${money(rec.baseline.energy_cost)})</span></div><div><b>${fmt(rec.expected.peak_power_kw, 1)} kW</b><span>peak (today ${fmt(rec.baseline.peak_power_kw, 1)} kW)</span></div></div>` : ''}
        <ul>${rec.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul><details><summary>Caveats</summary><ul>${rec.caveats.map((c) => `<li>${esc(c)}</li>`).join('')}</ul></details></div>
      <div class="grid g2"><div class="panel"><h2>Daily cost of each schedule</h2>${Charts.runCosts({ rows: runs.map((r) => ({ id: r.run_id, cost: r.outcomes.energy_cost, feasible: r.feasible, best: r.run_id === a.best_run_id, label: schedLabel(r) })), baseline: base })}</div>
        <div class="panel"><h2>Cost against peak demand</h2>${Charts.costPeak({ points: runs.map((r) => ({ x: r.outcomes.peak_power_kw, y: r.outcomes.energy_cost, feasible: r.feasible, best: r.run_id === a.best_run_id, title: `${schedLabel(r)}: ${money(r.outcomes.energy_cost)}, ${fmt(r.outcomes.peak_power_kw, 1)} kW` })) })}</div></div>
      <div class="grid g2"><div class="panel"><h2>What moves the cost</h2>${Charts.effects({ items: eff })}<p class="hint">Bars show the change in daily cost from the low to the high end of each tested range (negative is cheaper). * = significant at ${fmt(a.alpha, 2)}. Whiskers: 95% interval.${a.effects.r_squared !== null && a.effects.r_squared !== undefined ? ` R&sup2; = ${fmt(a.effects.r_squared, 3)}.` : ''}</p>
        ${a.effects.interactions.length ? `<h3>Interactions</h3><table><thead><tr><th>Terms</th><th class="num">Coefficient</th><th class="num">p</th></tr></thead><tbody>${a.effects.interactions.map((i) => `<tr><td>${esc(i.term.split(':').map(label).join(' \u00d7 '))}${i.significant ? ' *' : ''}</td><td class="num">${fmt(i.coefficient, 3)}</td><td class="num">${i.p_value === null ? 'n/a' : Number(i.p_value).toPrecision(3)}</td></tr>`).join('')}</tbody></table>` : ''}</div>
      <div class="panel"><h2>Departure-charge constraint</h2><p class="hint" style="margin-top:0">${a.feasible_run_ids.length} schedules reached ${fmt(d.desired_charge_pct, 0)}%; ${a.infeasible.length} did not.</p>
        ${a.infeasible.length ? `<div class="scroll"><table><thead><tr><th>Schedule</th><th class="num">Cost</th><th class="num">Charge reached</th></tr></thead><tbody>${a.infeasible.map((i) => `<tr><td>${esc(schedLabel(a.runs[i.run_id] || {}))}</td><td class="num">${i.cost === null || i.cost === undefined ? '' : money(i.cost)}</td><td class="num">${esc((i.violations || []).map((v) => (v.value === undefined || v.value === null ? 'violated' : fmt(v.value, 0) + '%')).join(', '))}</td></tr>`).join('')}</tbody></table></div>` : '<p class="good">Every measured schedule leaves the car ready.</p>'}</div></div>
      ${a.effects.anova ? `<div class="panel"><h2>ANOVA (Type II)</h2><div class="scroll"><table><thead><tr><th>Term</th><th class="num">Sum of squares</th><th class="num">df</th><th class="num">F</th><th class="num">p</th></tr></thead><tbody>${a.effects.anova.rows.map((r) => `<tr><td>${esc(String(r.term).split(':').map(label).join(' \u00d7 '))}</td><td class="num">${fmt(r.sum_squares, 4)}</td><td class="num">${fmt(r.df, 0)}</td><td class="num">${r.f_statistic === null ? '' : fmt(r.f_statistic, 3)}</td><td class="num">${r.p_value === null ? '' : Number(r.p_value).toPrecision(3)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
      ${(a.warnings.length || a.limitations.length) ? `<div class="panel"><h2>Warnings and limitations</h2><ul class="warnlist">${[...a.warnings, ...a.limitations].map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}`;
    } catch (e) {
      $('#an').innerHTML = '';
      $('#err').innerHTML = e instanceof ApiError && e.status === 409 ? `<div class="note">${esc(e.message)}. <a href="#/e/${esc(id)}/runs">Open the run monitor</a>.</div>` : errorsBox(e);
    }
  };
  $('#rerun').addEventListener('click', run); run();
}

/* ------------------------------------------------------------ experiment: optimisation */
async function viewOptimise(id) {
  const eid = encodeURIComponent(id);
  const d = await api('GET', `/api/experiments/${eid}`);
  const hh = d.household;
  view().innerHTML = expHeader(d, 'optimise') + `<div id="err"></div><div class="grid g21"><div class="panel"><h2>Cheapest feasible schedule so far</h2><div id="best">Loading&hellip;</div></div>
    <div class="panel"><h2>Next schedules to try</h2><div class="field"><label for="opt-m">Method</label><select id="opt-m">${META.optimisers.map((o) => `<option value="${o.id}">${esc(o.id)}</option>`).join('')}</select><div class="hint" id="opt-d"></div></div>
      <div class="field"><label for="opt-n">How many</label><input id="opt-n" type="number" min="1" max="10" value="2"></div>
      <div class="row"><button class="btn" id="suggest">Suggest</button><button class="btn primary" id="apply">Run suggested schedules</button></div><div id="sug"></div></div></div>
    <div class="grid g2"><div class="panel"><h2>Best cost as schedules are measured</h2><div id="prog"></div></div><div class="panel"><h2>Optimiser history</h2><div id="hist">Loading&hellip;</div></div></div>`;
  const od = () => { $('#opt-d').textContent = META.optimisers.find((o) => o.id === $('#opt-m').value).description; };
  $('#opt-m').addEventListener('change', od); od();
  const recHtml = (r) => `<div class="panel" style="margin:10px 0 0;background:var(--panel2)"><div class="row"><b>${Object.entries(r.schedule).map(([k, v]) => `${esc(label(k))} ${esc(v)}`).join(' &middot; ')}</b></div><p class="hint" style="margin:4px 0">${esc(r.rationale)}</p>
    <p class="hint" style="margin:0">${r.predicted_value !== null && r.predicted_value !== undefined ? `Predicted cost ${money(r.predicted_value)}${r.prediction_std ? ` &plusmn; ${money(r.prediction_std)}` : ''}. ` : ''}${r.predicted_feasibility_probability !== null && r.predicted_feasibility_probability !== undefined ? `Chance the car is ready: ${fmt(100 * r.predicted_feasibility_probability, 0)}%. ` : ''}<span class="chip">${esc(String(r.prediction_kind).replaceAll('_', ' '))}</span></p></div>`;
  const loadBest = async () => {
    const best = d.best;
    if (!best) { $('#best').innerHTML = '<div class="empty">No feasible schedule measured yet. <a href="#/e/' + esc(id) + '/runs">Run the experiment</a>.</div>'; return; }
    const [bp, base] = await Promise.all([api('GET', `/api/experiments/${eid}/runs/${encodeURIComponent(best.run_id)}/profile`), api('GET', `/api/experiments/${eid}/baseline/profile`)]);
    $('#best').innerHTML = `<div class="sched">${Object.entries(best.schedule).map(([k, v]) => `<div><b>${esc(v)}</b><span>${esc(label(k))}</span></div>`).join('')}<div><b>${money(best.cost)}</b><span>per day, ${fmt(best.saving_pct, 0)}% below today</span></div></div>
      ${Charts.dayProfile({ steps: bp.steps, tariff: bp.tariff, departure: hh.ev?.departure_hour, compare: base.steps.map((s) => ({ t: s.t, kw: s.kw })) })}`;
  };
  const loadRest = async () => {
    const [runs, hist] = await Promise.all([api('GET', `/api/experiments/${eid}/runs`), api('GET', `/api/experiments/${eid}/optimise/history`)]);
    let bestCost = Infinity; const pts = [];
    runs.filter((r) => r.status === 'completed' && r.outcomes.energy_cost !== undefined).sort((a, b) => a.order - b.order).forEach((r, i) => { if (r.feasible && r.outcomes.energy_cost < bestCost) bestCost = r.outcomes.energy_cost; if (bestCost < Infinity) pts.push({ x: i + 1, y: bestCost, opt: !!r.iteration }); });
    $('#prog').innerHTML = Charts.progressLine({ pts });
    $('#hist').innerHTML = hist.length ? `<div class="scroll"><table><thead><tr><th>When</th><th>Method</th><th>Schedule</th></tr></thead><tbody>${hist.map((h) => `<tr><td class="dim">${esc(String(h.created_at).slice(0, 16).replace('T', ' '))}</td><td>${esc(h.strategy || '')}</td><td>${Object.entries(h.configuration).map(([k, v]) => `${esc(label(k))} ${esc(fmtFactor(k, v))}`).join(', ')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="hint">Nothing proposed yet.</p>';
  };
  const call = async (path, applyIt) => {
    $('#err').innerHTML = ''; $('#sug').innerHTML = '';
    try {
      const out = await api('POST', `/api/experiments/${eid}/optimise/${path}`, { count: +$('#opt-n').value, method: $('#opt-m').value });
      const recs = out.recommendations;
      $('#sug').innerHTML = recs.map(recHtml).join('');
      if (applyIt) { toast(`${out.added_runs.length} schedule(s) added and running`, true); location.hash = `#/e/${id}/runs`; }
    } catch (e) { $('#err').innerHTML = errorsBox(e); }
  };
  $('#suggest').addEventListener('click', () => call('suggest', false)); $('#apply').addEventListener('click', () => call('apply', true));
  try { await Promise.all([loadBest(), loadRest()]); } catch (e) { $('#err').innerHTML = errorsBox(e); }
}

/* ------------------------------------------------------------ experiment: reports */
async function viewReport(id) {
  const eid = encodeURIComponent(id);
  const d = await api('GET', `/api/experiments/${eid}`);
  const ready = d.counts.completed > 0, base = `/api/experiments/${eid}`;
  view().innerHTML = expHeader(d, 'report') + `<div id="err"></div>${ready ? `<div class="panel"><h2>Download</h2><div class="row"><a class="btn primary" href="${base}/report.html" target="_blank" rel="noopener">Open printable report</a>
    ${downloadLink(`${base}/report.html`, 'Report (HTML)')}${downloadLink(`${base}/report.json`, 'Report (JSON)')}${downloadLink(`${base}/export/runs.csv`, 'Schedules (CSV)')}${downloadLink(`${base}/export/measurements.csv`, 'Measurements (CSV)')}${downloadLink(`${base}/export/effects.csv`, 'Factor effects (CSV)')}</div></div>
    <div class="panel"><h2>Preview</h2><iframe title="Report preview" sandbox src="${base}/report.html" style="width:100%;height:640px;border:1px solid var(--line);border-radius:8px;background:#fff"></iframe></div>`
    : `<div class="panel"><div class="empty"><h2>Nothing to report yet</h2><p>Reports need at least one projected schedule.</p><a class="btn primary" href="#/e/${esc(id)}/runs">Open the run monitor</a></div></div>`}`;
}

/* ------------------------------------------------------------ boot */
(async function boot() {
  try { const h = await api('GET', '/api/health'); setLed('ok', `ready \u00b7 trialforge ${h.trialforge}`); } catch (_) { setLed('bad', 'backend unreachable'); }
  route();
})();
