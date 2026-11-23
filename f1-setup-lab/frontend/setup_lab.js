/* F1 SETUP LAB: Setup Lab views (dashboard, new experiment, telemetry import, experiment tabs).
   Classic script: shares one global scope with app.js, which defines esc, fmt, $, view, META, ApiError, api, toast,
   errorsBox, setNav, setLed, downsample and the router. Loaded before app.js. */
let pollTimer = null;
const poll = (fn, ms = 1000) => { clearInterval(pollTimer); pollTimer = setInterval(fn, ms); };
const statusChip = (s) => `<span class="chip ${esc(s)}">${esc(s.replaceAll('_', ' '))}</span>`;
const progress = (c, t, cls = '') => `<div class="progress ${cls}"><i style="width:${t ? (100 * c) / t : 0}%"></i></div>`;
const label = (n) => (META?.factors[n]?.label) || n;
const flabel = (d, n) => d.factors.find((f) => f.name === n)?.label || label(n);
function setBanner(mode) {
  const b = $('#banner'); if (!b || !META.notices) return;
  b.textContent = META.notices[mode] || META.notices.synthetic;
}
const unit = (n) => (META?.factors[n]?.unit) || '';
const isActive = (d) => ['running', 'queued'].includes(d.status) || d.counts.running > 0 || d.counts.queued > 0 && d.status === 'running';



const TABS = [['design', 'Setup design'], ['runs', 'Run monitor'], ['telemetry', 'Telemetry'], ['analysis', 'Analysis'], ['optimise', 'Optimisation'], ['report', 'Report']];

function expHeader(d, tab) {
  setBanner(d.mode);
  const done = d.counts.completed + d.counts.failed + d.counts.cancelled;
  return `<div class="row"><div><h1>${esc(d.name)}</h1><p class="sub">${esc(d.hypothesis || 'No hypothesis stated')}</p></div><div class="spacer"></div>
    <div><span class="chip synthetic">SYNTHETIC</span> ${statusChip(d.status)}</div></div>
    <div class="row"><div style="flex:1">${progress(d.counts.completed, d.runs_total)}</div><span class="mono dim">${d.counts.completed}/${d.runs_total} runs complete &middot; budget ${d.runs_total}/${d.run_budget}</span></div>
    <div class="tabs">${TABS.map(([k, n]) => `<a href="#/e/${esc(d.id)}/${k}" class="${k === tab ? 'on' : ''}">${n}</a>`).join('')}</div>`;
}

/* --------------------------------------------------------------- dashboard */
async function viewDashboard() {
  const draw = async () => {
    const list = await api('GET', '/api/experiments');
    if (!$('#dash')) return;
    $('#dash').innerHTML = list.length ? `<table><thead><tr><th>Experiment</th><th>Status</th><th>Progress</th><th>Design</th><th class="num">Best feasible lap</th><th class="num">Stability</th><th>Created</th><th></th></tr></thead><tbody>
      ${list.map((e) => `<tr class="clickable" data-id="${esc(e.id)}"><td><b>${esc(e.name)}</b><div class="dim mono">${esc(e.id)}</div></td><td>${statusChip(e.status)}</td>
      <td style="min-width:150px">${progress(e.counts.completed, e.runs_total, 'sm')}<span class="dim mono">${e.counts.completed}/${e.runs_total}${e.counts.failed ? ` &middot; <span class="badc">${e.counts.failed} failed</span>` : ''}</span></td>
      <td>${esc(e.design_method.replaceAll('_', ' '))}</td><td class="num">${e.best ? fmt(e.best.lap_time, 3) + ' s' : '&mdash;'}</td><td class="num">${e.best ? fmt(e.best.stability, 1) : '&mdash;'}</td>
      <td class="dim mono">${esc(e.created_at.slice(0, 16).replace('T', ' '))}</td><td><a class="btn small" href="#/e/${esc(e.id)}/design">Open</a></td></tr>`).join('')}</tbody></table>`
      : `<div class="empty"><h2>No experiments yet</h2><p>Design a screening experiment, run it on the synthetic simulator, find the setup factors that matter and let the optimiser refine them.</p>
      <p><button class="btn primary" id="demo">Create demo experiment</button> &nbsp; <a class="btn" href="#/new">Configure my own</a></p></div>`;
    document.querySelectorAll('tr[data-id]').forEach((tr) => tr.addEventListener('click', (ev) => { if (!ev.target.closest('a')) location.hash = `#/e/${tr.dataset.id}/design`; }));
    $('#demo')?.addEventListener('click', async () => {
      try { const d = await api('POST', '/api/experiments', { name: 'Wing screening (demo)', hypothesis: 'A rearward aero balance improves lap time while keeping the car stable.' }); location.hash = `#/e/${d.id}/design`; } catch (e) { toast(e.message); }
    });
    return list;
  };
  view().innerHTML = `<div class="row"><div><h1>Dashboard</h1><p class="sub">Experiments on the synthetic vehicle simulator</p></div><div class="spacer"></div><a class="btn primary" href="#/new">Create new experiment</a></div><div class="panel" id="dash">Loading&hellip;</div>`;
  const first = await draw();
  poll(async () => { try { const l = await draw(); if (!l.some((e) => ['running', 'queued'].includes(e.status))) clearInterval(pollTimer); } catch (_) {} }, 2000);
  return first;
}

