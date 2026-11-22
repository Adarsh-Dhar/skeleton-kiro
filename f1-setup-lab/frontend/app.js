/* F1 SETUP LAB single-page app (no build step). All dynamic text is escaped with esc(). */
const esc = Charts.esc, fmt = Charts.f;
const $ = (s, r = document) => r.querySelector(s);
const view = () => $('#view');
let META = null, pollTimer = null;

class ApiError extends Error { constructor(status, issues) { super(issues.map((i) => i.message).join('; ')); this.status = status; this.issues = issues; } }
async function api(method, path, body) {
  const res = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(res.status, Array.isArray(data.detail) ? data.detail : [{ field: 'request', message: String(data.detail || res.statusText) }]);
  return data;
}
function toast(msg, ok = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (ok ? ' ok' : ''); t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 4500);
}
const errorsBox = (e) => e instanceof ApiError
  ? `<div class="errors" role="alert"><b>Request rejected</b><ul>${e.issues.map((i) => `<li><code>${esc(i.field)}</code> ${esc(i.message)}</li>`).join('')}</ul></div>`
  : `<div class="errors" role="alert">${esc(e.message || e)}</div>`;
const poll = (fn, ms = 1000) => { clearInterval(pollTimer); pollTimer = setInterval(fn, ms); };
const statusChip = (s) => `<span class="chip ${esc(s)}">${esc(s.replaceAll('_', ' '))}</span>`;
const progress = (c, t, cls = '') => `<div class="progress ${cls}"><i style="width:${t ? (100 * c) / t : 0}%"></i></div>`;
const label = (n) => (META?.factors[n]?.label) || n;
const LIVE = 'live_simulator';
const isLive = (d) => d && d.mode === LIVE;
/* The second quality metric differs by mode: invented 'stability' (synthetic) vs lap-time spread (recorded laps). */
const SECOND = (d) => (isLive(d) ? { key: 'lap_time_std', name: 'lap spread', dp: 2 } : { key: 'stability', name: 'stability', dp: 1 });
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const flabel = (d, n) => d.factors.find((f) => f.name === n)?.label || label(n);
function setBanner(mode) {
  const b = $('#banner'); if (!b || !META.notices) return;
  b.textContent = META.notices[mode] || META.notices.synthetic; b.classList.toggle('simrec', mode === LIVE);
}
let STINT_MSG = '';
const unit = (n) => (META?.factors[n]?.unit) || '';
const isActive = (d) => ['running', 'queued'].includes(d.status) || d.counts.running > 0 || d.counts.queued > 0 && d.status === 'running';

function setNav(key) { document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.nav === key)); }
function setLed(state, text) { const a = $('#api-status'); a.innerHTML = `<i class="led ${state}"></i><span>${esc(text)}</span>`; }

/* ------------------------------------------------------------------ router */
const TABS = [['design', 'Setup design'], ['runs', 'Run monitor'], ['telemetry', 'Telemetry'], ['analysis', 'Analysis'], ['optimise', 'Optimisation'], ['report', 'Report']];
async function route() {
  clearInterval(pollTimer);
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  try {
    if (!META) META = await api('GET', '/api/meta');
    setBanner('synthetic');
    if (!parts.length) { location.replace('#/new'); return; }  // real historical F1 data is the landing page
    if (parts[0] === 'dashboard') { setNav('dash'); return await viewDashboard(); }
    if (parts[0] === 'new') {
      setNav('new');
      if (parts[1] === 'live') return await viewNewLive();
      if (parts[1] === 'synthetic') return await viewNew();
      setBanner('historical');  // real historical data is the default way to start
      return await viewNewHistorical();
    }
    if (parts[0] === 'live') { setNav('live'); setBanner(LIVE); return await viewLive(); }
    if (parts[0] === 'telemetry') { setNav('telemetry'); return await viewTelemetryStandalone(); }
    if (parts[0] === 'hist') { setNav('hist'); setBanner('historical'); return parts[1] ? await viewHistSession(parts[1], parts[2] || 'overview') : await viewHist(); }
    if (parts[0] === 'e' && parts[1]) {
      setNav('dash');
      const tab = parts[2] || 'design';
      const fn = { design: viewDesign, runs: viewRuns, telemetry: viewExpTelemetry, analysis: viewAnalysis, optimise: viewOptimise, report: viewReport }[tab];
      if (fn) return await fn(parts[1]);
    }
    view().innerHTML = '<div class="empty"><h2>Page not found</h2><a href="#/dashboard">Back to dashboard</a></div>';
  } catch (e) { view().innerHTML = errorsBox(e) + '<p><a href="#/dashboard">Back to dashboard</a></p>'; }
}
window.addEventListener('hashchange', route);

function expHeader(d, tab) {
  setBanner(d.mode);
  const done = d.counts.completed + d.counts.failed + d.counts.cancelled;
  return `<div class="row"><div><h1>${esc(d.name)}</h1><p class="sub">${esc(d.hypothesis || 'No hypothesis stated')}</p></div><div class="spacer"></div>
    <div>${isLive(d) ? '<span class="chip simrec">SIM-RECORDED</span> ' : '<span class="chip synthetic">SYNTHETIC</span> '}${statusChip(d.status)}</div></div>
    <div class="row"><div style="flex:1">${progress(d.counts.completed, d.runs_total)}</div><span class="mono dim">${d.counts.completed}/${d.runs_total} runs complete &middot; budget ${d.runs_total}/${d.run_budget}</span></div>
    <div class="tabs">${TABS.map(([k, n]) => `<a href="#/e/${esc(d.id)}/${k}" class="${k === tab ? 'on' : ''}">${isLive(d) && k === 'telemetry' ? 'Stint telemetry' : n}</a>`).join('')}</div>`;
}

/* --------------------------------------------------------------- dashboard */
async function viewDashboard() {
  const draw = async () => {
    const list = await api('GET', '/api/experiments');
    if (!$('#dash')) return;
    $('#dash').innerHTML = list.length ? `<table><thead><tr><th>Experiment</th><th>Status</th><th>Progress</th><th>Design</th><th class="num">Best feasible lap</th><th class="num">Stability</th><th>Created</th><th></th></tr></thead><tbody>
      ${list.map((e) => `<tr class="clickable" data-id="${esc(e.id)}"><td><b>${esc(e.name)}</b> ${isLive(e) ? '<span class="chip simrec">SIM-RECORDED</span>' : ''}<div class="dim mono">${esc(e.id)}</div></td><td>${statusChip(e.status)}</td>
      <td style="min-width:150px">${progress(e.counts.completed, e.runs_total, 'sm')}<span class="dim mono">${e.counts.completed}/${e.runs_total}${e.counts.failed ? ` &middot; <span class="badc">${e.counts.failed} failed</span>` : ''}</span></td>
      <td>${esc(e.design_method.replaceAll('_', ' '))}</td><td class="num">${e.best ? fmt(e.best.lap_time, 3) + ' s' : '&mdash;'}</td><td class="num">${e.best ? (isLive(e) ? fmt(e.best.lap_time_std, 2) + ' s spread' : fmt(e.best.stability, 1)) : '&mdash;'}</td>
      <td class="dim mono">${esc(e.created_at.slice(0, 16).replace('T', ' '))}</td><td><a class="btn small" href="#/e/${esc(e.id)}/design">Open</a></td></tr>`).join('')}</tbody></table>`
      : `<div class="empty"><h2>No experiments yet</h2><p>Design a screening experiment, run it on the synthetic simulator, find the setup factors that matter and let the optimiser refine them.</p>
      <p><button class="btn primary" id="demo">Create demo experiment</button> &nbsp; <a class="btn" href="#/new/synthetic">Configure my own</a></p></div>`;
    document.querySelectorAll('tr[data-id]').forEach((tr) => tr.addEventListener('click', (ev) => { if (!ev.target.closest('a')) location.hash = `#/e/${tr.dataset.id}/design`; }));
    $('#demo')?.addEventListener('click', async () => {
      try { const d = await api('POST', '/api/experiments', { name: 'Wing screening (demo)', hypothesis: 'A rearward aero balance improves lap time while keeping the car stable.' }); location.hash = `#/e/${d.id}/design`; } catch (e) { toast(e.message); }
    });
    return list;
  };
  view().innerHTML = `<div class="row"><div><h1>Dashboard</h1><p class="sub">Experiments on the synthetic vehicle simulator</p></div><div class="spacer"></div><a class="btn primary" href="#/new">New experiment</a></div><div class="panel" id="dash">Loading&hellip;</div>`;
  const first = await draw();
  poll(async () => { try { const l = await draw(); if (!l.some((e) => ['running', 'queued'].includes(e.status))) clearInterval(pollTimer); } catch (_) {} }, 2000);
  return first;
}

/* ----------------------------------------------------------- new experiment */
const NEW_MODES = [
  ['hist', '#/new', 'Historical F1 data', 'Recommended', 'Real Grand Prix sessions from OpenF1 (free) or FastF1. Opens an analysis; no experiment runs are created.'],
  ['synthetic', '#/new/synthetic', 'Synthetic simulator', 'Simulated data', 'Design-of-experiments and setup optimisation on the artificial vehicle model.'],
  ['live', '#/new/live', 'Live simulator (EA SPORTS F1 25)', 'Requires game', 'Drive your own stints in the game and record them over UDP.'],
];
const newModeCards = (active) => `<div class="modecards" role="tablist" aria-label="Data source">${NEW_MODES.map(([k, href, title, badge, text]) => `<a class="modecard ${k === active ? 'on' : ''}" href="${href}" role="tab" aria-selected="${k === active}" data-mode="${k}">
  <b>${esc(title)}</b><span class="tag">${esc(badge)}</span><span class="dim">${esc(text)}</span></a>`).join('')}</div>`;

/* Default New experiment screen: pick a real historical session and import it. Nothing here creates an experiment. */
async function viewNewHistorical() {
  view().innerHTML = `<h1>New experiment</h1>${newModeCards('hist')}<p class="sub">Choose a season, Grand Prix, session and driver. The real data is imported into the historical library and opened for analysis.</p>
    <div class="note info"><b>Real-data analysis, not a setup experiment.</b> Historical sessions contain no car setup, so they cannot show what a wing or suspension change does. For controlled setup studies and optimisation choose <a href="#/new/synthetic">Synthetic simulator</a> or the live game.</div><div id="hpick"></div>`;
  HistPicker.mount($('#hpick'), { api, errorsBox, toast, navigate: true });
}
async function viewNew() {
  const fdef = META.default_factors;
  view().innerHTML = `<h1>New experiment</h1>${newModeCards('synthetic')}<p class="sub">Define the setup variables, the objective and the stability constraint, then generate the design.</p><div id="err"></div>
  <form id="f" class="grid g21"><div>
    <div class="panel"><h2>1 &middot; Experiment</h2><div class="field"><label for="name">Name</label><input id="name" value="Wing & suspension screening" maxlength="80"></div>
      <div class="field"><label for="hyp">Hypothesis</label><textarea id="hyp" style="min-height:60px">A rearward aero balance improves lap time while keeping the car stable.</textarea></div></div>
    <div class="panel"><h2>2 &middot; Setup variables (factors)</h2><table><thead><tr><th>Use</th><th>Factor</th><th>Low</th><th>High</th><th>Unit</th><th>Simulator limits</th></tr></thead><tbody>
      ${Object.entries(META.factors).map(([k, v]) => `<tr><td><input type="checkbox" data-en="${k}" checked></td><td>${esc(v.label)}<div class="dim mono">${k}</div></td>
      <td><input type="number" step="any" data-lo="${k}" value="${fdef[k].low}"></td><td><input type="number" step="any" data-hi="${k}" value="${fdef[k].high}"></td><td class="mono">${esc(v.unit)}</td><td class="mono dim">${v.low} &ndash; ${v.high}</td></tr>`).join('')}
      </tbody></table><p class="hint">Unchecked factors stay at the baseline setup. Two factors or more are required.</p></div>
    <div class="panel"><h2>3 &middot; Objective &amp; constraint</h2><div class="grid g2"><div class="field"><label>Objective</label><input value="Minimise lap time" disabled></div>
      <div class="field"><label for="minstab">Minimum stability score (constraint)</label><input id="minstab" type="number" step="any" min="0" max="100" value="85"><div class="hint">Setups below this are infeasible and are never recommended.</div></div></div>
      <label><input type="checkbox" id="tyre"> also minimise tyre degradation (multi-objective; adds a Pareto view)</label></div>
  </div><div>
    <div class="panel"><h2>4 &middot; Design</h2><div class="field"><label for="method">Method</label><select id="method">${META.methods.map((m) => `<option value="${m.id}">${esc(m.id.replaceAll('_', ' '))}</option>`).join('')}</select><div class="hint" id="mdesc"></div></div>
      <div class="grid g2"><div class="field"><label for="reps">Replicates</label><input id="reps" type="number" min="1" max="10" value="2"></div><div class="field"><label for="cp">Centre points</label><input id="cp" type="number" min="0" max="10" value="2"></div>
      <div class="field"><label for="nruns">Runs (random only)</label><input id="nruns" type="number" min="3" max="200" value="16"></div><div class="field"><label for="budget">Run budget</label><input id="budget" type="number" min="4" max="200" value="40"></div>
      <div class="field"><label for="seed">Random seed</label><input id="seed" type="number" min="0" value="1"></div></div></div>
    <div class="panel"><h2>5 &middot; Simulator</h2><div class="grid g2"><div class="field"><label for="lat">Seconds per run</label><input id="lat" type="number" step="any" min="0" max="5" value="0.4"></div>
      <div class="field"><label for="laps">Laps per stint</label><input id="laps" type="number" min="3" max="20" value="5"></div><div class="field"><label for="retries">Retries on failure</label><input id="retries" type="number" min="0" max="5" value="1"></div>
      <div class="field"><label for="fault">Demo fault rate (0-1)</label><input id="fault" type="number" step="any" min="0" max="1" value="0"></div></div><p class="hint">Fault injection makes the first attempt of some runs fail, to demonstrate retries and failure handling.</p></div>
    <button class="btn primary" type="submit" style="width:100%;padding:12px">Generate design</button></div></form>`;
  const md = () => { $('#mdesc').textContent = META.methods.find((m) => m.id === $('#method').value).description; };
  $('#method').addEventListener('change', md); md();
  $('#f').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const factors = {};
    Object.keys(META.factors).forEach((k) => { factors[k] = { enabled: $(`[data-en="${k}"]`).checked, low: parseFloat($(`[data-lo="${k}"]`).value), high: parseFloat($(`[data-hi="${k}"]`).value) }; });
    const body = { name: $('#name').value, hypothesis: $('#hyp').value, factors, min_stability: parseFloat($('#minstab').value), also_minimise_tyre_wear: $('#tyre').checked,
      design: { method: $('#method').value, replicates: +$('#reps').value, center_points: +$('#cp').value, n_runs: +$('#nruns').value, run_budget: +$('#budget').value, seed: +$('#seed').value },
      simulator: { latency_seconds: parseFloat($('#lat').value), stint_laps: +$('#laps').value, max_retries: +$('#retries').value, fault_rate: parseFloat($('#fault').value) } };
    try { const d = await api('POST', '/api/experiments', body); toast('Design generated', true); location.hash = `#/e/${d.id}/design`; }
    catch (e) { $('#err').innerHTML = errorsBox(e); window.scrollTo(0, 0); }
  });
}

