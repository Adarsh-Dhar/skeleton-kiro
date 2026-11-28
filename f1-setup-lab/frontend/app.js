/* F1 SETUP LAB single-page app (no build step): setup engineering and historical Formula 1 (OpenF1). All dynamic text is escaped with esc().
   Shared helpers and the router live here; the setup-engineering views are in setup_lab.js (loaded first). */
const esc = Charts.esc, fmt = Charts.f;
const $ = (s, r = document) => r.querySelector(s);
const view = () => $('#view-body');  // views render into this nested container; #view-error holds failures so earlier data stays
let META = null;
class ApiError extends Error { constructor(status, issues) { super(issues.map((i) => i.message).join('; ')); this.status = status; this.issues = issues; } }
async function api(method, path, body) {
  let res;
  try { res = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined }); }
  catch (_) { throw new ApiError(0, [{ field: 'request', message: 'request failed' }]); }
  const ct = res.headers.get('content-type') || '';
  let data;
  try { data = ct.includes('json') ? await res.json() : await res.text(); } catch (_) { data = null; }
  if (!res.ok) {
    const d = data && typeof data === 'object' ? data.detail : null;
    throw new ApiError(res.status, Array.isArray(d) ? d : [{ field: 'request', message: String(d || 'request failed') }]);
  }
  if (data === null) throw new ApiError(res.status, [{ field: 'request', message: 'request failed' }]);
  return data;
}
function toast(msg, ok = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (ok ? ' ok' : ''); t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 4500);
}
const errorsBox = (e) => e instanceof ApiError
  ? `<div class="errors" role="alert"><b>Request rejected</b><ul>${e.issues.map((i) => `<li><code>${esc(i.field)}</code> ${esc(i.message)}</li>`).join('')}</ul></div>`
  : `<div class="errors" role="alert">${esc(e.message || e)}</div>`;
function setNav(key) { document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.nav === key)); }
function setLed(state, text) { const a = $('#api-status'); a.innerHTML = `<i class="led ${state}"></i><span>${esc(text)}</span>`; }
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

/* ------------------------------------------------------------------ router */
/* An open tab keeps the JavaScript it loaded: switching pages only changes the #hash, which never reloads anything. After an
   update the server's frontend fingerprint changes, and the next page change reloads the tab so it runs the current code. */
let FRONTEND_BUILD = null;
async function reloadIfUpdated() {
  try {
    const { build } = await (await fetch('/api/frontend-build', { cache: 'no-store' })).json();
    if (FRONTEND_BUILD === null) FRONTEND_BUILD = build;
    else if (build && build !== FRONTEND_BUILD) { location.reload(); return true; }
  } catch (_) { /* offline backend: keep the current code */ }
  return false;
}
/* Banner: the synthetic notice on Setup Engineering views, the historical notice on Historical F1 views. */
function showNotice(mode) { if (typeof setBanner === 'function') setBanner(mode); }
const EXP_TABS = { design: 'viewDesign', runs: 'viewRuns', telemetry: 'viewExpTelemetry', analysis: 'viewAnalysis', optimise: 'viewOptimise', report: 'viewReport' };
async function route() {
  if (await reloadIfUpdated()) return;
  clearInterval(pollTimer);
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const err = $('#view-error'); err.innerHTML = '';
  try {
    if (!META) META = await api('GET', '/api/meta');
    if (!parts.length) { location.replace('#/dashboard'); return; }
    const head = parts[0];
    if (head === 'dashboard') { setNav('dashboard'); showNotice('synthetic'); return await viewDashboard(); }
    if (head === 'experiments') { setNav('experiments'); showNotice('synthetic'); return await viewExperiments(); }
    if (head === 'new') { setNav('experiments'); showNotice('synthetic'); return await viewNew(); }
    if (head === 'telemetry') { setNav('telemetry'); showNotice('synthetic'); return await viewTelemetryStandalone(); }
    if (head === 'e' && parts[1]) {
      const fn = { viewDesign, viewRuns, viewExpTelemetry, viewAnalysis, viewOptimise, viewReport }[EXP_TABS[parts[2] || 'design']];
      if (fn) { setNav('experiments'); showNotice('synthetic'); return await fn(parts[1]); }
    }
    if (head === 'hist') { setNav(parts[1] ? 'sessions' : 'hist'); showNotice('historical'); return parts[1] ? await viewHistSession(parts[1], parts[2] || 'overview') : await viewHist(); }
    if (head === 'setup-link' && parts[1]) {  // opened by the "Use telemetry in Setup Experiment" action; the form is viewSetupLink in setup_lab.js
      setNav('sessions'); showNotice('historical');
      if (typeof viewSetupLink === 'function') return await viewSetupLink(decodeURIComponent(parts[1]));
      view().innerHTML = `<div class="empty"><h2>Setup Experiment form unavailable</h2><p>This build has no form for sending telemetry to an experiment.</p><a href="#/hist/${esc(parts[1])}">Back to the session</a></div>`;
      return;
    }
    if (head === 'sessions') { setNav('sessions'); showNotice('historical'); return await viewSessions(); }
    if (head === 'standings') { setNav('standings'); showNotice('historical'); return await HistAnalysis.viewStandings(parts[1] ? decodeURIComponent(parts[1]) : undefined); }
    if (head === 'season') { setNav('season'); showNotice('historical'); return await HistAnalysis.viewSeason(); }
    showNotice('historical');
    view().innerHTML = '<div class="empty"><h2>Page not found</h2><p>This page does not exist in F1 SETUP LAB.</p><a href="#/dashboard">Dashboard</a> &middot; <a href="#/hist">Import a session</a> &middot; <a href="#/sessions">Imported sessions</a></div>';
  } catch (e) { err.innerHTML = errorsBox(e) + '<p><a href="#/dashboard">Back to dashboard</a></p>'; }
}
window.addEventListener('hashchange', route);