/* -------------------------------------------------------------- experiments */
async function viewExperiments() {
  let all = [];
  const paint = () => {
    const q = ($('#xq')?.value || '').trim().toLowerCase(), st = $('#xst')?.value || '';
    const list = all.filter((e) => (!st || e.status === st) && (!q || e.name.toLowerCase().includes(q) || e.id.toLowerCase().includes(q)));
    $('#xlist').innerHTML = !all.length ? `<div class="empty"><h2>No experiments yet</h2><p>Create an experiment to design a screening run on the synthetic simulator.</p><p><a class="btn primary" href="#/new">Create new experiment</a></p></div>`
      : !list.length ? `<div class="empty"><p>No experiments match the filter.</p></div>`
      : `<table><thead><tr><th>Experiment</th><th>Status</th><th>Progress</th><th>Design</th><th class="num">Best feasible lap</th><th>Created</th></tr></thead><tbody>
      ${list.map((e) => `<tr class="clickable" data-id="${esc(e.id)}"><td><b>${esc(e.name)}</b><div class="dim mono">${esc(e.id)}</div></td><td>${statusChip(e.status)}</td>
      <td style="min-width:150px">${progress(e.counts.completed, e.runs_total, 'sm')}<span class="dim mono">${e.counts.completed}/${e.runs_total}${e.counts.failed ? ` &middot; <span class="badc">${e.counts.failed} failed</span>` : ''}</span></td>
      <td>${esc(e.design_method.replaceAll('_', ' '))}</td><td class="num">${e.best ? fmt(e.best.lap_time, 3) + ' s' : '&mdash;'}</td>
      <td class="dim mono">${esc(e.created_at.slice(0, 16).replace('T', ' '))}</td></tr>`).join('')}</tbody></table>`;
    document.querySelectorAll('#xlist tr[data-id]').forEach((tr) => tr.addEventListener('click', () => { location.hash = `#/e/${tr.dataset.id}/design`; }));
  };
  const draw = async () => {
    all = await api('GET', '/api/experiments');
    if (!$('#xlist')) return all;
    $('#xtools').hidden = !all.length;
    const sel = $('#xst'), cur = sel.value, seen = [...new Set(all.map((e) => e.status))].sort();
    sel.innerHTML = '<option value="">All statuses</option>' + seen.map((x) => `<option value="${esc(x)}">${esc(x.replaceAll('_', ' '))}</option>`).join('');
    sel.value = seen.includes(cur) ? cur : '';
    paint();
    return all;
  };
  view().innerHTML = `<div class="page-head"><div><h1>Experiments</h1><p class="sub">All experiments on the synthetic vehicle simulator</p></div><a class="btn primary" id="xnew" href="#/new">Create new experiment</a></div>
    <div class="row filters" id="xtools" hidden><input id="xq" type="search" placeholder="Search by name or id" aria-label="Search experiments"><select id="xst" aria-label="Filter by status"><option value="">All statuses</option></select></div>
    <div class="panel" id="xlist">Loading&hellip;</div>`;
  $('#xq').addEventListener('input', paint); $('#xst').addEventListener('change', paint);
  const first = await draw();
  poll(async () => { try { const l = await draw(); if (!l.some((e) => ['running', 'queued'].includes(e.status))) clearInterval(pollTimer); } catch (_) {} }, 2000);
  return first;
}
/* ----------------------------------------------------------- new experiment */
async function viewNew() {
  const fdef = META.default_factors;
  view().innerHTML = `<div class="page-head"><div><h1>Create experiment</h1></div><a class="btn" href="#/experiments">Back to experiments</a></div><p class="sub">Define the setup variables, the objective and the stability constraint, then generate the design.</p><div id="err"></div>
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
      <tr><td class="dim">Constraint</td><td>stability &ge; ${esc(d.min_stability)}</td></tr><tr><td class="dim">Budget</td><td>${d.run_budget} runs</td></tr></tbody></table>
      <p class="hint">Coded levels: <span class="coded m1">-1</span> low end of range, <span class="coded p1">+1</span> high end, <span class="coded z">0</span> centre.</p></div>
      ${alias ? `<div class="panel"><h2>Alias structure</h2><table><tbody>${alias}</tbody></table></div>` : ''}</div></div>
    <div class="row"><button class="btn primary" id="go" ${d.counts.queued ? '' : 'disabled'}>Run ${d.counts.queued} queued setups</button><span class="dim">Runs execute on the synthetic simulator, 4 at a time.</span></div>`;
  $('#go').addEventListener('click', async () => { try { await api('POST', `/api/experiments/${id}/execute`); location.hash = `#/e/${id}/runs`; } catch (e) { toast(e.message); } });
}