/* ------------------------------------------------------------------ design */
const codedChip = (v) => (v === undefined || v === null ? '' : `<span class="coded ${v < -0.01 ? 'm1' : v > 0.01 ? 'p1' : 'z'}">${v > 0 ? '+' : ''}${fmt(v, Math.abs(v) % 1 ? 2 : 0)}</span>`);
async function viewDesign(id) {
  const d = await api('GET', `/api/experiments/${id}`);
  const g = d.design, names = d.factors.map((f) => f.name);
  const letters = g.factor_letters || {}, byLetter = Object.fromEntries(Object.entries(letters).map(([n, l]) => [l, n]));
  const alias = g.alias_structure ? Object.entries(g.alias_structure).map(([e, al]) => `<tr><td class="mono">${[...e].map((c) => esc(byLetter[c] || c)).join(' &times; ')}</td><td class="mono dim">${al.map((a) => [...a].map((c) => esc(byLetter[c] || c)).join(' &times; ')).join(' &nbsp;=&nbsp; ')}</td></tr>`).join('') : '';
  view().innerHTML = expHeader(d, 'design') + `<div class="kpis">
    <div class="kpi"><b>${esc(g.method.replaceAll('_', ' '))}</b><span>design</span></div>
    <div class="kpi"><b>${g.k !== undefined && g.p !== undefined ? `2<sup>${g.k}${g.p ? '-' + g.p : ''}</sup>` : g.runs_planned}</b><span>${g.p ? 'fractional design' : 'design size'}</span></div>
    <div class="kpi"><b>${g.runs_planned}</b><span>planned runs</span></div><div class="kpi"><b>${g.resolution ? 'Res ' + ['', '', '', 'III', 'IV', 'V', 'VI'][g.resolution] : 'full'}</b><span>resolution</span></div>
    <div class="kpi"><b>${esc(g.seed)}</b><span>seed (reproducible)</span></div></div>
    ${g.defining_relation?.length ? `<div class="note info">Defining relation <code>${esc(g.defining_relation.join(', '))}</code> &mdash; main effects are aliased with higher-order interactions; two-factor interactions are aliased in pairs (see below).</div>` : ''}
    ${(g.limitations || []).map((l) => `<div class="note">${esc(l.replace('see alias_structure', 'see the alias table'))}</div>`).join('')}
    <div class="grid g21"><div class="panel"><h2>Generated setups (run order is randomised)</h2><div style="overflow:auto;max-height:520px"><table><thead><tr><th>#</th>${names.map((n) => `<th>${esc(flabel(d, n))}</th>`).join('')}<th>Replicate</th><th>Point</th></tr></thead><tbody>
      ${d.runs.map((r) => `<tr><td class="mono">${r.order + 1}</td>${names.map((n) => `<td class="num">${fmt(r.configuration[n], 2)} ${codedChip(r.coded[n])}</td>`).join('')}<td class="mono">${r.replicate + 1}</td><td class="dim">${esc(r.point_kind || (r.iteration ? 'optimiser ' + r.iteration : 'design'))}</td></tr>`).join('')}</tbody></table></div></div>
    <div><div class="panel"><h2>Design details</h2><table><tbody><tr><td class="dim">Randomisation</td><td>${esc(g.randomization || '')}</td></tr><tr><td class="dim">Control setup</td><td class="mono">${names.map((n) => esc(n) + '=' + fmt(g.control[n], 2)).join('<br>')}</td></tr>
      <tr><td class="dim">Constraint</td><td>${isLive(d) ? `valid laps &ge; ${esc(d.min_valid_laps)}, lap spread &le; ${esc(d.max_lap_std)} s` : `stability &ge; ${esc(d.min_stability)}`}</td></tr><tr><td class="dim">Budget</td><td>${d.run_budget} runs</td></tr></tbody></table>
      <p class="hint">Coded levels: <span class="coded m1">-1</span> low end of range, <span class="coded p1">+1</span> high end, <span class="coded z">0</span> centre.</p></div>
      ${alias ? `<div class="panel"><h2>Alias structure</h2><table><tbody>${alias}</tbody></table></div>` : ''}</div></div>
    <div class="row"><button class="btn primary" id="go" ${d.counts.queued ? '' : 'disabled'}>${isLive(d) ? 'Open run monitor' : `Run ${d.counts.queued} queued setups`}</button><span class="dim">${isLive(d) ? 'Each setup is driven by you in the game and verified from its UDP output.' : 'Runs execute on the synthetic simulator, 4 at a time.'}</span></div>${isLive(d) && d.catalog_verified === false ? '<div class="note">The game setup fields used here have not been verified with the probe script yet.</div>' : ''}`;
  $('#go').addEventListener('click', async () => { try { if (!isLive(d)) await api('POST', `/api/experiments/${id}/execute`); location.hash = `#/e/${id}/runs`; } catch (e) { toast(e.message); } });
}

/* ------------------------------------------------------------- live simulator (EA SPORTS F1 25 over UDP) */
const stintButtons = (r, ls) => (ls.stint && ls.stint.run_id === r.run_id
  ? `<button class="btn small primary" data-submit="${esc(r.run_id)}">Submit stint</button>`
  : `<button class="btn small" data-start="${esc(r.run_id)}" ${ls.listening && !ls.stint ? '' : 'disabled'}>Start stint</button>`);