/* ------------------------------------------------------------- historical F1 */
const HIST_TABS = [['overview', 'Overview'], ['laps', 'Lap analysis'], ['telemetry', 'Telemetry'], ['track', 'Track map'], ['compare', 'Compare'], ['weather', 'Weather & tyres'], ['minisectors', 'Mini-sectors'], ['tyres', 'Tyre degradation']];
const OPENF1_TABS = [['race', 'Race & results'], ['strategy', 'Pit, flags & radio']];
const histTabs = () => HIST_TABS.concat(OPENF1_TABS);
/* Team colour of a driver code from the stored session drivers (HistAnalysis.teamColour); fb (the existing palette colour) when none is stored. */
const histColour = (drivers, code, fb) => {
  if (typeof HistAnalysis === 'undefined') return fb;
  const c = HistAnalysis.teamColour(code, drivers || []);
  return HistAnalysis.PALETTE.includes(c) || c === '#64748b' ? fb : c;
};
const histSwatch = (drivers, code, fb) => `<span style="color:${histColour(drivers, code, fb)}" aria-hidden="true">&#9632;</span> `;
const histChipFor = (m) => `<span class="chip hist">${esc(m.source_label || 'HISTORICAL F1 · OPENF1')}</span>`;
/* OpenF1 brake data is an on/off flag stored as brake_applied (0/100); brake_pct is a fallback. */
const brakeOf = (pts) => (pts.some((p) => p.brake_applied != null) ? 'brake_applied' : 'brake_pct');
const sessionsTable = (list) => HistAnalysis.sessionsTable(list);