/* ------------------------------------------------------------- run monitor */
function runCharts(d) {
  const done = d.runs.filter((r) => r.status === 'completed' && r.outcomes.lap_time !== undefined).sort((a, b) => a.order - b.order);
  let best = Infinity; const step = [];
  done.forEach((r) => { if (r.feasible && r.outcomes.lap_time < best) best = r.outcomes.lap_time; if (best < Infinity) step.push({ x: r.order + 1, y: best }); });
  const bestId = d.best?.run_id;
  const pts = done.map((r) => ({ x: r.order + 1, y: r.outcomes.lap_time, cls: r.run_id === bestId ? 'pt-best' : r.feasible ? 'pt-ok' : 'pt-bad', r: r.run_id === bestId ? 7 : 4.5, title: `Run ${r.order + 1}: ${fmt(r.outcomes.lap_time, 3)} s, stability ${fmt(r.outcomes['stability'], 1)}${r.feasible ? '' : ' (INFEASIBLE)'}` }));
  const sc = done.filter((r) => r.outcomes['stability'] !== undefined).map((r) => ({ x: r.outcomes.lap_time, y: r.outcomes['stability'], cls: r.run_id === bestId ? 'pt-best' : r.feasible ? 'pt-ok' : 'pt-bad', r: r.run_id === bestId ? 7 : 4.5, title: `Run ${r.order + 1}` }));
  return `<div class="grid g2"><div class="panel"><h2>Lap time by run</h2>${Charts.scatter({ points: pts, step, xLabel: 'run', yLabel: 'mean lap (s)', label: 'lap time by run' })}
    <div class="legend"><span><i style="background:var(--green)"></i>feasible</span><span><i style="background:var(--bad)"></i>violates stability</span><span><i style="background:var(--amber)"></i>best feasible</span><span>line = best feasible so far</span></div></div>
    <div class="panel"><h2>Stability vs lap time</h2>${Charts.scatter({ points: sc, lines: d.min_stability != null ? [{ h: d.min_stability, label: `min ${d.min_stability}` }] : [], shade: d.min_stability != null ? { y0: 0, y1: d.min_stability } : null, ymin: sc.length ? Math.min(...sc.map((p) => p.y), d.min_stability - 3) : undefined, xLabel: 'mean lap (s)', yLabel: 'stability score', label: 'stability vs lap time' })}
    <div class="legend"><span>shaded area = infeasible region</span></div></div></div>`;
}
async function viewRuns(id) {
  view().innerHTML = '<div id="runs-root"></div>';
  let open = null;
  const draw = async () => {
    const d = await api('GET', `/api/experiments/${id}`);
    const root = $('#runs-root'); if (!root) return d;
    const names = d.factors.map((f) => f.name), c = d.counts, active = c.running + c.queued > 0 && d.status !== 'created';
    setLedFor(d);
    root.innerHTML = expHeader(d, 'runs') + `<div class="kpis"><div class="kpi"><b>${c.completed}</b><span>completed</span></div><div class="kpi"><b>${c.running}</b><span>running</span></div><div class="kpi"><b>${c.queued}</b><span>queued</span></div>
      <div class="kpi ${c.failed ? 'bad' : ''}"><b>${c.failed}</b><span>failed</span></div><div class="kpi good"><b>${d.best ? fmt(d.best.lap_time, 3) : '&mdash;'}</b><span>best feasible lap (s)</span></div></div>
      <div class="row" style="margin-bottom:12px"><button class="btn primary" id="run" ${c.queued && !c.running ? '' : 'disabled'}>Run queued</button><button class="btn" id="cancel" ${c.queued || c.running ? '' : 'disabled'}>Cancel</button>
      <button class="btn" id="retry" ${c.failed ? '' : 'disabled'}>Retry ${c.failed} failed</button><span class="spacer"></span><a class="btn" href="/api/experiments/${esc(id)}/export/runs.csv">Export runs CSV</a></div>
      ${runCharts(d)}<div class="panel"><h2>Runs</h2><div style="overflow:auto;max-height:560px"><table><thead><tr><th>#</th><th>Status</th>${names.map((n) => `<th class="num">${esc(flabel(d, n))}</th>`).join('')}<th class="num">Lap (s)</th><th class="num">Stability</th><th>Feasible</th><th class="num">Tries</th><th>Source</th><th></th></tr></thead><tbody>
      ${d.runs.map((r) => `<tr><td class="mono">${r.order + 1}</td><td>${statusChip(r.status)}${r.status === 'running' ? ' <i class="led run"></i>' : ''}</td>${names.map((n) => `<td class="num">${fmt(r.configuration[n], 2)}</td>`).join('')}
      <td class="num">${r.outcomes.lap_time !== undefined ? fmt(r.outcomes.lap_time, 3) : ''}</td><td class="num">${r.outcomes['stability'] !== undefined ? fmt(r.outcomes['stability'], 1) : ''}</td>
      <td>${r.feasible === true ? '<span class="good">yes</span>' : r.feasible === false ? '<span class="badc">NO</span>' : ''}</td><td class="num">${r.attempts}</td>
      <td>${r.imported ? '<span class="chip imported">imported</span>' : r.status === 'completed' ? '<span class="chip synthetic">synthetic</span>' : ''}${r.iteration ? ` <span class="chip model">opt ${r.iteration}</span>` : ''}</td>
      <td>${r.error ? `<span class="badc" title="${esc(r.error)}">${esc(r.error.slice(0, 60))}</span>` : r.status === 'queued' ? `<button class="btn small" data-imp="${esc(r.run_id)}" data-n="${r.order + 1}">Import telemetry</button>` : ''}</td></tr>`).join('')}</tbody></table></div></div>`;
    $('#run')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${id}/execute`); poll(draw, 700); draw(); } catch (e) { toast(e.message); } });
    $('#cancel')?.addEventListener('click', async () => { await api('POST', `/api/experiments/${id}/cancel`); draw(); });
    $('#retry')?.addEventListener('click', async () => { try { await api('POST', `/api/experiments/${id}/retry`); poll(draw, 700); draw(); } catch (e) { toast(e.message); } });
    document.querySelectorAll('[data-imp]').forEach((b) => b.addEventListener('click', () => importModal(id, b.dataset.imp, b.dataset.n, draw)));
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
      const sc = Object.values(a.runs).filter((r) => r.status === 'completed' && r.outcomes['stability'] !== undefined).map((r) => ({ x: r.outcomes.lap_time, y: r.outcomes['stability'], cls: r.run_id === a.best_run_id ? 'pt-best' : r.feasible ? 'pt-ok' : 'pt-bad', r: r.run_id === a.best_run_id ? 7 : 4.5, title: `Run ${r.order + 1}` }));
      const pair = names.length >= 2 ? [names[0], names[1]] : null;
      $('#res').innerHTML = recoCard(a.recommendation) + `<div class="grid g2"><div class="panel"><h2>Factor effects on lap time (low &rarr; high of tested range)</h2>${Charts.bars({ items: eff, goodNegative: true, xLabel: 'seconds (negative = faster); whiskers = 95% CI; * significant' })}
      <p class="hint">R&sup2; ${fmt(fx.r_squared, 3)} &middot; adj. ${fmt(fx.adjusted_r_squared, 3)} &middot; residual df ${fx.residual_df ?? 'n/a'}. Bars show the <em>linear</em> effect of moving a factor from the low to the high end (regression on coded factors). The ANOVA below treats levels as categories, so it can also flag <em>curvature</em> (centre vs ends) that a straight line misses &mdash; a factor can be non-significant here and significant there.</p></div>
      <div class="panel"><h2>Feasibility: stability vs lap time</h2>${Charts.scatter({ points: sc, lines: d.min_stability != null ? [{ h: d.min_stability, label: `min ${d.min_stability}` }] : [], shade: d.min_stability != null ? { y0: 0, y1: d.min_stability } : null, ymin: sc.length ? Math.min(...sc.map((p) => p.y), d.min_stability - 3) : undefined, xLabel: 'mean lap (s)', yLabel: 'stability' })}
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