function liveRunPanel(d, ls) {
  const act = ls.stint && ls.stint.experiment_id === d.id ? d.runs.find((r) => r.run_id === ls.stint.run_id) : null;
  const rows = act ? d.factors.map((f) => {
    const key = META.game.factors[f.name]?.packet_field, got = ls.current_setup ? ls.current_setup[key] : undefined, want = act.configuration[f.name];
    const ok = got !== undefined && got !== null && Math.abs(got - want) < 0.011;
    return `<tr><td>${esc(f.label)}</td><td class="num">${fmt(want, 0)}</td><td class="num">${got === undefined || got === null ? '&mdash;' : fmt(got, 0)}</td><td>${ok ? '<span class="good">match</span>' : '<span class="badc">differs</span>'}</td></tr>`;
  }).join('') : '';
  const link = '<a href="#/live">Live simulator page</a>';
  const state = !ls.listening ? `<div class="note">Not listening for the game. ${link}.</div>`
    : ls.connected ? '' : `<div class="note">Listening, but no packets for ${esc(ls.seconds_since_last_packet ?? '?')} s. Check the game's UDP settings. ${link}.</div>`;
  const body = act
    ? `<p class="hint">Stint in progress on run ${act.order + 1}. Laps recorded in this session so far: ${esc(ls.laps_recorded)}. Drive at least ${d.min_valid_laps + 1} laps (the first is the out-lap), then press <b>Submit stint</b>.</p>
       <table><thead><tr><th>Setup</th><th class="num">Target</th><th class="num">Game reports</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
    : '<p class="hint">1. Set a run\'s target setup in the game garage (see the Design tab). 2. Press <b>Start stint</b> on that run. 3. Drive your laps. 4. Press <b>Submit stint</b>. A stint is accepted only if the game reported exactly the target setup for the whole stint.</p>';
  return `<div class="panel"><h2>Run instructions</h2>${state}${body}${STINT_MSG ? `<div class="errors" role="alert"><b>Stint not accepted:</b> ${esc(STINT_MSG)}</div>` : ''}</div>`;
}

/* ------------------------------------------------- live telemetry charts */
const WHEELS = ['RL', 'RR', 'FL', 'FR'];
/* Bucket downsampling that keeps each bucket's min and max y so peaks (brake/throttle transitions) survive. */
function downsample(pts, max = 300) {
  if (pts.length <= max) return pts;
  const nb = Math.max(1, Math.floor(max / 2)), size = pts.length / nb, out = [];
  for (let b = 0; b < nb; b++) {
    const seg = pts.slice(Math.floor(b * size), Math.floor((b + 1) * size));
    if (!seg.length) continue;
    let mn = seg[0], mx = seg[0];
    seg.forEach((p) => { if (p.y < mn.y) mn = p; if (p.y > mx.y) mx = p; });
    out.push(mn, mx);
  }
  return out.sort((a, b) => a.x - b.x);
}
const seriesFrom = (samples, fn, name, color) => ({ name, color, pts: downsample(samples.map((s) => ({ x: s.session_time, y: fn(s) })).filter((p) => p.y !== null && p.y !== undefined && !Number.isNaN(p.y))) });
const wheelSeries = (samples, field, name, color) => WHEELS.map((w, i) => ({ name: `${name} ${w}`, color, pts: downsample(samples.map((s) => ({ x: s.session_time, y: s[field]?.[i] ?? null })).filter((p) => p.y !== null)) }));

let liveChartKey = '';
async function updateLiveCharts(s) {
  const box = $('#live-charts'); if (!box) return;
  const note = $('#live-tel-note');
  if (note) note.innerHTML = s.connected ? '' : ' &middot; <span class="badc">no recent packets</span>';
  let t;
  try { t = await api('GET', '/api/live/telemetry?scope=live&limit=1500'); }
  catch (e) { box.innerHTML = `<div class="errors">${esc(e.message)}</div>`; return; }
  const samples = t.samples || [];
  // Only rebuild the SVGs when the stream actually advanced; identical polls are a no-op.
  const key = `${t.count ?? samples.length}:${samples.length ? samples[samples.length - 1].session_time : 0}`;
  if (key === liveChartKey) return;
  liveChartKey = key;
  if (!samples.length) { box.innerHTML = '<div class="empty" style="grid-column:1/-1">No telemetry packets yet &mdash; the game must be in a session and sending UDP telemetry.</div>'; return; }
  box.innerHTML = renderTelemetryCharts(samples);
}

/* Shared renderer for decoded UDP telemetry samples (live buffer or an archived stint series). */
function renderTelemetryCharts(samples) {
  const pct = (v) => v * 100;
  const charts = [
    { title: 'Speed', yLabel: 'km/h', series: [seriesFrom(samples, (s) => s.speed_kph, 'speed', '#e10600')] },
    { title: 'Throttle &amp; brake', yLabel: 'input (%)', series: [seriesFrom(samples, (s) => pct(s.throttle), 'throttle', '#2ecc71'), seriesFrom(samples, (s) => pct(s.brake), 'brake', '#e10600')] },
    { title: 'Steering', yLabel: 'steer (%)', series: [seriesFrom(samples, (s) => pct(s.steer), 'steer', '#4aa8ff')] },
    { title: 'Engine RPM', yLabel: 'rpm', series: [seriesFrom(samples, (s) => s.rpm, 'rpm', '#ffb400')] },
    { title: 'Gear', yLabel: 'gear', series: [seriesFrom(samples, (s) => s.gear, 'gear', '#c99bff')] },
    { title: 'Clutch', yLabel: 'clutch', series: [seriesFrom(samples, (s) => s.clutch, 'clutch', '#95a5a6')] },
    { title: 'DRS', yLabel: 'state', series: [seriesFrom(samples, (s) => s.drs, 'drs', '#00cec9')] },
    { title: 'Rev lights', yLabel: '%', series: [seriesFrom(samples, (s) => s.rev_lights_percent, 'rev lights', '#ff9f43')] },
    { title: 'Tyre surface temperature', yLabel: '&deg;C', series: wheelSeries(samples, 'tyre_surface_temperature_c', 'tyre', '#ff9f43') },
    { title: 'Tyre inner temperature', yLabel: '&deg;C', series: wheelSeries(samples, 'tyre_inner_temperature_c', 'tyre inner', '#e17055') },
    { title: 'Tyre pressure', yLabel: 'psi', series: wheelSeries(samples, 'tyre_pressure_psi', 'tyre', '#00cec9') },
    { title: 'Brake temperature', yLabel: '&deg;C', series: wheelSeries(samples, 'brake_temperature_c', 'brake', '#d63031') },
    { title: 'Track surface type', yLabel: 'type', series: wheelSeries(samples, 'surface_type', 'surface', '#6c5ce7') },
  ];
  return charts.map((c) => `<div class="panel" style="margin-bottom:0"><h2>${c.title}</h2>${Charts.lines({ series: c.series, xLabel: 'session time (s)', yLabel: c.yLabel, label: c.title })}</div>`).join('');
}

function liveStatusRows(s) {
  return `<tr><td class="dim">Listening</td><td>${s.listening ? '<span class="good">yes</span>' : 'no'}</td></tr>
    <tr><td class="dim">Receiving packets</td><td>${s.connected ? '<span class="good">yes</span>' : '<span class="badc">no</span>'}</td></tr>
    <tr><td class="dim">Packets accepted</td><td class="num">${esc(s.packets ?? '&mdash;')}</td></tr>
    <tr><td class="dim">Wrong format</td><td class="num ${s.wrong_format ? 'badc' : ''}">${esc(s.wrong_format ?? 0)}</td></tr>
    <tr><td class="dim">Malformed</td><td class="num ${s.malformed ? 'badc' : ''}">${esc(s.malformed ?? 0)}</td></tr>
    <tr><td class="dim">Laps in this session</td><td class="num">${esc(s.laps_recorded)}</td></tr>
    <tr><td class="dim">Game session</td><td class="mono">${esc(s.session?.uid ?? '&mdash;')} &middot; car ${esc(s.session?.player_car ?? '&mdash;')} &middot; fmt ${esc(s.session?.packet_format ?? '&mdash;')}</td></tr>
    <tr><td class="dim">Telemetry samples</td><td class="num">${esc(s.telemetry?.samples ?? 0)}${s.telemetry?.samples ? ` &middot; top ${esc(s.telemetry.top_speed_kph)} km/h` : ''}</td></tr>
    <tr><td class="dim">Last error</td><td>${s.last_error ? `<span class="badc">${esc(s.last_error)}</span>` : '&mdash;'}</td></tr>`;
}

async function viewLive() {
  // Static shell rendered once; the 1 s poll updates only the status table, setup panel and charts
  // so form fields and scroll position are never reset.
  view().innerHTML = `<div id="live-root"><h1>Live simulator</h1><p class="sub">Receive EA SPORTS F1 25 UDP telemetry on this computer. Setups come from the game itself, so every stint can be checked against the setup the experiment asked for.</p><div id="err"></div>
    <div class="grid g2"><div class="panel"><h2>Receiver</h2>
      <div class="grid g2"><div class="field"><label for="lhost">IP address</label><input id="lhost" value="127.0.0.1"></div><div class="field"><label for="lport">Port</label><input id="lport" type="number" min="1024" max="65535" value="${META.game.default_port}"></div></div>
      <div class="row"><button class="btn primary" id="lcon">Start listening</button><button class="btn" id="ldis" disabled>Stop</button></div>
      <h3>In the game</h3><p class="hint">Settings &rsaquo; Telemetry Settings: UDP Telemetry <b>On</b>, IP address and port as above, send rate 20 Hz or more, UDP format <b>F1 25</b> or <b>F1 25: 2026 Season Pack</b> (this app decodes packet format <span id="live-formats">&hellip;</span>).</p></div>
    <div class="panel"><h2>Status</h2><table><tbody id="live-status"><tr><td class="dim">Loading&hellip;</td></tr></tbody></table><div id="live-extra"></div></div></div>
    <div class="panel"><h2>Current setup reported by the game</h2><div id="live-setup"><p class="hint">Waiting for the first status update&hellip;</p></div></div>
    <div class="panel"><h2>Live telemetry<span class="dim" id="live-tel-note"></span></h2><p class="hint">Decoded from the game's UDP packets. Last 1500 samples, peak-preserving downsampled for display.</p><div class="grid g2" id="live-charts"><div class="empty" style="grid-column:1/-1">Waiting for telemetry packets&hellip;</div></div></div>
  </div>`;
  const draw = async () => {
    const s = await api('GET', '/api/live/status');
    if (!document.getElementById('live-root')) return s;  // user navigated away; poll stops itself
    $('#live-status').innerHTML = liveStatusRows(s);
    const fmts = $('#live-formats'); if (fmts && s.accepted_formats) fmts.textContent = s.accepted_formats.join(', ');
    $('#live-extra').innerHTML = s.wrong_format ? `<div class="note">${esc(s.format_help)}</div>` : '';
    const setup = s.current_setup ? Object.entries(s.current_setup).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="num">${esc(fmt(v, Number.isInteger(v) ? 0 : 2))}</td></tr>`).join('') : '';
    $('#live-setup').innerHTML = setup ? `<table><tbody>${setup}</tbody></table>` : '<p class="hint">Waiting for a Car Setups packet (about twice a second while the game is in a session).</p>';
    $('#lhost').disabled = $('#lport').disabled = s.listening;
    $('#lcon').disabled = s.listening; $('#ldis').disabled = !s.listening;
    updateLiveCharts(s);
    return s;
  };
  $('#lcon').addEventListener('click', async () => { try { await api('POST', '/api/live/connect', { host: $('#lhost').value, port: +$('#lport').value }); draw(); } catch (e) { $('#err').innerHTML = errorsBox(e); } });
  $('#ldis').addEventListener('click', async () => { await api('POST', '/api/live/disconnect'); draw(); });
  const first = await draw();
  poll(async () => { try { await draw(); } catch (_) { /* transient */ } }, 1000);
  return first;
}

async function viewNewLive() {
  const gf = META.game.factors, defaults = { front_wing: ['20,25,30', 25], rear_wing: ['24,29,34', 29], on_throttle_diff: ['40,50,60', 50], rear_anti_roll_bar: ['8,11,14', 11] };
  const methods = META.methods.filter((m) => !META.game.unsupported_methods.includes(m.id));
  view().innerHTML = `<h1>New experiment</h1>${newModeCards('live')}
  <p class="sub">You set each generated setup in the game and drive a stint; the app records the laps and checks the game really used that setup.</p><div id="err"></div>
  <form id="f" class="grid g21"><div>
    <div class="panel"><h2>1 &middot; Experiment</h2><div class="field"><label for="name">Name</label><input id="name" value="Aero and differential screening" maxlength="80"></div>
      <div class="field"><label for="hyp">Hypothesis</label><textarea id="hyp" style="min-height:60px">More rear wing costs straight-line speed but improves lap time on this track.</textarea></div></div>
    <div class="panel"><h2>2 &middot; Setup variables</h2><table><thead><tr><th>Use</th><th>Setup</th><th>Values you will set (comma separated, 2&ndash;7)</th><th>Baseline</th><th>Mapping</th></tr></thead><tbody>
      ${Object.entries(gf).map(([k, v]) => `<tr><td><input type="checkbox" data-en="${k}" checked></td><td>${esc(v.label)}<div class="dim mono">${k}</div></td>
      <td><input data-vals="${k}" value="${defaults[k]?.[0] || ''}" aria-label="${esc(v.label)} values"></td><td><input type="number" step="any" data-base="${k}" value="${defaults[k]?.[1] ?? ''}" aria-label="${esc(v.label)} baseline"></td>
      <td>${Object.entries(v.verification || {}).map(([variant, s]) => `<div>${s.verified ? '<span class="chip completed"' : '<span class="chip waiting_for_measurements"'} title="${esc(s.source)}">${esc((META.game.variants || {})[variant] || variant)}: ${s.verified ? 'verified' : 'not verified'}</span></div>`).join('')
        || (v.verified ? '<span class="chip completed">verified</span>' : '<span class="chip waiting_for_measurements" title="Run scripts/probe_setup.py, then scripts/verify_setup_mapping.py">not verified</span>')}</td></tr>`).join('')}</tbody></table>
      <p class="hint">The example values are placeholders: use values your game's garage actually offers. The baseline must be one of them.</p></div>
    <div class="panel"><h2>3 &middot; Objective &amp; constraints</h2><div class="grid g2"><div class="field"><label>Objective</label><input value="Minimise mean lap time" disabled></div>
      <div class="field"><label for="minlaps">Valid timed laps per stint</label><input id="minlaps" type="number" min="2" max="20" value="3"></div>
      <div class="field"><label for="maxstd">Max lap-time spread (s)</label><input id="maxstd" type="number" step="any" min="0.01" max="30" value="1.5"><div class="hint">Consistency check: standard deviation of the stint's lap times.</div></div></div></div>
  </div><div>
    <div class="panel"><h2>4 &middot; Design</h2><div class="field"><label for="method">Method</label><select id="method">${methods.map((m) => `<option value="${m.id}">${esc(m.id.replaceAll('_', ' '))}</option>`).join('')}</select><div class="hint" id="mdesc"></div></div>
      <div class="grid g2"><div class="field"><label for="reps">Replicates</label><input id="reps" type="number" min="1" max="10" value="2"></div><div class="field"><label for="nruns">Runs (random only)</label><input id="nruns" type="number" min="3" max="200" value="12"></div>
      <div class="field"><label for="budget">Run budget (stints)</label><input id="budget" type="number" min="4" max="200" value="40"></div><div class="field"><label for="seed">Random seed</label><input id="seed" type="number" min="0" value="1"></div></div>
      <p class="hint">Central composite and Box-Behnken need continuous settings and are not offered: the game only accepts fixed steps.</p></div>
    <button class="btn primary" type="submit" style="width:100%;padding:12px">Generate design</button></div></form>`;
  const md = () => { $('#mdesc').textContent = META.methods.find((m) => m.id === $('#method').value).description; };
  $('#method').addEventListener('change', md); md();
  $('#f').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const factors = {};
    Object.keys(gf).forEach((k) => {
      if (!$(`[data-en="${k}"]`).checked) return;
      factors[k] = { values: $(`[data-vals="${k}"]`).value.split(',').map((x) => x.trim()).filter(Boolean).map(Number), baseline: parseFloat($(`[data-base="${k}"]`).value) };
    });
    const body = { name: $('#name').value, hypothesis: $('#hyp').value, mode: LIVE, live: { factors, min_valid_laps: +$('#minlaps').value, max_lap_std: parseFloat($('#maxstd').value) },
      design: { method: $('#method').value, replicates: +$('#reps').value, n_runs: +$('#nruns').value, run_budget: +$('#budget').value, seed: +$('#seed').value } };
    try { const d = await api('POST', '/api/experiments', body); toast('Design generated', true); location.hash = `#/e/${d.id}/design`; }
    catch (e) { $('#err').innerHTML = errorsBox(e); window.scrollTo(0, 0); }
  });
}