/* Import page (landing page): choose a session, import it, and the analysis opens when the import finishes. */
async function viewHist() {
  view().innerHTML = `<h1>Historical F1</h1><p class="sub">Real Formula 1 session data from <b>OpenF1</b> (free, no account, 2023 onwards; unofficial, not affiliated with Formula 1). Choose a season, Grand Prix, session and drivers; the session opens for analysis when the import finishes.</p>
  <div class="grid g21"><div><div id="hpick"></div></div>
  <div><div class="panel"><h2>Recent imports</h2><div id="hist-jobs"><div class="dim">Loading&hellip;</div></div></div>
    <div class="panel"><h2>Imported sessions</h2><div id="hist-lib"><div class="dim">Loading&hellip;</div></div></div>
    <div class="panel"><h2>About this data</h2><p class="hint">OpenF1 is an unofficial source of Formula 1 timing and telemetry data, and coverage varies by year and session. It supplies no steering, no corner positions and no lap deletions. Car telemetry is sampled at about 3.7 Hz; weather is sampled sparsely. Each session's Import coverage panel lists what was provided, what is estimated and what is missing.</p></div></div></div>`;
  const picker = HistPicker.mount($('#hpick'), { api, errorsBox, toast, navigate: true, onChange: () => { loadLibrary(); loadJobs(); } });
  const loadJobs = async () => {
    try {
      const jobs = await api('GET', '/api/historical/imports?limit=10');
      $('#hist-jobs').innerHTML = jobs.length ? `<table><thead><tr><th>Import</th><th>State</th><th>Detail</th><th></th></tr></thead><tbody>${jobs.map((j) => `<tr>
        <td class="mono">${esc(j.request.year)} ${esc(j.request.event)} ${esc(j.request.session)}<div class="dim">${esc((j.created_at || '').slice(0, 16).replace('T', ' '))}</div></td>
        <td>${esc(j.state)}${j.error_code ? `<div class="badc mono">${esc(j.error_code)}</div>` : ''}</td><td class="dim">${esc(j.error || j.detail || '')}</td>
        <td><button class="btn small hjob" data-id="${esc(j.import_id)}" type="button">Show</button></td></tr>`).join('')}</tbody></table>` : '<div class="empty">No imports yet.</div>';
      document.querySelectorAll('.hjob').forEach((b) => b.addEventListener('click', () => picker.track(b.dataset.id)));
      const running = jobs.find((j) => !['COMPLETED', 'FAILED', 'CANCELLED'].includes(j.state));
      if (running && !$('#hprog').innerHTML) picker.track(running.import_id);
    } catch (e) { $('#hist-jobs').innerHTML = errorsBox(e); }
  };
  const loadLibrary = async () => {
    await HistAnalysis.loadSessionList($('#hist-lib'), { api });
  };
  await loadLibrary();
  await loadJobs();
}

/* Library of imported sessions. */
async function viewSessions() {
  view().innerHTML = `<div class="row"><div><h1>Imported sessions</h1><p class="sub">Sessions imported from OpenF1 or a CSV file, stored in the local database. Each row shows its provider, data type and where the data came from.</p></div><div class="spacer"></div><a class="btn primary" href="#/hist">Import a session</a></div>
    <div class="panel" id="hist-lib"><div class="dim">Loading&hellip;</div></div>`;
  await HistAnalysis.loadSessionList($('#hist-lib'), { api });
}