/* ------------------------------------------------------------- run monitor */
function runCharts(d) {
  const done = d.runs.filter((r) => r.status === 'completed' && r.outcomes.lap_time !== undefined).sort((a, b) => a.order - b.order);
  let best = Infinity; const step = [];
  done.forEach((r) => { if (r.feasible && r.outcomes.lap_time < best) best = r.outcomes.lap_time; if (best < Infinity) step.push({ x: r.order + 1, y: best }); });
  const bestId = d.best?.run_id;
  const pts = done.map((r) => ({ x: r.order + 1, y: r.outcomes.lap_time, cls: r.run_id === bestId ? 'pt-best' : r.feasible ? 'pt-ok' : 'pt-bad', r: r.run_id === bestId ? 7 : 4.5, title: `Run ${r.order + 1}: ${fmt(r.outcomes.lap_time, 3)} s, ${SECOND(d).name} ${fmt(r.outcomes[SECOND(d).key], SECOND(d).dp)}${r.feasible ? '' : ' (INFEASIBLE)'}` }));
  const sc = done.filter((r) => r.outcomes[SECOND(d).key] !== undefined).map((r) => ({ x: r.outcomes.lap_time, y: r.outcomes[SECOND(d).key], cls: r.run_id === bestId ? 'pt-best' : r.feasible ? 'pt-ok' : 'pt-bad', r: r.run_id === bestId ? 7 : 4.5, title: `Run ${r.order + 1}` }));
  return `<div class="grid g2"><div class="panel"><h2>Lap time by run</h2>${Charts.scatter({ points: pts, step, xLabel: 'run', yLabel: 'mean lap (s)', label: 'lap time by run' })}
    <div class="legend"><span><i style="background:var(--green)"></i>feasible</span><span><i style="background:var(--bad)"></i>${isLive(d) ? 'violates a constraint' : 'violates stability'}</span><span><i style="background:var(--amber)"></i>best feasible</span><span>line = best feasible so far</span></div></div>
    <div class="panel"><h2>${cap(SECOND(d).name)} vs lap time</h2>${Charts.scatter({ points: sc, lines: d.min_stability != null ? [{ h: d.min_stability, label: `min ${d.min_stability}` }] : [], shade: d.min_stability != null ? { y0: 0, y1: d.min_stability } : null, ymin: sc.length ? Math.min(...sc.map((p) => p.y), d.min_stability - 3) : undefined, xLabel: 'mean lap (s)', yLabel: 'stability score', label: 'stability vs lap time' })}
    <div class="legend"><span>shaded area = infeasible region</span></div></div></div>`;
}
async function viewRuns(id) {
  view().innerHTML = '<div id="runs-root"></div>';
  let open = null;
  const draw = async () => {
    const d = await api('GET', `/api/experiments/${id}`);
    const live = isLive(d), ls = live ? await api('GET', '/api/live/status') : null;
    const root = $('#runs-root'); if (!root) return d;
    const names = d.factors.map((f) => f.name), c = d.counts, active = c.running + c.queued > 0 && d.status !== 'created';
    setLedFor(d);
    root.innerHTML = expHeader(d, 'runs') + `<div class="kpis"><div class="kpi"><b>${c.completed}</b><span>completed</span></div><div class="kpi"><b>${c.running}</b><span>running</span></div><div class="kpi"><b>${c.queued}</b><span>queued</span></div>
      <div class="kpi ${c.failed ? 'bad' : ''}"><b>${c.failed}</b><span>failed</span></div><div class="kpi good"><b>${d.best ? fmt(d.best.lap_time, 3) : '&mdash;'}</b><span>best feasible lap (s)</span></div></div>
      <div class="row" style="margin-bottom:12px">${live ? '' : `<button class="btn primary" id="run" ${c.queued && !c.running ? '' : 'disabled'}>Run queued</button><button class="btn" id="cancel" ${c.queued || c.running ? '' : 'disabled'}>Cancel</button>
      <button class="btn" id="retry" ${c.failed ? '' : 'disabled'}>Retry ${c.failed} failed</button>`}<span class="spacer"></span><a class="btn" href="/api/experiments/${esc(id)}/export/runs.csv">Export runs CSV</a></div>
      ${live ? liveRunPanel(d, ls) : ''}${runCharts(d)}<div class="panel"><h2>Runs</h2><div style="overflow:auto;max-height:560px"><table><thead><tr><th>#</th><th>Status</th>${names.map((n) => `<th class="num">${esc(flabel(d, n))}</th>`).join('')}<th class="num">Lap (s)</th><th class="num">${cap(SECOND(d).name)}</th><th>Feasible</th><th class="num">Tries</th><th>Source</th><th></th></tr></thead><tbody>
      ${d.runs.map((r) => `<tr><td class="mono">${r.order + 1}</td><td>${statusChip(r.status)}${r.status === 'running' ? ' <i class="led run"></i>' : ''}</td>${names.map((n) => `<td class="num">${fmt(r.configuration[n], 2)}</td>`).join('')}
      <td class="num">${r.outcomes.lap_time !== undefined ? fmt(r.outcomes.lap_time, 3) : ''}</td><td class="num">${r.outcomes[SECOND(d).key] !== undefined ? fmt(r.outcomes[SECOND(d).key], SECOND(d).dp) : ''}</td>
      <td>${r.feasible === true ? '<span class="good">yes</span>' : r.feasible === false ? '<span class="badc">NO</span>' : ''}</td><td class="num">${r.attempts}</td>
      <td>${r.imported ? (live ? '<span class="chip simrec">sim-recorded</span>' : '<span class="chip imported">imported</span>') : r.status === 'completed' ? '<span class="chip synthetic">synthetic</span>' : ''}${r.iteration ? ` <span class="chip model">opt ${r.iteration}</span>` : ''}</td>
      <td>${r.error ? `<span class="badc" title="${esc(r.error)}">${esc(r.error.slice(0, 60))}</span>` : live && r.status === 'completed' ? `<a href="/api/experiments/${esc(id)}/runs/${esc(r.run_id)}/evidence" target="_blank" rel="noopener">evidence</a>` : r.status === 'queued' && live ? stintButtons(r, ls) : r.status === 'queued' ? `<button class="btn small" data-imp="${esc(r.run_id)}" data-n="${r.order + 1}">Import telemetry</button>` : ''}</td></tr>`).join('')}</tbody></table></div></div>`;
    $('#run')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${id}/execute`); poll(draw, 700); draw(); } catch (e) { toast(e.message); } });
    $('#cancel')?.addEventListener('click', async () => { await api('POST', `/api/experiments/${id}/cancel`); draw(); });
    $('#retry')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${id}/retry`); poll(draw, 700); draw(); } catch (e) { toast(e.message); } });
    document.querySelectorAll('[data-imp]').forEach((b) => b.addEventListener('click', () => importModal(id, b.dataset.imp, b.dataset.n, draw)));
    document.querySelectorAll('[data-start]').forEach((b) => b.addEventListener('click', async () => {
      STINT_MSG = '';
      try { await api('POST', `/api/experiments/${id}/runs/${b.dataset.start}/stint/start`); toast('Stint started: drive your laps, then press Submit stint', true); } catch (e) { STINT_MSG = e.message; }
      draw();
    }));
    document.querySelectorAll('[data-submit]').forEach((b) => b.addEventListener('click', async () => {
      try { await api('POST', `/api/experiments/${id}/runs/${b.dataset.submit}/stint/submit`); STINT_MSG = ''; toast('Stint accepted and recorded', true); } catch (e) { STINT_MSG = e.message; }
      draw();
    }));
    return d;
  };
  const d = await draw();
  poll(async () => { try { const x = await draw(); if (x.counts.running + x.counts.queued === 0) { clearInterval(pollTimer); setLed('ok', 'idle'); } } catch (_) {} }, 700);
  return d;
}
function setLedFor(d) { setLed(d.counts.running > 0 ? 'run' : 'ok', d.counts.running > 0 ? `running ${d.counts.running}` : 'online'); }

/* --------------------------------------------------------------- telemetry */
function importPanelHtml(prefix = '') {
  return `<div class="panel"><h2>Import recorded telemetry (CSV)</h2><p class="hint">Columns: <code>${META.telemetry_columns.join(', ')}</code>. Sector times are required; <code>lap_time</code> defaults to the sector sum.</p>
    <div class="row"><input type="file" id="${prefix}file" accept=".csv,text/csv" style="width:auto"><select id="${prefix}unit" style="width:auto"><option value="s">times in seconds</option><option value="ms">times in milliseconds</option></select>
    <button class="btn small" id="${prefix}demo1">Load demo (s)</button><button class="btn small" id="${prefix}demo2">Load demo (ms)</button></div>
    <div class="field" style="margin-top:10px"><textarea id="${prefix}csv" placeholder="lap,sector_1,sector_2,sector_3,..."></textarea></div><div id="${prefix}perr"></div>
    <div class="row"><button class="btn primary" id="${prefix}prev">Preview &amp; validate</button><span id="${prefix}extra"></span></div></div><div id="${prefix}out"></div>`;
}
function previewHtml(p) {
  const s = p.summary;
  return `${p.errors.length ? `<div class="note"><b>${p.errors.length} problem(s)</b><ul class="warnlist">${p.errors.slice(0, 8).map((e) => `<li>${esc(e)}</li>`).join('')}</ul>Rows with problems are skipped.</div>` : ''}
    ${s ? `<div class="kpis"><div class="kpi"><b>${s.laps}</b><span>laps</span></div><div class="kpi good"><b>${fmt(s.best_lap, 3)}</b><span>best lap (s)</span></div><div class="kpi"><b>${fmt(s.mean_lap, 3)}</b><span>mean lap (s)</span></div>
    <div class="kpi ${s.outlier_laps.length ? 'bad' : ''}"><b>${s.outlier_laps.length}</b><span>flagged laps</span></div></div>${s.unit_converted ? '<div class="note info">Times were converted from milliseconds to seconds by TRIALFORGE (raw values kept).</div>' : ''}
    <div class="grid g2"><div class="panel"><h2>Sector times</h2>${Charts.sectors({ laps: p.laps })}</div><div class="panel"><h2>Lap time</h2>${Charts.lines({ series: [{ name: 'lap time (s)', color: '#e10600', pts: p.laps.map((l) => ({ x: l.lap, y: l.lap_time, flag: l.outlier })) }], xLabel: 'lap', yLabel: 'seconds' })}</div></div>${lapTable(p.laps)}` : ''}`;
}
const lapTable = (laps) => `<div class="panel"><h2>Laps</h2><div style="overflow:auto;max-height:380px"><table><thead><tr><th>Lap</th><th class="num">S1</th><th class="num">S2</th><th class="num">S3</th><th class="num">Lap time</th><th>Quality</th></tr></thead><tbody>
  ${laps.map((l) => `<tr class="${l.outlier ? 'outlier' : ''}"><td class="mono">${l.lap}</td><td class="num">${fmt(l.sector_1, 3)}</td><td class="num">${fmt(l.sector_2, 3)}</td><td class="num">${fmt(l.sector_3, 3)}</td><td class="num"><b>${fmt(l.lap_time, 3)}</b></td>
  <td>${Object.entries(l.flags || {}).map(([m, f]) => `<span class="tag">${esc(m)}: ${esc(f.join(', '))}</span>`).join('')}</td></tr>`).join('')}</tbody></table></div></div>`;
function bindImport(prefix, onPreview) {
  const g = (s) => $(`#${prefix}${s}`);
  const load = async (path) => { g('csv').value = await (await fetch(path)).text(); };
  g('file').addEventListener('change', async (e) => { const f = e.target.files[0]; if (f) { g('csv').value = await f.text(); g('csv').dataset.name = f.name; } });
  g('demo1').addEventListener('click', () => load('/api/demo/telemetry.csv').then(() => (g('unit').value = 's')));
  g('demo2').addEventListener('click', () => load('/api/demo/telemetry_ms.csv').then(() => (g('unit').value = 'ms')));
  g('prev').addEventListener('click', async () => {
    g('perr').innerHTML = '';
    try { const p = await api('POST', '/api/telemetry/preview', { csv: g('csv').value, time_unit: g('unit').value }); g('out').innerHTML = previewHtml(p); onPreview?.(p); } catch (e) { g('perr').innerHTML = errorsBox(e); }
  });
  return { csv: () => g('csv').value, unit: () => g('unit').value, name: () => g('csv').dataset.name || 'telemetry.csv' };
}
async function viewTelemetryStandalone() {
  view().innerHTML = `<h1>Telemetry import</h1><p class="sub">Inspect recorded lap and sector data: validation, unit conversion and outlier flagging run through TRIALFORGE. Nothing is stored until you attach it to a run.</p>${importPanelHtml('s-')}`;
  bindImport('s-');
}
async function viewExpTelemetry(id) {
  const d = await api('GET', `/api/experiments/${id}`);
  if (isLive(d)) return viewStintTelemetry(id, d);
  const tel = await api('GET', `/api/experiments/${id}/telemetry`);
  const queued = d.runs.filter((r) => r.status === 'queued');
  view().innerHTML = expHeader(d, 'telemetry') + `<div class="grid g21"><div><div class="panel"><h2>Run telemetry</h2>${tel.length ? `<div class="row"><select id="runsel" style="max-width:420px">${tel.map((t) => `<option value="${esc(t.run_id)}">Run ${t.order + 1} &middot; ${t.status} &middot; ${t.synthetic ? 'synthetic' : 'imported'}</option>`).join('')}</select></div><div id="tel"></div>` : '<div class="empty">No telemetry yet. Execute runs or import a CSV.</div>'}</div></div>
    <div>${importPanelHtml('x-')}</div></div>`;
  if (tel.length) {
    const show = () => {
      const t = tel.find((x) => x.run_id === $('#runsel').value);
      $('#tel').innerHTML = `<div class="row"><span class="chip ${t.synthetic ? 'synthetic' : 'imported'}">${t.synthetic ? 'synthetic' : 'imported'}</span><span class="dim mono">${esc(t.source)}</span></div><div class="mono dim" style="margin:6px 0">${Object.entries(t.configuration).map(([k, v]) => `${esc(k)}=${fmt(v, 2)}`).join(' &middot; ')}</div>
        ${Charts.sectors({ laps: t.laps })}${lapTable(t.laps)}`;
    };
    $('#runsel').addEventListener('change', show); show();
  }
  const imp = bindImport('x-', null);
  $('#x-extra').innerHTML = queued.length ? `<select id="x-run" style="width:auto">${queued.map((r) => `<option value="${esc(r.run_id)}">attach to run ${r.order + 1}</option>`).join('')}</select> <button class="btn" id="x-attach">Attach to run</button>` : '<span class="dim">Attach is available for queued runs.</span>';
  $('#x-attach')?.addEventListener('click', async () => {
    try { const r = await api('POST', `/api/experiments/${id}/runs/${$('#x-run').value}/telemetry`, { csv: imp.csv(), time_unit: imp.unit(), filename: imp.name() }); toast('Telemetry attached; run completed', true); if (r.row_errors.length) toast(`${r.row_errors.length} row(s) skipped`); viewExpTelemetry(id); }
    catch (e) { $('#x-perr').innerHTML = errorsBox(e); }
  });
}
/* Saved stints of a live experiment: the decoded UDP telemetry archived with each submitted stint (accepted or rejected). */
async function viewStintTelemetry(id, d) {
  const idx = await api('GET', `/api/experiments/${id}/telemetry/series-index`);
  const stored = idx.filter((r) => r.samples);
  const runNo = (rid) => (d.runs.find((x) => x.run_id === rid)?.order ?? 0) + 1;
  const opt = (r) => `<option value="${esc(r.run_id)}">Run ${r.order + 1} &middot; ${esc(r.status)} &middot; ${r.samples} samples</option>`;
  view().innerHTML = expHeader(d, 'telemetry') + `<div class="panel"><h2>Stint telemetry inspector</h2>
    <p class="hint">Decoded UDP samples recorded during each submitted stint (SIM-RECORDED), exactly as stored. Pick a stint, choose channels and a time window, hover or replay, or compare two stints. CSV import is not available for live experiments: a CSV cannot prove which setup was driven.</p>
    ${stored.length ? `<div class="row"><select id="tel-run" style="width:auto">${stored.map(opt).join('')}</select><select id="tel-cmp" style="width:auto"><option value="">no comparison</option>${stored.map((r) => `<option value="${esc(r.run_id)}">compare with run ${r.order + 1}</option>`).join('')}</select>
      <button class="btn primary small" id="tel-series">Open recorded telemetry</button><span class="dim" id="tel-series-msg"></span></div><div id="tel-insp"></div>`
    : '<div class="empty">No stint telemetry stored yet. Drive a stint (Run monitor &rarr; Start stint / Submit stint) to record one.</div>'}</div>`;
  $('#tel-series')?.addEventListener('click', async () => {
    const msg = $('#tel-series-msg'), a = $('#tel-run').value, other = $('#tel-cmp').value === a ? '' : $('#tel-cmp').value;
    try {
      const r = await api('GET', `/api/experiments/${id}/runs/${a}/telemetry/series?limit=20000`);
      const b = other ? await api('GET', `/api/experiments/${id}/runs/${other}/telemetry/series?limit=20000`) : null;
      const dl = (rid) => `<a class="btn small" href="/api/experiments/${esc(id)}/runs/${esc(rid)}/telemetry/series.csv" download>CSV, run ${runNo(rid)} (all samples)</a>
        <a class="btn small" href="/api/experiments/${esc(id)}/runs/${esc(rid)}/telemetry/series?limit=20000" download="${esc(rid)}-telemetry.json">JSON, run ${runNo(rid)}</a>`;
      msg.textContent = `${r.count} measured samples${b ? `; comparing with run ${runNo(other)} (${b.count})` : ''}`;
      Inspector.mount($('#tel-insp'), { a: { label: `Run ${runNo(a)}`, samples: r.samples }, b: b && b.samples?.length ? { label: `Run ${runNo(other)}`, samples: b.samples } : null,
        downloads: `<div class="row" style="margin-top:12px"><span class="dim">Download the decoded dataset:</span>${dl(a)}${b ? dl(other) : ''}</div>` });
    } catch (e) { msg.innerHTML = `<span class="badc">${esc(e.message)}</span>`; }
  });
}
function importModal(id, runId, n, done) {
  const bg = document.createElement('div'); bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal"><h2>Import telemetry for run ${esc(n)}</h2>${importPanelHtml('m-')}<div class="row"><button class="btn" id="m-close">Close</button><button class="btn primary" id="m-attach">Attach to run ${esc(n)}</button></div></div>`;
  document.body.appendChild(bg);
  const imp = bindImport('m-', null);
  $('#m-close').onclick = () => bg.remove();
  $('#m-attach').onclick = async () => {
    try { await api('POST', `/api/experiments/${id}/runs/${runId}/telemetry`, { csv: imp.csv(), time_unit: imp.unit(), filename: imp.name() }); bg.remove(); toast('Telemetry attached', true); done(); }
    catch (e) { $('#m-perr').innerHTML = errorsBox(e); }
  };
}