async function viewHistSession(id, tab) {
  const m = await api('GET', `/api/historical/sessions/${id}`);
  view().innerHTML = `<div class="row"><div><h1>${esc(m.event)} ${m.round ? `<span class="dim">&middot; Round ${esc(m.round)}</span>` : ''}</h1>
    <p class="sub">${esc(m.circuit || '')} &middot; ${esc(m.session)} (${esc(m.session_type)}) &middot; ${esc(m.session_date || '')} &middot; ${m.provider === 'openf1' || !m.provider ? histChipFor(m) + ' ' : ''}${HistAnalysis.sessionFacts(m)}</p></div><div class="spacer"></div>${HistAnalysis.setupAction(id)} <a class="btn" href="#/sessions">All sessions</a></div>
    <div id="hist-export"></div>
    <div class="tabs">${histTabs().map(([k, n]) => `<a href="#/hist/${esc(id)}/${k}" class="${k === tab ? 'on' : ''}">${n}</a>`).join('')}</div><div id="hist-body"><div class="dim">Loading&hellip;</div></div>`;
  const body = $('#hist-body');
  if (typeof HistAnalysis !== 'undefined') HistAnalysis.exportButtons(id, { drivers: m.drivers || [], container: $('#hist-export') });
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
        ${drivers.map((d) => `<tr><td class="mono">${histSwatch(drivers, d.code, '#94a3b8')}<b>${esc(d.code)}</b></td><td>${esc(d.name)}</td><td class="dim">${esc(d.team || '&mdash;')}</td><td class="num">${best[d.code] ? fmt(best[d.code].lap_time_s, 3) + ' s' : '&mdash;'}</td></tr>`).join('') || '<tr><td colspan="4" class="dim">none</td></tr>'}</tbody></table></div></div>
      <div><div class="panel"><h2>Data available</h2><table><tbody>
        <tr><td class="dim">Lap timing</td><td>${laps.length ? `<span class="good">${laps.length} laps</span>` : '<span class="badc">none</span>'}</td></tr>
        <tr><td class="dim">Weather</td><td>${ch.weather ? '<span class="good">yes</span>' : '<span class="dim">no</span>'}</td></tr>
        <tr><td class="dim">Telemetry</td><td>${drivers.map((d) => `${esc(d.code)}: ${ch.telemetry?.[d.code] ? '<span class="good">yes</span>' : '<span class="dim">no</span>'}`).join(' &middot; ') || '&mdash;'}</td></tr>
        <tr><td class="dim">Track location (x/y)</td><td>${drivers.map((d) => `${esc(d.code)}: ${ch.position?.[d.code] ? '<span class="good">yes</span>' : '<span class="dim">no</span>'}`).join(' &middot; ') || '&mdash;'}</td></tr>
        <tr><td class="dim">Lap validity</td><td>${validity ? '<span class="good">provided</span>' : '<span class="dim">not provided by this source</span>'}</td></tr>
        ${`<tr><td class="dim">Running order</td><td>${m.datasets?.race_position?.rows ? `<span class="good">${esc(m.datasets.race_position.rows)} changes</span> <a href="#/hist/${esc(id)}/race">open</a>` : '<span class="dim">no</span>'}</td></tr>`}</tbody></table>
        <p class="hint">${esc(m.attribution || '')}</p></div>${HistRace.coverage(m)}</div></div>`;
  } else if (tab === 'laps') {
    body.innerHTML = `<div class="panel"><div class="row"><div class="field" style="max-width:280px"><label for="hlapdrv">Driver</label><select id="hlapdrv">${drvOpts()}</select></div></div><div id="hlapout"><div class="dim">Loading&hellip;</div></div></div>`;
    const show = async () => {
      const code = $('#hlapdrv').value, laps = await lapsFor(code);
      const rows = laps.map((l) => ({ lap: l.lap_number, sector_1: l.sector_1_s, sector_2: l.sector_2_s, sector_3: l.sector_3_s, lap_time: l.lap_time_s, outlier: l.is_valid === false }));
      $('#hlapout').innerHTML = `<div class="kpis"><div class="kpi"><b>${laps.length}</b><span>laps</span></div><div class="kpi good"><b>${laps.filter((l) => l.lap_time_s != null).length ? fmt(Math.min(...laps.map((l) => l.lap_time_s).filter((v) => v != null)), 3) : '&mdash;'}</b><span>best lap (s)</span></div>
        <div class="kpi"><b>${laps.some((l) => l.is_valid != null || l.is_deleted != null) ? laps.filter((l) => l.is_valid === false).length : 'n/a'}</b><span>${laps.some((l) => l.is_valid != null || l.is_deleted != null) ? 'invalid/deleted' : 'invalid/deleted (not provided)'}</span></div><div class="kpi"><b>${[...new Set(laps.map((l) => l.compound).filter(Boolean))].join(', ') || '&mdash;'}</b><span>compounds</span></div></div>
        <div class="grid g2"><div class="panel"><h2>Lap time</h2>${Charts.lines({ series: [{ name: `${code} lap time`, color: histColour(drivers, code, '#e10600'), pts: laps.filter((l) => l.lap_time_s != null).map((l) => ({ x: l.lap_number, y: l.lap_time_s, flag: l.is_valid === false })) }], xLabel: 'lap', yLabel: 'seconds' })}</div>
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
        if (HistAnalysis.isLapSummaries(m)) { $('#htrkout').innerHTML = HistAnalysis.channelsUnavailable() + HistAnalysis.lapSectorTables(await lapsFor(code)); return; }
        const r = await api('GET', `/api/historical/sessions/${id}/telemetry?driver=${encodeURIComponent(code)}${lap ? `&lap=${lap}` : ''}&limit=1500`);
        $('#htrkout').innerHTML = histTraceCharts(r.samples) + (r.samples.length ? `<p class="hint">${r.count} samples${lap ? ` on lap ${lap}` : ''} &middot; distance on the x-axis (an estimate: speed integrated within the lap) &middot; throttle is a percentage; brake is an on/off flag (the timing feed publishes no brake pressure); DRS is a raw state code.</p>` : '');
      } else {
        if (HistAnalysis.isLapSummaries(m)) { $('#htrkout').innerHTML = HistAnalysis.unavailableChart('Track map', HistAnalysis.trackMissing([]), 'track'); return; }
        const r = await api('GET', `/api/historical/sessions/${id}/track-map?driver=${encodeURIComponent(code)}${lap ? `&lap=${lap}` : ''}`);
        const noPos = HistAnalysis.trackMissing(r.points || []);
        if (noPos.length) { $('#htrkout').innerHTML = HistAnalysis.unavailableChart('Track map', noPos, 'track'); return; }
        $('#htrkout').innerHTML = `<div class="panel"><div class="mono">${histSwatch(drivers, $('#htrkdrv').value, '#94a3b8')}<b>${esc($('#htrkdrv').value)}</b></div>${Charts.trackMap({ points: r.points || [], corners: r.corners || [] })}</div><p class="hint">Actual X/Y location from the timing feed${lap ? `, lap ${lap}` : ', fastest available lap stream'}. OpenF1 coordinates are approximate, in OpenF1's own units with an arbitrary origin (not verified as metres); no corner markers are available. Running order (P1, P2, ...) is a different dataset: see the Race tab.</p>`;
      }
    };
    $('#htrkdrv').addEventListener('change', async () => { await fillLaps(); show(); });
    $('#htrklap').addEventListener('change', show);
    await fillLaps(); show();
  } else if (tab === 'compare') {
    if (HistAnalysis.isLapSummaries(m)) { body.innerHTML = '<div class="note info">Driver comparison needs sampled telemetry, which this session does not hold.</div>' + HistAnalysis.channelsUnavailable(); return; }
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
        const cA = histColour(drivers, r.a.driver, '#e10600'), cB = histColour(drivers, r.b.driver, '#4aa8ff');
        const [dA, dB] = typeof HistAnalysis !== 'undefined' ? HistAnalysis.distinguish([{ color: cA }, { color: cB }]).map((x) => x.dash) : ['0', '0'];
        const ser = (pts, key, color, name, norm, dash) => ({ name, color, dash, pts: pts.map((p) => ({ x: (p.distance_m || 0) / norm * 100, y: p[key] })).filter((p) => p.y !== null && p.y !== undefined) });
        $('#hcmpout').innerHTML = `<div class="note info">${esc(r.delta.note)}</div>
          <div class="panel"><h2>Speed vs distance</h2>${Charts.lines({ series: [ser(r.a.points, 'speed_kph', cA, `${r.a.driver} lap ${r.a.lap}`, pctA, dA), ser(r.b.points, 'speed_kph', cB, `${r.b.driver} lap ${r.b.lap}`, pctB, dB)], xLabel: 'lap distance (%)', yLabel: 'km/h' })}</div>
          <div class="grid g2"><div class="panel"><h2>Throttle</h2>${Charts.lines({ series: [ser(r.a.points, 'throttle_pct', cA, `${r.a.driver} (A)`, pctA, dA), ser(r.b.points, 'throttle_pct', cB, `${r.b.driver} (B)`, pctB, dB)], xLabel: 'lap distance (%)', yLabel: '%' })}</div>
          <div class="panel"><h2>Brake (on/off)</h2>${Charts.lines({ series: [ser(r.a.points, brakeOf(r.a.points), cA, `${r.a.driver} (A)`, pctA, dA), ser(r.b.points, brakeOf(r.b.points), cB, `${r.b.driver} (B)`, pctB, dB)], xLabel: 'lap distance (%)', yLabel: 'applied (0/100)' })}</div></div>
          <div class="panel"><h2>Time delta (B &minus; A)</h2>${Charts.lines({ series: [{ name: 't_B - t_A (s)', color: '#ffb400', pts: r.delta.distance_pct.map((x, i) => ({ x: x * 100, y: r.delta.delta_s[i] })) }], xLabel: 'lap distance (%)', yLabel: 'seconds', ymin: Math.min(...r.delta.delta_s) - 0.05, ymax: Math.max(...r.delta.delta_s) + 0.05 })}
          <p class="hint">Positive delta = driver B is slower than A at that point of the lap. Delta is computed on an interpolated common distance grid, not raw samples.</p></div>`;
      } catch (e) { $('#hcmpmsg').innerHTML = `<span class="badc">${esc(e.message)}</span>`; }
    });
  } else if (tab === 'minisectors') {
    await HistAnalysis.miniSectorsTab(body, api, id, m);
  } else if (tab === 'tyres') {
    await HistAnalysis.tyresTab(body, api, id, m);
  } else if (tab === 'race') {
    await HistRace.race(body, api, id, m);
  } else if (tab === 'strategy') {
    await HistRace.strategy(body, api, id, m, await api('GET', `/api/historical/sessions/${id}/laps`));
  } else if (tab === 'weather') {
    const [wx, laps, pitRes] = await Promise.all([api('GET', `/api/historical/sessions/${id}/weather`), api('GET', `/api/historical/sessions/${id}/laps`),
      api('GET', `/api/historical/sessions/${id}/datasets/pit_stops`).catch(() => ({ rows: [] }))]);
    const s = wx.samples || [];
    const wchart = (title, key, unit) => {
      const pts = downsample(s.map((p) => ({ x: p.time_s ?? 0, y: p[key] })).filter((p) => p.y !== null && p.y !== undefined));
      return s.length ? `<div class="panel"><h2>${title}</h2>${Charts.lines({ series: [{ name: title, color: '#4aa8ff', pts }], xLabel: 'session time (s)', yLabel: unit })}</div>` : `<div class="panel"><h2>${title}</h2><div class="empty">No data.</div></div>`;
    };
    const stints = {};
    laps.forEach((l) => { const k = `${l.driver}|${l.stint}`; if (l.compound || l.tyre_life != null) stints[k] = { driver: l.driver, stint: l.stint, compound: l.compound, tyre_life: l.tyre_life, laps: (stints[k]?.laps || 0) + 1 }; });
    // A recorded pit stop with no tyre-stint change near it means the stint feed is incomplete for that driver (the table would hide a stop).
    const startsOf = {};
    laps.forEach((l) => { if (l.stint == null) return; const a = startsOf[l.driver] || (startsOf[l.driver] = {}); a[l.stint] = Math.min(a[l.stint] ?? Infinity, l.lap_number); });
    const stintWarn = [];
    (pitRes.rows || []).forEach((p) => {
      const st = Object.values(startsOf[p.driver] || {});
      if (p.lap_number != null && st.length && !st.some((n) => n === p.lap_number || n === p.lap_number + 1)) stintWarn.push(`${p.driver}: pit stop on lap ${p.lap_number}`);
    });
    const stintNote = stintWarn.length ? `<div class="note warn" role="status"><b>Stint data may be incomplete.</b> The pit-stop feed records a stop with no matching tyre-stint change in OpenF1's stint feed (${stintWarn.map(esc).join('; ')}). The stint and lap counts below for these drivers are as published by OpenF1 and can understate the number of stints.</div>` : '';
    body.innerHTML = `<div class="note info">${esc(wx.note || 'Weather is sampled sparsely by the timing feed.')}</div>
      <div class="grid g2">${wchart('Air temperature', 'air_temp_c', '°C')}${wchart('Track temperature', 'track_temp_c', '°C')}${wchart('Humidity', 'humidity_pct', '%')}${wchart('Wind speed', 'wind_speed_ms', 'm/s')}${s.some((p) => p.rainfall_flag != null) ? wchart('Rainfall (0 = dry, 1 = rain)', 'rainfall_flag', 'flag') : wchart('Rainfall', 'rainfall_mm', 'mm')}${wchart('Pressure', 'pressure_hpa', 'hPa')}</div>
      ${typeof HistAnalysis !== 'undefined' ? HistAnalysis.windDirection(s) : ''}
      <div class="panel"><h2>Tyre strategy</h2>${stintNote}<table><thead><tr><th>Driver</th><th>Stint</th><th>Compound</th><th class="num">Tyre life</th><th class="num">Laps</th></tr></thead><tbody>
      ${Object.values(stints).map((t) => `<tr><td class="mono">${histSwatch(drivers, t.driver, '#94a3b8')}<b>${esc(t.driver)}</b></td><td class="mono">${esc(t.stint ?? '&mdash;')}</td><td>${esc(t.compound || '&mdash;')}</td><td class="num">${esc(t.tyre_life ?? '&mdash;')}</td><td class="num">${t.laps}</td></tr>`).join('') || '<tr><td colspan="5" class="dim">No stint data.</td></tr>'}</tbody></table></div>`;
  }
}

/* Speed/throttle/brake/RPM/gear/DRS traces vs distance for a historical lap.
   OpenF1's brake channel is an on/off 'brake pressed' flag, stored as brake_applied (0/100);
   brake_pct only exists if a source ever provides a real percentage. */
function histTraceCharts(samples) { return HistAnalysis.traceCharts(samples, { downsample }); }

/* -------------------------------------------------------------------- boot */
(async () => {
  try { await api('GET', '/api/health'); setLed('ok', 'online'); } catch (_) { setLed('bad', 'backend offline'); }
  route();
})();