/* ---------------------------------------------------------------- analysis */
/** Collapse the many 'term X is aliased ...' warnings into one readable note. */
function warnBlock(ws) {
  const al = ws.filter((w) => w.includes('is aliased with other terms')), rest = ws.filter((w) => !w.includes('is aliased with other terms'));
  const terms = al.map((w) => (w.match(/term '([^']+)'/) || [])[1]).filter(Boolean).map((t) => t.split(':').map(label).join(' × '));
  return rest.map((w) => `<div class="note">${esc(w)}</div>`).join('') + (terms.length ? `<div class="note info"><b>${terms.length} interaction term(s) are aliased</b> (confounded with other terms) in this fractional design, so they cannot be tested separately: <span class="mono">${terms.map(esc).join('; ')}</span>.</div>` : '');
}
const pfmt = (p) => (p === null || p === undefined ? 'n/a' : p < 0.001 ? '<0.001' : p.toFixed(3));
function recoCard(r) {
  if (!r) return '';
  const ok = r.status === 'recommended';
  return `<div class="reco ${ok ? '' : 'bad'}"><h3>${esc(r.headline)}</h3>${ok ? `<div class="setup">${Object.entries(r.setup).map(([k, v]) => `<div><b>${fmt(v, 2)}</b><span>${esc(label(k))} (${esc(unit(k))})</span></div>`).join('')}</div>` : ''}
    <ul>${(r.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul><details><summary class="dim">Caveats (${(r.caveats || []).length})</summary><ul class="warnlist">${(r.caveats || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul></details></div>`;
}
function interactionPlot(d, a, b) {
  const rows = Object.values(d.runs).filter((r) => r.status === 'completed' && r.coded[a] !== undefined && r.coded[b] !== undefined && Math.abs(r.coded[a]) > 0.99 && Math.abs(r.coded[b]) > 0.99 && r.outcomes.lap_time !== undefined);
  if (rows.length < 4) return '<div class="empty">Needs design runs at the low/high levels of both factors.</div>';
  const mean = (xs) => xs.reduce((s, x) => s + x, 0) / xs.length;
  const series = [-1, 1].map((lb, i) => ({ name: `${label(b)} ${lb < 0 ? 'low' : 'high'}`, color: i ? '#e10600' : '#4aa8ff', pts: [-1, 1].map((la) => { const c = rows.filter((r) => Math.sign(r.coded[a]) === la && Math.sign(r.coded[b]) === lb); return c.length ? { x: la, y: mean(c.map((r) => r.outcomes.lap_time)) } : null; }).filter(Boolean) }));
  return Charts.lines({ series, xLabel: `${label(a)} (-1 low, +1 high)`, yLabel: 'mean lap (s)', xmin: -1.3, xmax: 1.3 });
}
async function viewAnalysis(id) {
  const d = await api('GET', `/api/experiments/${id}`);
  view().innerHTML = expHeader(d, 'analysis') + `<div class="panel"><div class="row"><div><label for="alpha">Significance level</label><select id="alpha" style="width:auto"><option>0.01</option><option selected>0.05</option><option>0.10</option></select></div>
    <label style="margin:18px 0 0"><input type="checkbox" id="boot"> bootstrap intervals</label><button class="btn primary" id="rerun" style="margin-top:14px">Run analysis</button><span class="dim" id="busy"></span></div></div><div id="res"></div>`;
  const run = async () => {
    $('#busy').textContent = ' analysing…'; $('#res').innerHTML = '';
    try {
      const a = await api('POST', `/api/experiments/${id}/analysis`, { alpha: parseFloat($('#alpha').value), bootstrap: $('#boot').checked });
      $('#busy').textContent = '';
      const fx = a.effects, names = d.factors.map((f) => f.name);
      const eff = fx.effects.map((e) => ({ label: label(e.term), value: e.low_to_high, lo: e.ci_95?.[0], hi: e.ci_95?.[1], sig: e.significant, p: e.p_value }));
      const sc = Object.values(a.runs).filter((r) => r.status === 'completed' && r.outcomes[SECOND(d).key] !== undefined).map((r) => ({ x: r.outcomes.lap_time, y: r.outcomes[SECOND(d).key], cls: r.run_id === a.best_run_id ? 'pt-best' : r.feasible ? 'pt-ok' : 'pt-bad', r: r.run_id === a.best_run_id ? 7 : 4.5, title: `Run ${r.order + 1}` }));
      const pair = names.length >= 2 ? [names[0], names[1]] : null;
      $('#res').innerHTML = recoCard(a.recommendation) + `<div class="grid g2"><div class="panel"><h2>Factor effects on lap time (low &rarr; high of tested range)</h2>${Charts.bars({ items: eff, goodNegative: true, xLabel: 'seconds (negative = faster); whiskers = 95% CI; * significant' })}
      <p class="hint">R&sup2; ${fmt(fx.r_squared, 3)} &middot; adj. ${fmt(fx.adjusted_r_squared, 3)} &middot; residual df ${fx.residual_df ?? 'n/a'}. Bars show the <em>linear</em> effect of moving a factor from the low to the high end (regression on coded factors). The ANOVA below treats levels as categories, so it can also flag <em>curvature</em> (centre vs ends) that a straight line misses &mdash; a factor can be non-significant here and significant there.</p></div>
      <div class="panel"><h2>Feasibility: ${SECOND(d).name} vs lap time</h2>${Charts.scatter({ points: sc, lines: d.min_stability != null ? [{ h: d.min_stability, label: `min ${d.min_stability}` }] : [], shade: d.min_stability != null ? { y0: 0, y1: d.min_stability } : null, ymin: sc.length ? Math.min(...sc.map((p) => p.y), d.min_stability - 3) : undefined, xLabel: 'mean lap (s)', yLabel: 'stability' })}
      <div class="legend"><span>${a.feasible_run_ids.length} feasible &middot; ${a.infeasible.length} infeasible</span><span>Pareto-optimal runs: ${a.pareto_run_ids.length}</span></div></div></div>
      <div class="grid g2"><div class="panel"><h2>ANOVA (Type II) &mdash; lap time</h2>${fx.anova && fx.anova.rows.length ? `<table><thead><tr><th>Term</th><th class="num">SS</th><th class="num">df</th><th class="num">F</th><th class="num">p</th></tr></thead><tbody>${fx.anova.rows.map((r) => `<tr><td>${esc(label(r.term))}</td><td class="num">${fmt(r.sum_squares, 4)}</td><td class="num">${r.df}</td><td class="num">${r.f_statistic == null ? '' : fmt(r.f_statistic, 2)}</td><td class="num ${r.p_value != null && r.p_value < a.alpha ? 'good' : ''}">${r.p_value == null ? '' : pfmt(r.p_value)}</td></tr>`).join('')}</tbody></table>${warnBlock(fx.anova.warnings || [])}` : '<div class="note">ANOVA not available (not enough residual degrees of freedom).</div>'}</div>
      <div class="panel"><h2>Interactions</h2>${fx.interactions.length ? `<table><thead><tr><th>Term</th><th class="num">Coef.</th><th class="num">p</th></tr></thead><tbody>${fx.interactions.map((i) => `<tr><td>${esc(i.term.split(':').map(label).join(' × '))}</td><td class="num">${fmt(i.coefficient, 3)}</td><td class="num ${i.significant ? 'good' : ''}">${pfmt(i.p_value)}</td></tr>`).join('')}</tbody></table>` : '<div class="dim">No interaction terms estimable.</div>'}
      ${pair ? `<h3>Interaction plot: ${esc(label(pair[0]))} &times; ${esc(label(pair[1]))}</h3>${interactionPlot(a, pair[0], pair[1])}` : ''}</div></div>
      <div class="panel"><h2>Level comparisons (95% CI)</h2><table><thead><tr><th>Factor</th><th>Level vs reference</th><th class="num">Diff (s)</th><th class="num">95% CI</th><th class="num">p</th><th>Evidence</th></tr></thead><tbody>
      ${a.main_effects.map((e) => `<tr><td>${esc(label(e.factor))}</td><td class="mono">${fmt(e.level, 2)} vs ${fmt(e.control_level, 2)}</td><td class="num">${fmt(e.mean_difference, 3)}</td><td class="num">${e.confidence_interval_95 ? `${fmt(e.confidence_interval_95[0], 3)} .. ${fmt(e.confidence_interval_95[1], 3)}` : 'withheld'}</td><td class="num">${pfmt(e.p_value)}</td><td><span class="chip ${e.evidence === 'sufficient' ? 'completed' : 'cancelled'}">${esc(e.evidence)}</span></td></tr>`).join('') || '<tr><td colspan="6" class="dim">No comparisons</td></tr>'}</tbody></table>
      <p class="hint">Marginal comparisons pool the other factors; the regression/ANOVA above are the adjusted results. Intervals are withheld when replication is insufficient.</p></div>
      ${a.warnings.length || fx.regression_warnings.length ? `<div class="panel"><h2>Warnings</h2><ul class="warnlist">${[...a.warnings, ...fx.regression_warnings].map((w) => `<li>${esc(w.replace(/^aliased\/dependent terms dropped \(earlier terms win\): (.*)$/, (_, t) => 'Terms that could not be estimated separately (aliased in the fractional design): ' + t))}</li>`).join('')}</ul></div>` : ''}
      <div class="panel"><h2>Assumptions &amp; limitations</h2><ul class="warnlist">${[...a.assumptions, ...a.limitations].map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>`;
    } catch (e) { $('#busy').textContent = ''; $('#res').innerHTML = errorsBox(e); }
  };
  $('#rerun').addEventListener('click', run);
  if (d.counts.completed) run(); else $('#res').innerHTML = '<div class="note">No completed runs yet. <a href="#/e/' + esc(id) + '/runs">Open the run monitor</a>.</div>';
}

/* ------------------------------------------------------------ optimisation */
async function viewOptimise(id) {
  view().innerHTML = '<div id="opt-root"></div>';
  let preview = null;
  const draw = async () => {
    const d = await api('GET', `/api/experiments/${id}`), hist = await api('GET', `/api/experiments/${id}/optimise/history`);
    const root = $('#opt-root'); if (!root) return d;
    const names = d.factors.map((f) => f.name), remaining = d.run_budget - d.runs_total, busy = d.counts.running + d.counts.queued > 0;
    const done = d.runs.filter((r) => r.status === 'completed').sort((a, b) => a.order - b.order);
    const pts = done.map((r) => ({ x: r.order + 1, y: r.outcomes.lap_time, cls: r.run_id === d.best?.run_id ? 'pt-best' : r.feasible ? 'pt-ok' : 'pt-bad', r: r.run_id === d.best?.run_id ? 7 : 4.5, title: `Run ${r.order + 1}${r.iteration ? ' (optimiser)' : ''}` }));
    let best = Infinity; const step = []; done.forEach((r) => { if (r.feasible && r.outcomes.lap_time < best) best = r.outcomes.lap_time; if (best < Infinity) step.push({ x: r.order + 1, y: best }); });
    const mine = d.runs.filter((r) => r.iteration);
    root.innerHTML = expHeader(d, 'optimise') + `<div class="kpis"><div class="kpi good"><b>${d.best ? fmt(d.best.lap_time, 3) : '&mdash;'}</b><span>best feasible lap (s)</span></div><div class="kpi"><b>${remaining}</b><span>runs left in budget</span></div><div class="kpi"><b>${mine.length}</b><span>optimiser runs</span></div></div>
    <div class="grid g21"><div class="panel"><h2>Convergence</h2>${Charts.scatter({ points: pts, step, xLabel: 'run', yLabel: 'mean lap (s)' })}<div class="legend"><span><i style="background:var(--green)"></i>feasible</span><span><i style="background:var(--bad)"></i>infeasible</span><span><i style="background:var(--amber)"></i>best</span></div></div>
    <div class="panel"><h2>Next setups</h2><div class="field"><label for="om">Method</label><select id="om">${META.optimisers.map((o) => `<option value="${o.id}">${esc(o.id)}</option>`).join('')}</select><div class="hint" id="omd"></div></div>
      <div class="field"><label for="oc">Setups to propose</label><input id="oc" type="number" min="1" max="10" value="2"></div><div id="oerr"></div>
      <div class="row"><button class="btn" id="oprev" ${d.counts.completed && remaining > 0 ? '' : 'disabled'}>Preview</button><button class="btn primary" id="oapply" ${d.counts.completed && remaining > 0 && !busy ? '' : 'disabled'}>Run recommended</button></div>
      ${busy ? '<div class="note info">Runs in progress&hellip;</div>' : ''}${remaining <= 0 ? '<div class="note">Run budget exhausted.</div>' : ''}</div></div>
    <div id="prev">${preview ? previewTable(preview, names) : ''}</div>
    ${mine.length ? `<div class="panel"><h2>Optimiser runs</h2><table><thead><tr><th>Iter</th><th>#</th>${names.map((n) => `<th class="num">${esc(label(n))}</th>`).join('')}<th class="num">Lap</th><th class="num">Stability</th><th>Feasible</th><th>Status</th></tr></thead><tbody>${mine.map((r) => `<tr><td>${r.iteration}</td><td class="mono">${r.order + 1}</td>${names.map((n) => `<td class="num">${fmt(r.configuration[n], 2)}</td>`).join('')}<td class="num">${r.outcomes.lap_time !== undefined ? fmt(r.outcomes.lap_time, 3) : ''}</td><td class="num">${r.outcomes.stability !== undefined ? fmt(r.outcomes.stability, 1) : ''}</td><td>${r.feasible === true ? '<span class="good">yes</span>' : r.feasible === false ? '<span class="badc">NO</span>' : ''}</td><td>${statusChip(r.status)}</td></tr>`).join('')}</tbody></table></div>` : ''}
    <div class="panel"><h2>What the labels mean</h2><p class="hint"><span class="chip model">model prediction</span> a Gaussian-process surrogate fitted to your runs predicts lap time &plusmn; uncertainty and an Expected Improvement; valid only under its smoothness assumptions.
      <span class="chip search">search</span> no prediction (grid/random/Optuna). The optimiser never proposes a setup outside your factor ranges, and the best setup always comes from <em>measured</em>, feasible runs. ${hist.length} recommendation(s) generated so far for this experiment (previews included).</p></div>`;
    const md = () => { $('#omd').textContent = META.optimisers.find((o) => o.id === $('#om').value).description; }; $('#om').addEventListener('change', md); md();
    const req = () => ({ count: +$('#oc').value, method: $('#om').value });
    $('#oprev')?.addEventListener('click', async () => { try { preview = (await api('POST', `/api/experiments/${id}/optimise/suggest`, req())).recommendations; $('#prev').innerHTML = previewTable(preview, names); $('#oerr').innerHTML = ''; } catch (e) { $('#oerr').innerHTML = errorsBox(e); } });
    $('#oapply')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${id}/optimise/apply`, req()); preview = null; toast('Optimiser runs queued', true); poll(draw, 800); draw(); } catch (e) { $('#oerr').innerHTML = errorsBox(e); } });
    return d;
  };
  const d = await draw();
  if (d.counts.running + d.counts.queued > 0) poll(async () => { const x = await draw(); if (x.counts.running + x.counts.queued === 0) clearInterval(pollTimer); }, 800);
}
function previewTable(recs, names) {
  return `<div class="panel"><h2>Recommended setups (preview)</h2><table><thead><tr>${names.map((n) => `<th class="num">${esc(label(n))}</th>`).join('')}<th>Kind</th><th class="num">Predicted lap</th><th class="num">Exp. improvement</th><th class="num">P(feasible)</th><th>Rationale</th></tr></thead><tbody>
  ${recs.map((r) => `<tr>${names.map((n) => `<td class="num">${fmt(r.configuration[n], 2)}</td>`).join('')}<td><span class="chip ${r.prediction_kind === 'model_prediction' ? 'model' : r.prediction_kind === 'heuristic_score' ? 'heur' : 'search'}">${esc(r.prediction_kind.replace('_', ' '))}</span></td>
  <td class="num">${r.predicted_value != null ? `${fmt(r.predicted_value, 3)} &plusmn; ${fmt(r.prediction_std, 3)}` : '&mdash;'}</td><td class="num">${r.expected_improvement != null ? fmt(r.expected_improvement, 4) : '&mdash;'}</td>
  <td class="num">${r.predicted_feasibility_probability != null ? fmt(r.predicted_feasibility_probability, 2) : '&mdash;'}</td><td class="dim" style="max-width:380px">${esc(r.rationale)}</td></tr>`).join('')}</tbody></table></div>`;
}

/* ------------------------------------------------------------------ report */
async function viewReport(id) {
  const d = await api('GET', `/api/experiments/${id}`);
  view().innerHTML = expHeader(d, 'report') + '<div id="rep"><div class="dim">Generating report&hellip;</div></div>';
  if (!d.counts.completed) { $('#rep').innerHTML = '<div class="note">No completed runs yet.</div>'; return; }
  try {
    const r = await api('GET', `/api/experiments/${id}/report`), rep = r.report, rp = rep.reproducibility;
    $('#rep').innerHTML = recoCard(r.recommendation) + `<div class="grid g2"><div class="panel"><h2>Export</h2><div class="row"><a class="btn primary" target="_blank" href="/api/experiments/${esc(id)}/report.html">Printable report (HTML)</a><a class="btn" href="/api/experiments/${esc(id)}/report.json">Report JSON</a></div>
      <div class="row" style="margin-top:10px"><a class="btn" href="/api/experiments/${esc(id)}/export/runs.csv">Runs CSV</a><a class="btn" href="/api/experiments/${esc(id)}/export/measurements.csv">Measurements CSV</a><a class="btn" href="/api/experiments/${esc(id)}/export/effects.csv">Effects CSV</a></div>
      <p class="hint">The HTML report is self-contained: print it to PDF from your browser.</p></div>
      <div class="panel"><h2>Reproducibility</h2><table><tbody><tr><td class="dim">Report version</td><td class="mono">${esc(rep.report_version)}</td></tr><tr><td class="dim">TRIALFORGE</td><td class="mono">${esc(rp.trialforge_version)}</td></tr><tr><td class="dim">Specification hash</td><td class="mono">${esc(rp.specification_hash.slice(0, 20))}&hellip;</td></tr>
      <tr><td class="dim">Seed / design</td><td class="mono">${esc(rp.random_seed)} / ${esc(rp.design_method)}</td></tr><tr><td class="dim">Analysis</td><td class="mono">${esc(rp.analysis_algorithm)} v${esc(rp.analysis_version)}</td></tr></tbody></table></div></div>
      <div class="grid g3"><div class="kpi"><b>${rep.completed_runs}/${rep.planned_runs}</b><span>runs completed</span></div><div class="kpi ${rep.failed_runs ? 'bad' : ''}"><b>${rep.failed_runs}</b><span>failed runs</span></div><div class="kpi"><b>${rep.constraint_violations.length}</b><span>constraint violations</span></div></div>
      <div class="panel"><h2>Limitations</h2><ul class="warnlist">${rep.limitations.map((l) => `<li>${esc(l)}</li>`).join('')}</ul></div>`;
  } catch (e) { $('#rep').innerHTML = errorsBox(e); }
}

/* ------------------------------------------------------------- historical F1 */
const HIST_TABS = [['overview', 'Overview'], ['laps', 'Lap analysis'], ['telemetry', 'Telemetry'], ['track', 'Track map'], ['compare', 'Compare'], ['weather', 'Weather & tyres']];
const OPENF1_TABS = [['race', 'Race & results'], ['strategy', 'Pit, flags & radio']];
const histTabs = (m) => (m.provider === 'openf1' ? HIST_TABS.concat(OPENF1_TABS) : HIST_TABS);
const PROVIDER_LABEL = { fastf1: 'FASTF1', openf1: 'OPENF1' };
const histChipFor = (m) => `<span class="chip hist">${esc(m.source_label || `HISTORICAL F1 · ${PROVIDER_LABEL[m.provider] || String(m.provider || '').toUpperCase()}`)}</span>`;
/* FastF1 brake data is an on/off flag stored as brake_applied (0/100); brake_pct is a fallback. */
const brakeOf = (pts) => (pts.some((p) => p.brake_applied != null) ? 'brake_applied' : 'brake_pct');

async function viewHist() {
  view().innerHTML = `<h1>Historical F1</h1><p class="sub">Real Formula 1 session data from <b>OpenF1</b> (free, no account, 2023 onwards) or <b>FastF1</b> (needs an F1TV subscription, 2018 onwards). Both are unofficial. For benchmarking and analysis &mdash; separate from your setup experiments.</p>
  <div class="grid g21"><div><div id="hpick"></div></div>
  <div><div class="panel"><h2>Imported sessions</h2><div id="hist-lib"><div class="dim">Loading&hellip;</div></div></div>
    <div class="panel"><h2>Recent imports</h2><div id="hist-jobs"><div class="dim">Loading&hellip;</div></div></div>
    <div class="panel"><h2>About this data</h2><p class="hint">OpenF1 (free, from 2023) and FastF1 (F1TV account, from 2018) are unofficial sources of Formula 1 timing and telemetry data. Neither is affiliated with Formula 1, and coverage varies by year and session. OpenF1 supplies no steering, no corner positions and no lap deletions. Car telemetry is sampled at the timing feed's rate; weather is sampled sparsely. Historical sessions contain no car setup configuration, so they are kept separate from the TRIALFORGE setup experiments.</p></div></div></div>`;
  const picker = HistPicker.mount($('#hpick'), { api, errorsBox, toast, navigate: false, onChange: () => { loadLibrary(); loadJobs(); } });
  const loadJobs = async () => {
    try {
      const jobs = await api('GET', '/api/historical/imports?limit=10');
      $('#hist-jobs').innerHTML = jobs.length ? `<table><thead><tr><th>Import</th><th>State</th><th>Detail</th><th></th></tr></thead><tbody>${jobs.map((j) => `<tr>
        <td class="mono">${esc(j.request.year)} ${esc(j.request.event)} ${esc(j.request.session)}<div class="dim">${esc(PROVIDER_LABEL[j.request.provider || 'fastf1'] || '')} &middot; ${esc((j.created_at || '').slice(0, 16).replace('T', ' '))}</div></td>
        <td>${esc(j.state)}${j.error_code ? `<div class="badc mono">${esc(j.error_code)}</div>` : ''}</td><td class="dim">${esc(j.error || j.detail || '')}</td>
        <td><button class="btn small hjob" data-id="${esc(j.import_id)}" type="button">Show</button></td></tr>`).join('')}</tbody></table>` : '<div class="empty">No imports yet.</div>';
      document.querySelectorAll('.hjob').forEach((b) => b.addEventListener('click', () => picker.track(b.dataset.id)));
      const running = jobs.find((j) => !['COMPLETED', 'FAILED', 'CANCELLED'].includes(j.state));
      if (running && !$('#hprog').innerHTML) picker.track(running.import_id);
    } catch (e) { $('#hist-jobs').innerHTML = errorsBox(e); }
  };
  const loadLibrary = async () => {
    try {
      const list = await api('GET', '/api/historical/sessions');
      $('#hist-lib').innerHTML = list.length ? `<table><thead><tr><th>Session</th><th>Season</th><th>Circuit</th><th class="num">Drivers</th><th>Imported</th><th></th></tr></thead><tbody>
        ${list.map((s) => `<tr><td><b>${esc(s.event)}</b><div class="dim mono">${esc(s.session)} &middot; ${esc(PROVIDER_LABEL[s.provider] || s.provider)} &middot; ${esc(s.session_id)}</div></td><td class="num">${esc(s.year)}</td><td>${esc(s.circuit || s.location || '&mdash;')}</td>
        <td class="num">${(s.drivers || []).length}</td><td class="dim mono">${esc((s.imported_at || '').slice(0, 10))}</td><td><a class="btn small" href="#/hist/${esc(s.session_id)}">Open</a></td></tr>`).join('')}</tbody></table>`
        : '<div class="empty">No historical sessions imported yet.</div>';
    } catch (e) { $('#hist-lib').innerHTML = errorsBox(e); }
  };
  await loadLibrary();
  await loadJobs();
}

async function viewHistSession(id, tab) {
  const m = await api('GET', `/api/historical/sessions/${id}`);
  view().innerHTML = `<div class="row"><div><h1>${esc(m.event)} ${m.round ? `<span class="dim">&middot; Round ${esc(m.round)}</span>` : ''}</h1>
    <p class="sub">${esc(m.circuit || '')} &middot; ${esc(m.session)} (${esc(m.session_type)}) &middot; ${esc(m.session_date || '')} &middot; ${histChipFor(m)}</p></div><div class="spacer"></div><a class="btn" href="#/hist">All sessions</a></div>
    <div class="tabs">${histTabs(m).map(([k, n]) => `<a href="#/hist/${esc(id)}/${k}" class="${k === tab ? 'on' : ''}">${n}</a>`).join('')}</div><div id="hist-body"><div class="dim">Loading&hellip;</div></div>`;
  const body = $('#hist-body');
  const drivers = m.drivers || [];
  const drvOpts = (sel = drivers[0]?.code) => drivers.map((d) => `<option value="${esc(d.code)}" ${d.code === sel ? 'selected' : ''}>${esc(d.code)} &middot; ${esc(d.name)}</option>`).join('');
  const lapsFor = async (code) => (await api('GET', `/api/historical/sessions/${id}/laps?driver=${encodeURIComponent(code)}`));

  if (tab === 'overview') {
    const laps = await api('GET', `/api/historical/sessions/${id}/laps`);
    const best = {};
    laps.forEach((l) => { if (l.lap_time_s != null && l.is_valid !== false && (!best[l.driver] || l.lap_time_s < best[l.driver].lap_time_s)) best[l.driver] = l; });
    const ch = m.channels || {};
    const validity = laps.some((l) => l.is_valid != null || l.is_deleted != null);
    body.innerHTML = `<div class="grid g21"><div>
      <div class="panel"><h2>Session</h2><table><tbody>
        <tr><td class="dim">Event</td><td>${esc(m.event)}${m.event_official && m.event_official !== m.event ? ` <span class="dim">(${esc(m.event_official)})</span>` : ''}</td></tr>
        <tr><td class="dim">Circuit</td><td>${esc(m.circuit || m.location || '&mdash;')}</td></tr>
        <tr><td class="dim">Session</td><td>${esc(m.session)} <span class="mono dim">(${esc(m.session_type)})</span></td></tr>
        <tr><td class="dim">Date</td><td>${esc(m.session_date || '&mdash;')}</td></tr>
        <tr><td class="dim">Imported</td><td class="mono dim">${esc(m.imported_at || '')}</td></tr>
        <tr><td class="dim">Source</td><td class="mono">${esc(m.provider)} &middot; ${esc(m.session_id)}</td></tr></tbody></table></div>
      <div class="panel"><h2>Drivers</h2><table><thead><tr><th>Code</th><th>Name</th><th>Team</th><th class="num">Best lap</th></tr></thead><tbody>
        ${drivers.map((d) => `<tr><td class="mono"><b>${esc(d.code)}</b></td><td>${esc(d.name)}</td><td class="dim">${esc(d.team || '&mdash;')}</td><td class="num">${best[d.code] ? fmt(best[d.code].lap_time_s, 3) + ' s' : '&mdash;'}</td></tr>`).join('') || '<tr><td colspan="4" class="dim">none</td></tr>'}</tbody></table></div></div>
      <div><div class="panel"><h2>Data available</h2><table><tbody>
        <tr><td class="dim">Lap timing</td><td>${laps.length ? `<span class="good">${laps.length} laps</span>` : '<span class="badc">none</span>'}</td></tr>
        <tr><td class="dim">Weather</td><td>${ch.weather ? '<span class="good">yes</span>' : '<span class="dim">no</span>'}</td></tr>
        <tr><td class="dim">Telemetry</td><td>${drivers.map((d) => `${esc(d.code)}: ${ch.telemetry?.[d.code] ? '<span class="good">yes</span>' : '<span class="dim">no</span>'}`).join(' &middot; ') || '&mdash;'}</td></tr>
        <tr><td class="dim">Track location (x/y)</td><td>${drivers.map((d) => `${esc(d.code)}: ${ch.position?.[d.code] ? '<span class="good">yes</span>' : '<span class="dim">no</span>'}`).join(' &middot; ') || '&mdash;'}</td></tr>
        <tr><td class="dim">Lap validity</td><td>${validity ? '<span class="good">provided</span>' : '<span class="dim">not provided by this source</span>'}</td></tr>
        ${m.provider === 'openf1' ? `<tr><td class="dim">Running order</td><td>${m.datasets?.race_position?.rows ? `<span class="good">${esc(m.datasets.race_position.rows)} changes</span> <a href="#/hist/${esc(id)}/race">open</a>` : '<span class="dim">no</span>'}</td></tr>` : ''}</tbody></table>
        <p class="hint">${esc(m.attribution || '')}</p></div>${HistRace.coverage(m)}</div></div>`;
  } else if (tab === 'laps') {
    body.innerHTML = `<div class="panel"><div class="row"><div class="field" style="max-width:280px"><label for="hlapdrv">Driver</label><select id="hlapdrv">${drvOpts()}</select></div></div><div id="hlapout"><div class="dim">Loading&hellip;</div></div></div>`;
    const show = async () => {
      const code = $('#hlapdrv').value, laps = await lapsFor(code);
      const rows = laps.map((l) => ({ lap: l.lap_number, sector_1: l.sector_1_s, sector_2: l.sector_2_s, sector_3: l.sector_3_s, lap_time: l.lap_time_s, outlier: l.is_valid === false }));
      $('#hlapout').innerHTML = `<div class="kpis"><div class="kpi"><b>${laps.length}</b><span>laps</span></div><div class="kpi good"><b>${laps.filter((l) => l.lap_time_s != null).length ? fmt(Math.min(...laps.map((l) => l.lap_time_s).filter((v) => v != null)), 3) : '&mdash;'}</b><span>best lap (s)</span></div>
        <div class="kpi"><b>${laps.some((l) => l.is_valid != null || l.is_deleted != null) ? laps.filter((l) => l.is_valid === false).length : 'n/a'}</b><span>${laps.some((l) => l.is_valid != null || l.is_deleted != null) ? 'invalid/deleted' : 'invalid/deleted (not provided)'}</span></div><div class="kpi"><b>${[...new Set(laps.map((l) => l.compound).filter(Boolean))].join(', ') || '&mdash;'}</b><span>compounds</span></div></div>
        <div class="grid g2"><div class="panel"><h2>Lap time</h2>${Charts.lines({ series: [{ name: `${code} lap time`, color: '#e10600', pts: laps.filter((l) => l.lap_time_s != null).map((l) => ({ x: l.lap_number, y: l.lap_time_s, flag: l.is_valid === false })) }], xLabel: 'lap', yLabel: 'seconds' })}</div>
        <div class="panel"><h2>Sector times</h2>${Charts.sectors({ laps: rows })}</div></div>
        <div class="panel"><h2>Laps</h2><div style="overflow:auto;max-height:420px"><table><thead><tr><th>Lap</th><th>Stint</th><th>Compound</th><th class="num">Tyre life</th><th class="num">S1</th><th class="num">S2</th><th class="num">S3</th><th class="num">Lap</th>${laps.some((l) => l.st_speed_kph != null) ? '<th class="num">Trap km/h</th>' : ''}<th>Flags</th></tr></thead><tbody>
        ${laps.map((l) => `<tr class="${l.is_valid === false ? 'outlier' : ''}"><td class="mono">${esc(l.lap_number)}</td><td class="mono">${esc(l.stint ?? '&mdash;')}</td><td>${esc(l.compound || '&mdash;')}</td><td class="num">${esc(l.tyre_life ?? '&mdash;')}</td>
        <td class="num">${fmt(l.sector_1_s, 3)}</td><td class="num">${fmt(l.sector_2_s, 3)}</td><td class="num">${fmt(l.sector_3_s, 3)}</td><td class="num"><b>${fmt(l.lap_time_s, 3)}</b></td>
        ${laps.some((x) => x.st_speed_kph != null) ? `<td class="num">${fmt(l.st_speed_kph, 0)}</td>` : ''}<td>${l.is_valid === false ? '<span class="tag">invalid</span>' : ''}${l.is_deleted ? '<span class="tag">deleted</span>' : ''}${l.is_accurate === false ? '<span class="tag dim">timing approx.</span>' : ''}${l.is_personal_best ? '<span class="chip completed">PB</span>' : ''}${l.pits ? '<span class="tag">pit</span>' : ''}</td></tr>`).join('')}</tbody></table></div></div>`;
    };
    $('#hlapdrv').addEventListener('change', show); show();
  } else if (tab === 'telemetry' || tab === 'track') {
    body.innerHTML = `<div class="panel"><div class="row"><div class="field" style="max-width:220px"><label for="htrkdrv">Driver</label><select id="htrkdrv">${drvOpts()}</select></div>
      <div class="field" style="max-width:160px"><label for="htrklap">Lap</label><select id="htrklap"><option>all</option></select></div></div><div id="htrkout"><div class="dim">Loading&hellip;</div></div></div>`;
    const fillLaps = async () => {
      const code = $('#htrkdrv').value, laps = await lapsFor(code);
      const nums = [...new Set(laps.map((l) => l.lap_number).filter((n) => n != null))].sort((a, b) => a - b);
      $('#htrklap').innerHTML = '<option value="">all</option>' + nums.map((n) => `<option value="${n}">${n}</option>`).join('');
    };
    const show = async () => {
      const code = $('#htrkdrv').value, lap = $('#htrklap').value ? +$('#htrklap').value : null;
      if (tab === 'telemetry') {
        const r = await api('GET', `/api/historical/sessions/${id}/telemetry?driver=${encodeURIComponent(code)}${lap ? `&lap=${lap}` : ''}&limit=1500`);
        $('#htrkout').innerHTML = r.samples.length ? histTraceCharts(r.samples) + `<p class="hint">${r.count} samples${lap ? ` on lap ${lap}` : ''} &middot; distance on the x-axis${m.provider === 'openf1' ? ' (an estimate: speed integrated within the lap)' : ''} &middot; throttle is a percentage; brake is an on/off flag (the timing feed publishes no brake pressure); DRS is a raw state code.</p>` : '<div class="empty">No telemetry stored for this driver/lap.</div>';
      } else {
        const r = await api('GET', `/api/historical/sessions/${id}/track-map?driver=${encodeURIComponent(code)}${lap ? `&lap=${lap}` : ''}`);
        $('#htrkout').innerHTML = `<div class="panel">${Charts.trackMap({ points: r.points || [], corners: r.corners || [] })}</div><p class="hint">Actual X/Y location from the timing feed${lap ? `, lap ${lap}` : ', fastest available lap stream'}.${m.provider === 'openf1' ? ' OpenF1 coordinates are approximate, in OpenF1\'s own units with an arbitrary origin (not verified as metres); no corner markers are available. Running order (P1, P2, ...) is a different dataset: see the Race tab.' : ''}</p>`;
      }
    };
    $('#htrkdrv').addEventListener('change', async () => { await fillLaps(); show(); });
    $('#htrklap').addEventListener('change', show);
    await fillLaps(); show();
  } else if (tab === 'compare') {
    const [a, b] = drivers.length >= 2 ? [drivers[0].code, drivers[1].code] : [drivers[0]?.code, drivers[0]?.code];
    body.innerHTML = `<div class="panel"><div class="grid g3">
      <div class="field"><label>Driver A</label><select id="hcmpa">${drvOpts(a)}</select></div>
      <div class="field"><label>Lap A</label><input id="hcmpla" type="number" min="1" value="1"></div>
      <div class="field"><label>Driver B</label><select id="hcmpb">${drvOpts(b)}</select></div>
      <div class="field"><label>Lap B</label><input id="hcmplb" type="number" min="1" value="1"></div></div>
      <button class="btn primary" id="hcmpgo">Compare</button><span class="dim" id="hcmpmsg"></span><div id="hcmpout"></div></div>`;
    $('#hcmpgo').addEventListener('click', async () => {
      $('#hcmpout').innerHTML = '<div class="dim">Comparing&hellip;</div>';
      try {
        const r = await api('POST', `/api/historical/sessions/${id}/compare`, { driver_a: $('#hcmpa').value, lap_a: +$('#hcmpla').value, driver_b: $('#hcmpb').value, lap_b: +$('#hcmplb').value });
        const pctA = (r.a.points.length ? Math.max(...r.a.points.map((p) => p.distance_m || 0)) : 1) || 1;
        const pctB = (r.b.points.length ? Math.max(...r.b.points.map((p) => p.distance_m || 0)) : 1) || 1;
        const ser = (pts, key, color, name, norm) => ({ name, color, pts: pts.map((p) => ({ x: (p.distance_m || 0) / norm * 100, y: p[key] })).filter((p) => p.y !== null && p.y !== undefined) });
        $('#hcmpout').innerHTML = `<div class="note info">${esc(r.delta.note)}</div>
          <div class="panel"><h2>Speed vs distance</h2>${Charts.lines({ series: [ser(r.a.points, 'speed_kph', '#e10600', `${r.a.driver} lap ${r.a.lap}`, pctA), ser(r.b.points, 'speed_kph', '#4aa8ff', `${r.b.driver} lap ${r.b.lap}`, pctB)], xLabel: 'lap distance (%)', yLabel: 'km/h' })}</div>
          <div class="grid g2"><div class="panel"><h2>Throttle</h2>${Charts.lines({ series: [ser(r.a.points, 'throttle_pct', '#e10600', 'A', pctA), ser(r.b.points, 'throttle_pct', '#4aa8ff', 'B', pctB)], xLabel: 'lap distance (%)', yLabel: '%' })}</div>
          <div class="panel"><h2>Brake (on/off)</h2>${Charts.lines({ series: [ser(r.a.points, brakeOf(r.a.points), '#e10600', 'A', pctA), ser(r.b.points, brakeOf(r.b.points), '#4aa8ff', 'B', pctB)], xLabel: 'lap distance (%)', yLabel: 'applied (0/100)' })}</div></div>
          <div class="panel"><h2>Time delta (B &minus; A)</h2>${Charts.lines({ series: [{ name: 't_B - t_A (s)', color: '#ffb400', pts: r.delta.distance_pct.map((x, i) => ({ x: x * 100, y: r.delta.delta_s[i] })) }], xLabel: 'lap distance (%)', yLabel: 'seconds', ymin: Math.min(...r.delta.delta_s) - 0.05, ymax: Math.max(...r.delta.delta_s) + 0.05 })}
          <p class="hint">Positive delta = driver B is slower than A at that point of the lap. Delta is computed on an interpolated common distance grid, not raw samples.</p></div>`;
      } catch (e) { $('#hcmpmsg').innerHTML = `<span class="badc">${esc(e.message)}</span>`; }
    });
  } else if (tab === 'race' && m.provider === 'openf1') {
    await HistRace.race(body, api, id, m);
  } else if (tab === 'strategy' && m.provider === 'openf1') {
    await HistRace.strategy(body, api, id, m, await api('GET', `/api/historical/sessions/${id}/laps`));
  } else if (tab === 'weather') {
    const [wx, laps] = await Promise.all([api('GET', `/api/historical/sessions/${id}/weather`), api('GET', `/api/historical/sessions/${id}/laps`)]);
    const s = wx.samples || [];
    const wchart = (title, key, unit) => {
      const pts = downsample(s.map((p) => ({ x: p.time_s ?? 0, y: p[key] })).filter((p) => p.y !== null && p.y !== undefined));
      return s.length ? `<div class="panel"><h2>${title}</h2>${Charts.lines({ series: [{ name: title, color: '#4aa8ff', pts }], xLabel: 'session time (s)', yLabel: unit })}</div>` : `<div class="panel"><h2>${title}</h2><div class="empty">No data.</div></div>`;
    };
    const stints = {};
    laps.forEach((l) => { const k = `${l.driver}|${l.stint}`; if (l.compound || l.tyre_life != null) stints[k] = { driver: l.driver, stint: l.stint, compound: l.compound, tyre_life: l.tyre_life, laps: (stints[k]?.laps || 0) + 1 }; });
    body.innerHTML = `<div class="note info">${esc(wx.note || 'Weather is sampled sparsely by the timing feed.')}</div>
      <div class="grid g2">${wchart('Air temperature', 'air_temp_c', '°C')}${wchart('Track temperature', 'track_temp_c', '°C')}${wchart('Humidity', 'humidity_pct', '%')}${wchart('Wind speed', 'wind_speed_ms', 'm/s')}${s.some((p) => p.rainfall_flag != null) ? wchart('Rainfall (0 = dry, 1 = rain)', 'rainfall_flag', 'flag') : wchart('Rainfall', 'rainfall_mm', 'mm')}${wchart('Pressure', 'pressure_hpa', 'hPa')}</div>
      <div class="panel"><h2>Tyre strategy</h2><table><thead><tr><th>Driver</th><th>Stint</th><th>Compound</th><th class="num">Tyre life</th><th class="num">Laps</th></tr></thead><tbody>
      ${Object.values(stints).map((t) => `<tr><td class="mono"><b>${esc(t.driver)}</b></td><td class="mono">${esc(t.stint ?? '&mdash;')}</td><td>${esc(t.compound || '&mdash;')}</td><td class="num">${esc(t.tyre_life ?? '&mdash;')}</td><td class="num">${t.laps}</td></tr>`).join('') || '<tr><td colspan="5" class="dim">No stint data.</td></tr>'}</tbody></table></div>`;
  }
}

/* Speed/throttle/brake/RPM/gear/DRS traces vs distance for a historical lap.
   FastF1's Brake channel is a boolean 'brakes applied' flag, stored as brake_applied (0/100);
   brake_pct only exists if a source ever provides a real percentage. */
function histTraceCharts(samples) {
  const s = (key, name, color, scale = 1) => ({ name, color, pts: downsample(samples.map((p) => ({ x: p.distance_m, y: p[key] == null ? null : p[key] * scale })).filter((p) => p.y !== null && !Number.isNaN(p.y))) });
  const brakeKey = samples.some((p) => p.brake_applied != null) ? 'brake_applied' : 'brake_pct';
  const brakeName = brakeKey === 'brake_applied' ? 'brake applied' : 'brake';
  return `<div class="grid g2">
    <div class="panel"><h2>Speed</h2>${Charts.lines({ series: [s('speed_kph', 'speed', '#e10600')], xLabel: 'distance (m)', yLabel: 'km/h' })}</div>
    <div class="panel"><h2>Throttle &amp; brake</h2>${Charts.lines({ series: [s('throttle_pct', 'throttle', '#2ecc71'), s(brakeKey, brakeName, '#e10600')], xLabel: 'distance (m)', yLabel: '%' })}</div>
    <div class="panel"><h2>RPM</h2>${Charts.lines({ series: [s('rpm', 'rpm', '#ffb400')], xLabel: 'distance (m)', yLabel: 'rpm' })}</div>
    <div class="panel"><h2>Gear</h2>${Charts.lines({ series: [s('gear', 'gear', '#c99bff')], xLabel: 'distance (m)', yLabel: 'gear' })}</div>
    <div class="panel"><h2>DRS state</h2>${Charts.lines({ series: [s('drs', 'drs state code', '#00cec9')], stepped: true, noDots: true, xLabel: 'distance (m)', yLabel: 'raw DRS code', ymin: -0.5, ymax: 14.5 })}
      <p class="hint">Raw state codes, not a 0/1 flag: 0 and 1 = off, 8 = detected (eligible in the activation zone), 10, 12 and 14 = on. Codes 2, 3 and 9 are undocumented.</p></div></div>`;
}

/* -------------------------------------------------------------------- boot */
(async () => {
  try { const h = await api('GET', '/api/health'); setLed('ok', `online · SDK ${h.trialforge}`); } catch (_) { setLed('bad', 'backend offline'); }
  route();
})();
