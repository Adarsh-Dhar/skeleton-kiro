/* WATTWISE LAB: household setup page. Zero fabricated input data: the form starts EMPTY, nothing is filled in with example values,
   and every value carries its source (Sources.srcFields). Loaded after app.js; uses its helpers at run time. */
/* ------------------------------------------------------------ 2. household configuration */
const DAY_CHOICES = [['weekday', 'weekday'], ['weekend', 'weekend'], ['mon', 'Monday'], ['tue', 'Tuesday'], ['wed', 'Wednesday'], ['thu', 'Thursday'], ['fri', 'Friday'], ['sat', 'Saturday'], ['sun', 'Sunday']];
const WD = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const val = (v) => (v === null || v === undefined || Number.isNaN(v) ? '' : esc(v));
const optVal = (sel, root = document) => { const v = $(sel, root).value.trim(); return v === '' ? null : parseFloat(v); };
function readinessBox(st) {
  if (!st) return '';
  const r = st.readiness || { ready: false, missing: [], warnings: [] };
  const miss = r.missing.length ? `<div class="errors" role="status"><b>Configuration required before anything can be projected</b><ul>${r.missing.map((m) => `<li><code>${esc(m.field)}</code> ${esc(m.message)}</li>`).join('')}</ul></div>` : '<div class="note info"><b>Ready:</b> every required input is configured.</div>';
  const warn = r.warnings.length ? `<ul class="warnlist">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : '';
  return `${miss}${warn}${st.notice ? `<div class="note">${esc(st.notice)}</div>` : ''}`;
}
const SEV = { blocking: ['BLOCKING', 'prov-s'], confirm: ['TO CONFIRM', 'prov-mt'], warning: ['WARNING', 'prov-u'] };
function auditBox(a) {
  if (!a.findings.length) return `<div class="note info"><b>Data audit:</b> no findings; ${a.counts.clean_inputs} input(s) have a source and nothing is blocked.</div><details class="panel"><summary>Real-data sign-off</summary>${signoffBox(a)}</details>`;
  return `<details class="panel" id="auditbox" ${a.counts.blocking || a.counts.confirm ? 'open' : ''}><summary><b>Data audit</b>: ${a.counts.blocking} blocking &middot; ${a.counts.confirm} to confirm &middot; ${a.counts.warning} warning(s) &middot; ${a.counts.clean_inputs} clean</summary>
    <div class="scroll"><table><thead><tr><th></th><th>Input</th><th>Finding</th><th>What to do</th></tr></thead><tbody>${a.findings.map((f) => `<tr><td><span class="chip ${SEV[f.severity][1]}">${SEV[f.severity][0]}</span></td>
      <td class="mono">${esc(f.input)}</td><td>${esc(f.message)}</td><td class="dim">${esc(f.action)}</td></tr>`).join('')}</tbody></table></div>
    ${signoffBox(a)}<ul class="hint">${a.limits.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></details>`;
}
const signoffBox = (a) => `<h3>Real-data sign-off: ${a.signoff.passed ? '<span class="chip prov-m">PASSED</span>' : '<span class="chip prov-s">NOT PASSED</span>'}</h3>
  <ul style="list-style:none;padding-left:0">${a.signoff.criteria.map((c) => `<li>${c.passed ? '&#9745;' : '&#9744;'} ${esc(c.criterion)}</li>`).join('')}</ul>
  <p class="hint">Bill reconciliation is on <a href="#/usage">My usage</a> (open an import, then "Reconcile with my bill").</p>`;
function examplesBox(list) {
  if (!list || !list.length) return '';
  return `<div class="errors"><b>Copies of the old built-in examples</b> (saved before source tracking; not used):<ul>${list.map((x, i) => `<li><code>${esc(x.input)}</code> ${esc(Object.entries(x.values).filter(([k, v]) => !['source', 'id', 'in_baseline', 'name'].includes(k) && v !== null && v !== undefined).map(([k, v]) => `${k} ${v}`).join(', '))}
    ${x.input.startsWith('appliances[') || x.input === 'base_load' ? ` <button type="button" class="btn small" data-adopt="${i}">This is my real value</button>` : ''}</li>`).join('')}</ul>
    <span class="hint">Only adopt a value you have checked yourself; you then have to say where it comes from. Saving the household removes the rest.</span></div>`;
}
async function viewHousehold() {
  const st = HSTATE || {};
  const blankTariff = () => ({ name: '', periods: [{ label: '', start: 0, end: 24, price: null }], demand_charge_per_kw: 0 });
  const current = tariffOf(HH.tariff_id);
  let creating = !current;
  let editing = current ? JSON.parse(JSON.stringify(current)) : blankTariff();
  const evSeed = HH.ev || st.ev_draft || null; // a draft is the owner's saved EV that lacks a value this version needs
  const renderPeriods = () => $('#periods').innerHTML = editing.periods.map((p, i) => `<tr data-i="${i}"><td><input data-f="label" value="${val(p.label)}" maxlength="30" aria-label="Period name" style="width:84px"></td>
    <td><input data-f="start" type="number" step="0.25" min="0" max="24" value="${val(p.start)}" aria-label="Start hour" style="width:58px"></td><td><input data-f="end" type="number" step="0.25" min="0" max="24" value="${val(p.end)}" aria-label="End hour" style="width:58px"></td>
    <td><input data-f="price" type="number" step="0.0001" min="0" max="10" value="${val(p.price)}" placeholder="from bill" aria-label="Price per kWh" style="width:84px"></td>
    <td><select data-f="days" aria-label="Days" style="width:92px">${['all', 'weekday', 'weekend'].map((d) => `<option value="${d}" ${(p.days || 'all') === d ? 'selected' : ''}>${d}</option>`).join('')}</select></td>
    <td><span class="wdays" role="group" aria-label="Weekdays this price applies to (none ticked = every day)">${['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((l, d) => `<label style="margin-right:3px" title="${WD[d]}"><input type="checkbox" data-wd="${d}" ${(p.weekdays || []).includes(d) ? 'checked' : ''} aria-label="${WD[d]}">${l}</label>`).join('')}</span></td>
    <td><input data-f="months" value="${esc((p.months || []).join(','))}" placeholder="all" aria-label="Months (1-12, comma separated)" style="width:60px"></td>
    <td><button type="button" class="btn small danger" data-del="${i}" aria-label="Remove period" title="Remove period">&times;</button></td></tr>`).join('');
  const renderTiers = () => $('#tiers').innerHTML = (editing.tiers || []).map((t, i) => `<tr><td><input data-t="up" type="number" step="0.1" min="0" value="${val(t.up_to_kwh)}" placeholder="no limit" aria-label="Tier up to kWh"></td>
    <td><input data-t="adder" type="number" step="0.001" value="${val(t.adder_per_kwh)}" aria-label="Tier price adder per kWh"></td><td><button type="button" class="btn small danger" data-tdel="${i}">Remove</button></td></tr>`).join('');
  const readTiers = () => $$('#tiers tr').map((tr) => { const up = $('[data-t=up]', tr).value.trim(); return { up_to_kwh: up === '' ? null : parseFloat(up), adder_per_kwh: parseFloat($('[data-t=adder]', tr).value) || 0 }; });
  // every field of the tariff model, with its source: an extended tariff is never saved without its advanced parts
  const readTariff = () => {
    const t = { name: $('#tname').value, demand_charge_per_kw: parseFloat($('#tdemand').value) || 0, source: Sources.readSrc('tsrc'),
      periods: $$('#periods tr').map((tr) => {
        const p = { label: $('[data-f=label]', tr).value, start: parseFloat($('[data-f=start]', tr).value), end: parseFloat($('[data-f=end]', tr).value), price: parseFloat($('[data-f=price]', tr).value) };
        const days = $('[data-f=days]', tr).value, months = $('[data-f=months]', tr).value.split(',').map((m) => parseInt(m, 10)).filter((m) => Number.isInteger(m));
        if (days !== 'all') p.days = days;
        const wds = $$('[data-wd]', tr).filter((c) => c.checked).map((c) => parseInt(c.dataset.wd, 10));
        if (wds.length) p.weekdays = wds;
        if (months.length) p.months = months;
        return p;
      }) };
    for (const [k, id] of [['utility', '#tutil'], ['consumer_category', '#tcat'], ['currency', '#tcur']]) { const v = $(id).value.trim(); if (v) t[k] = v; }
    for (const [k, id] of [['fixed_daily_charge', '#tfixd'], ['fixed_monthly_charge', '#tfixm'], ['surcharge_per_kwh', '#tsur'], ['tax_pct', '#ttax'], ['export_credit_per_kwh', '#texp']]) {
      const v = optVal(id); if (v !== null && v !== 0) t[k] = v;
    }
    const bd = optVal('#tbilld'); if (bd !== null) t.billing_days = Math.round(bd);
    if ($('#tdbasis').value !== 'daily') t.demand_basis = $('#tdbasis').value;
    const tiers = readTiers(); if (tiers.length) { t.tiers = tiers; t.tier_basis = $('#ttbasis').value; }
    if (t.periods.some((p) => p.days || p.months || p.weekdays)) t.simulation_day = { month: parseInt($('#tsimm').value, 10) || 1, day_type: $('#tsimd').value };
    return t;
  };
  const validate = async () => {
    const t = readTariff(); editing = { ...editing, ...t };
    if (t.periods.some((p) => Number.isNaN(p.price))) { $('#tvalid').innerHTML = '<div class="note">Enter the price of every period from your bill or tariff order.</div>'; $('#tband').innerHTML = ''; return; }
    try {
      const r = await api('POST', '/api/tariffs/validate', t);
      $('#tvalid').innerHTML = r.valid ? '<div class="note info">Prices cover all 24 hours with no gaps or overlaps.</div>' : `<div class="errors" role="alert"><ul>${r.issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul></div>`;
      $('#tband').innerHTML = r.valid ? bandBar({ periods: t.periods }) : '';
    } catch (e) { $('#tvalid').innerHTML = errorsBox(e); }
  };
  const appRow = (a, i) => `<tr data-a="${i}"><td><input data-f="name" value="${val(a.name)}" maxlength="40" placeholder="e.g. washing machine" aria-label="Appliance name"></td>
    <td><input data-f="power_kw" type="number" step="0.01" min="0.01" max="10" value="${val(a.power_kw)}" placeholder="rating plate" aria-label="Power kW" style="width:84px"></td>
    <td><input data-f="duration_h" type="number" step="0.25" min="0.25" max="8" value="${val(a.duration_h)}" aria-label="Duration hours" style="width:64px"></td><td><input data-f="flexible" type="checkbox" ${a.flexible ? 'checked' : ''} aria-label="Can be moved"></td>
    <td><input data-f="fixed_start" type="time" value="${a.fixed_start === null || a.fixed_start === undefined ? '' : toTime(a.fixed_start)}" aria-label="Usual start time"></td>
    <td><button type="button" class="btn small danger" data-adel="${i}">Remove</button></td></tr>
    <tr class="srcrow" data-src="${i}"><td colspan="6">${Sources.srcFields(`asrc-${i}`, a.source || null, 'appliance')}</td></tr>`;
  let appList = HH.appliances.map((a) => ({ ...a }));
  const apps = () => appList.map(appRow).join('') || '<tr><td colspan="6" class="dim">No appliances yet. Add the ones you can schedule, with the power from their rating plate.</td></tr>';
  const example = current && current.source_status && !current.source_status.usable;
  view().innerHTML = `<h1>Household setup</h1><p class="sub">Enter your own data: the tariff from your bill, your appliances, your car and your meter readings. Every value needs its source. Nothing is filled in for you, and results are projections from these inputs.</p><div id="err"></div>
  <div id="ready">${readinessBox(st)}</div>${examplesBox(st.example_values)}<div id="audit"></div>
  <div class="grid g2"><div>
    <div class="panel"><h2>Electricity tariff</h2>
      ${META.tariffs.length ? '' : '<div class="note info">No tariff yet. Enter your tariff from your latest electricity bill or your DISCOM\u2019s current tariff order, then press <b>Save tariff</b>.</div>'}
      <div class="field"><label for="tsel">Tariff</label><select id="tsel"><option value="">&mdash; none selected &mdash;</option>${META.tariffs.map((t) => `<option value="${esc(t.id)}" ${t.id === HH.tariff_id ? 'selected' : ''}>${esc(t.name)}${t.source_status && !t.source_status.usable ? ' (example, not usable)' : ''}</option>`).join('')}<option value="__new" ${creating && META.tariffs.length ? 'selected' : ''}>+ New tariff</option></select></div>
      ${current ? `<p class="hint">Status: ${Sources.badge(current.source_status)} ${Sources.summary(current.source)}</p>` : ''}
      ${example ? '<div class="errors" role="alert">This tariff is a built-in example from an earlier version, not a real tariff. It cannot be used for any calculation: enter your own tariff and delete this one.</div>' : ''}
      <div class="grid g2"><div class="field"><label for="tname">Name</label><input id="tname" value="${val(editing.name)}" maxlength="60" placeholder="e.g. LT-1 Domestic 2025-26"></div>
        <div class="field"><label for="tutil">Utility (DISCOM)</label><input id="tutil" value="${val(editing.utility)}" maxlength="120"></div>
        <div class="field"><label for="tcat">Consumer category</label><input id="tcat" value="${val(editing.consumer_category)}" maxlength="120" placeholder="as printed on the bill"></div>
        <div class="field"><label for="tcur">Currency</label><input id="tcur" value="${val(editing.currency)}" maxlength="8" placeholder="e.g. INR"></div></div>
      ${Sources.srcFields('tsrc', editing.source || null, 'tariff')}
      <div id="tband">${editing.periods.every((p) => Number.isFinite(p.price)) ? bandBar(editing) : ''}</div>
      <div class="scroll" style="overflow-x:auto"><table><thead><tr><th>Period</th><th>From (h)</th><th>To (h)</th><th>Price / kWh</th><th>Days</th><th>Weekdays</th><th>Months</th><th></th></tr></thead><tbody id="periods"></tbody></table></div>
      <div class="row" style="margin:8px 0"><button type="button" class="btn small" id="addp">Add period</button><span class="hint">Hours run 0&ndash;24; a period such as 23 to 7 wraps past midnight.</span></div>
      <div class="field"><label for="tdemand">Demand charge per kW of peak</label><input id="tdemand" type="number" step="0.01" min="0" max="100" value="${editing.demand_charge_per_kw || 0}"></div>
      <details id="tadv" ${['fixed_daily_charge', 'fixed_monthly_charge', 'surcharge_per_kwh', 'tax_pct', 'export_credit_per_kwh', 'tiers', 'demand_basis'].some((k) => editing[k]) || editing.periods.some((p) => p.days || p.months || (p.weekdays || []).length) ? 'open' : ''}>
        <summary>Advanced charges (copy them from your bill; leave empty if your bill has none)</summary>
        <div class="grid g2">
          <div class="field"><label for="tfixd">Fixed charge per day</label><input id="tfixd" type="number" step="0.01" min="0" max="1000" value="${val(editing.fixed_daily_charge)}"></div>
          <div class="field"><label for="tfixm">Fixed charge per month</label><input id="tfixm" type="number" step="0.01" min="0" max="10000" value="${val(editing.fixed_monthly_charge)}"></div>
          <div class="field"><label for="tbilld">Days per billing period</label><input id="tbilld" type="number" step="1" min="1" max="366" value="${val(editing.billing_days)}" placeholder="from bill"></div>
          <div class="field"><label for="tdbasis">Demand charge applies to</label><select id="tdbasis"><option value="daily" ${(editing.demand_basis || 'daily') === 'daily' ? 'selected' : ''}>each day's peak</option><option value="billing_period" ${editing.demand_basis === 'billing_period' ? 'selected' : ''}>the billing period's peak</option></select></div>
          <div class="field"><label for="tsur">Surcharge per kWh (e.g. fuel adjustment)</label><input id="tsur" type="number" step="0.001" min="0" max="10" value="${val(editing.surcharge_per_kwh)}"></div>
          <div class="field"><label for="ttax">Tax or electricity duty (% of all charges)</label><input id="ttax" type="number" step="0.01" min="0" max="100" value="${val(editing.tax_pct)}"></div>
          <div class="field"><label for="texp">Export credit per kWh (solar)</label><input id="texp" type="number" step="0.001" min="0" max="10" value="${val(editing.export_credit_per_kwh)}"></div>
          <div class="field"><label for="ttbasis">Tier thresholds count energy per</label><select id="ttbasis"><option value="daily" ${(editing.tier_basis || 'daily') === 'daily' ? 'selected' : ''}>day</option><option value="billing_period" ${editing.tier_basis === 'billing_period' ? 'selected' : ''}>billing period</option></select></div></div>
        <h3>Tiered pricing (slabs)</h3><p class="hint">Each tier adds to the period price for the kWh up to its limit; the last tier has no limit. Leave empty for no tiers.</p>
        <table><thead><tr><th>Up to kWh</th><th>Extra price / kWh</th><th></th></tr></thead><tbody id="tiers"></tbody></table>
        <button type="button" class="btn small" id="addtier">Add tier</button>
        <h3>Day used by the one-day projection</h3><p class="hint">Only matters when periods are limited to days, weekdays or months. Historical meter data is priced on each reading's real date.</p>
        <div class="grid g2"><div class="field"><label for="tsimm">Month</label><input id="tsimm" type="number" min="1" max="12" value="${editing.simulation_day?.month ?? 1}"></div>
          <div class="field"><label for="tsimd">Day type</label><select id="tsimd">${DAY_CHOICES.map(([v, l]) => `<option value="${v}" ${(editing.simulation_day?.day_type || 'weekday') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div></div>
      </details>
      <div id="tvalid"></div>
      <div class="row">${creating ? '<button type="button" class="btn primary" id="tnew">Save tariff</button>' : '<button type="button" class="btn primary" id="tsave">Save changes</button><button type="button" class="btn" id="tnew">Save as new tariff</button><button type="button" class="btn danger" id="tdel">Delete</button>'}</div></div>
  </div><div>
    <div class="panel" id="evpanel"><h2>Electric vehicle</h2>
      <div class="field"><label class="inline"><input type="checkbox" id="noev" ${evSeed && !st.ev_draft?.example ? '' : 'checked'}> No EV (schedule appliances only)</label></div>
      ${st.ev_draft && !HH.ev ? (st.ev_draft.example ? '<div class="errors">The saved EV is a copy of the example EV earlier versions filled in. Untick "No EV" only if these are your car\u2019s real values, and choose their source.</div>'
        : `<div class="note">Your saved EV is missing ${esc((st.ev_draft.missing || []).join(', '))}. Fill it in and save to use the EV.</div>`) : ''}
      <fieldset id="evfields" style="border:0;padding:0;margin:0"><div class="grid g2">
      <div class="field"><label for="ev-bat">Usable battery capacity (kWh)</label><input id="ev-bat" type="number" step="0.1" min="10" max="200" value="${val(evSeed?.battery_kwh)}" placeholder="vehicle specification"></div>
      <div class="field"><label for="ev-eff">Charging efficiency, grid to battery (0.5&ndash;1)</label><input id="ev-eff" type="number" step="0.01" min="0.51" max="1" value="${val(evSeed?.efficiency)}" placeholder="manual or measured"></div>
      <div class="field"><label for="ev-init">Typical charge when you plug in (%)</label><input id="ev-init" type="number" step="1" min="0" max="100" value="${val(evSeed?.initial_soc_pct)}"></div>
      <div class="field"><label for="ev-want">Charge needed at departure (%)</label><input id="ev-want" type="number" step="1" min="1" max="100" value="${val(evSeed?.desired_charge_pct)}"></div>
      <div class="field"><label for="ev-plug">Usually plugged in at</label><input id="ev-plug" type="time" value="${evSeed?.plug_in_hour === undefined || evSeed?.plug_in_hour === null ? '' : toTime(evSeed.plug_in_hour)}"></div>
      <div class="field"><label for="ev-dep">Departure time</label><input id="ev-dep" type="time" value="${evSeed?.departure_hour === undefined || evSeed?.departure_hour === null ? '' : toTime(evSeed.departure_hour)}"></div></div>
      ${Sources.srcFields('evsrc', evSeed?.source || null, 'ev')}
      <div class="field"><label for="ev-charger">Home charger capacity, kW</label><input id="ev-charger" type="number" step="0.01" min="1.4" max="350" value="${val(HH.charger_power_kw)}" placeholder="charger nameplate"></div>
      ${Sources.srcFields('chsrc', HH.charger_source || null, 'charger')}
      <p class="hint">The needed charge is the constraint: no schedule that leaves the car below it is ever recommended. Your plug-in time and charger capacity are today's schedule, the baseline every saving is measured against.</p></fieldset></div>
    <div class="panel" id="apppanel"><h2>Appliances</h2><div class="scroll"><table><thead><tr><th>Name</th><th>kW</th><th>Hours</th><th>Movable</th><th>Usual start</th><th></th></tr></thead><tbody id="apps">${apps()}</tbody></table></div>
      <div class="row" style="margin:8px 0"><button type="button" class="btn small" id="adda">Add appliance</button><span class="hint">Usual start is when you run it today. Movable appliances may be rescheduled; the others always run at their usual start.</span></div></div>
    <div class="panel" id="basepanel"><h2>Background load</h2>
      ${HH.baseline_import_id ? `<div class="note info">Using the measured profile of meter import <b>${esc(HH.baseline_import_id)}</b> (set on <a href="#/usage">My usage</a>). The value below is only used without it.</div>` : '<p class="hint">Best: import your meter readings on <a href="#/usage">My usage</a> and use them as the baseline. Otherwise enter your measured always-on load.</p>'}
      <div class="field"><label for="base">Always-on load (kW)</label><input id="base" type="number" step="0.01" min="0" max="5" value="${val(HH.base_load_kw)}" placeholder="measured, e.g. night-time meter reading"></div>
      <div id="bsrcwrap">${Sources.srcFields('bsrc', HH.base_load_source || null, 'base_load')}</div></div>
    <div class="note" id="hdirty" role="status" hidden>Unsaved changes: press <b>Save household settings</b> or they are lost on reload.</div>
    <button type="button" class="btn primary" id="hsave" style="width:100%;padding:12px">Save household settings</button>
    <div class="row" style="margin-top:8px"><button type="button" class="btn" id="hreset" style="flex:1">Clear configuration</button>
      <button type="button" class="btn" id="hrestore" style="flex:1" ${st.backup?.available ? '' : 'disabled'}>Restore backup${st.backup?.updated_at ? ` (${esc(String(st.backup.updated_at).slice(0, 16))})` : ''}</button></div>
    <p class="hint">Saved on the server (available in every browser on this installation). Clearing keeps a backup you can restore. <a href="/api/inputs/export.json" download>Export all inputs with their sources</a></p></div></div>`;
  renderPeriods(); renderTiers(); validate();
  api('GET', '/api/audit').then((a) => { if ($('#audit')) $('#audit').innerHTML = auditBox(a); }).catch(() => { /* the audit is advisory */ });
  const showEv = () => { const off = $('#noev').checked; $('#evfields').disabled = off; $('#evfields').style.opacity = off ? '.45' : '1'; };
  $('#noev').addEventListener('change', showEv); showEv();
  $('#addtier').addEventListener('click', () => { editing = { ...editing, ...readTariff(), tiers: [...readTiers(), { up_to_kwh: null, adder_per_kwh: null }] }; renderTiers(); validate(); });
  $('#tiers').addEventListener('click', (e) => { const b = e.target.closest('[data-tdel]'); if (!b) return; const t = readTiers(); t.splice(+b.dataset.tdel, 1); editing = { ...editing, ...readTariff(), tiers: t }; renderTiers(); validate(); });
  $('#tadv').addEventListener('input', validate); $('#tadv').addEventListener('change', validate);
  $('#tsel').addEventListener('change', async () => {
    const v = $('#tsel').value;
    if (v === '__new') return viewHouseholdNewTariff();
    HH.tariff_id = v || null;
    try { await saveHousehold(); } catch (e) { toast(e.message); }
    viewHousehold();
  });
  $('#addp').addEventListener('click', () => { editing = { ...editing, ...readTariff() }; editing.periods.push({ label: '', start: 0, end: 24, price: null }); renderPeriods(); validate(); });
  $('#periods').addEventListener('click', (e) => { const b = e.target.closest('[data-del]'); if (!b) return; editing = { ...editing, ...readTariff() }; editing.periods.splice(+b.dataset.del, 1); renderPeriods(); validate(); });
  $('#periods').addEventListener('input', validate); $('#tdemand').addEventListener('input', validate); $('#tname').addEventListener('input', validate);
  const saveTariff = async (asNew) => {
    $('#err').innerHTML = '';
    try {
      const t = readTariff();
      const saved = asNew ? await api('POST', '/api/tariffs', t) : await api('PUT', `/api/tariffs/${encodeURIComponent(HH.tariff_id)}`, t);
      await refreshTariffs(); HH.tariff_id = saved.id; await saveHousehold(); toast(asNew ? 'Tariff added' : 'Tariff saved', true); viewHousehold();
    } catch (e) { $('#err').innerHTML = errorsBox(e); window.scrollTo(0, 0); }
  };
  $('#tsave')?.addEventListener('click', () => saveTariff(false)); $('#tnew').addEventListener('click', () => saveTariff(true));
  $('#tdel')?.addEventListener('click', async () => {
    if (!confirm(`Delete the tariff "${editing.name}"? Experiments that already used it keep their own copy.`)) return;
    try { await api('DELETE', `/api/tariffs/${encodeURIComponent(HH.tariff_id)}`); HH.tariff_id = null; await refreshTariffs(); await saveHousehold(); toast('Tariff deleted', true); viewHousehold(); } catch (e) { $('#err').innerHTML = errorsBox(e); window.scrollTo(0, 0); }
  });
  const readApps = () => $$('#apps tr[data-a]').map((tr) => {
    const i = tr.dataset.a, start = $('[data-f=fixed_start]', tr).value;
    const a = { name: $('[data-f=name]', tr).value, power_kw: parseFloat($('[data-f=power_kw]', tr).value), duration_h: parseFloat($('[data-f=duration_h]', tr).value),
      flexible: $('[data-f=flexible]', tr).checked, fixed_start: start ? toAbs(toHours(start)) : null, source: Sources.readSrc(`asrc-${i}`) };
    const prev = appList[+i] || {};
    if (prev.id) a.id = prev.id;
    if (prev.in_baseline) a.in_baseline = prev.in_baseline;
    return a;
  });
  // Household edits are stored when you press Save (the server validates first); until then the form shows it is unsaved
  let dirty = false;
  const markDirty = (on) => { dirty = on; $('#hdirty').hidden = !on; };
  ['#evpanel', '#apppanel', '#basepanel'].forEach((sel) => { $(sel).addEventListener('input', () => markDirty(true)); $(sel).addEventListener('change', () => markDirty(true)); });
  window.onbeforeunload = () => (dirty && $('#hsave') ? 'Unsaved household changes' : undefined);
  $('#adda').addEventListener('click', () => {
    appList = readApps();
    if (appList.length >= 6) return toast('At most six appliances are supported');
    appList.push({ name: '', power_kw: null, duration_h: null, flexible: true, fixed_start: null, source: null });
    $('#apps').innerHTML = apps(); markDirty(true);
  });
  // adopting an example copy: the value comes back into the form with NO source, so the owner must say where it comes from
  $$('[data-adopt]').forEach((b) => b.addEventListener('click', () => {
    const x = st.example_values[+b.dataset.adopt], blank = { source_type: 'example' };
    if (x.input === 'base_load') { $('#base').value = x.values.base_load_kw; $('#bsrcwrap').innerHTML = Sources.srcFields('bsrc', blank, 'base_load'); }
    else { appList = readApps(); appList.push({ ...x.values, source: blank }); $('#apps').innerHTML = apps(); }
    b.disabled = true; markDirty(true);
  }));
  $('#apps').addEventListener('click', (e) => { const b = e.target.closest('[data-adel]'); if (!b) return; appList = readApps(); appList.splice(+b.dataset.adel, 1); $('#apps').innerHTML = apps(); markDirty(true); });
  const saveAll = async () => {
    $('#err').innerHTML = '';
    const noEv = $('#noev').checked, base = optVal('#base'), charger = optVal('#ev-charger');
    const next = { tariff_id: HH.tariff_id || null, baseline_import_id: HH.baseline_import_id || null, appliances: readApps(),
      base_load_kw: base, base_load_source: base === null ? null : Sources.readSrc('bsrc'),
      ev: noEv ? null : { battery_kwh: optVal('#ev-bat'), efficiency: optVal('#ev-eff'), initial_soc_pct: optVal('#ev-init'), desired_charge_pct: optVal('#ev-want'),
        plug_in_hour: $('#ev-plug').value ? toHours($('#ev-plug').value) : null, departure_hour: $('#ev-dep').value ? toHours($('#ev-dep').value) : null,
        source: Sources.readSrc('evsrc'), ...(HH.ev?.in_baseline ? { in_baseline: true } : {}) },
      charger_power_kw: noEv ? null : charger, charger_source: noEv || charger === null ? null : Sources.readSrc('chsrc') };
    try { await saveHousehold(next); markDirty(false); toast('Household settings saved', true); viewHousehold(); } // validated and stored by the server
    catch (e) { markDirty(true); $('#err').innerHTML = errorsBox(e); window.scrollTo(0, 0); }
  };
  $('#hsave').addEventListener('click', () => saveAll());
  $('#hreset').addEventListener('click', async () => {
    if (!confirm('Clear all household settings? Nothing is filled in afterwards; the current settings are kept as a backup you can restore.')) return;
    try { HSTATE = await api('POST', '/api/household/reset'); HH = HSTATE.settings; markDirty(false); toast('Household cleared (backup kept)', true); viewHousehold(); } catch (e) { $('#err').innerHTML = errorsBox(e); }
  });
  $('#hrestore').addEventListener('click', async () => {
    if (!confirm('Replace the current household settings with the backup?')) return;
    try { HSTATE = await api('POST', '/api/household/restore'); HH = HSTATE.settings; markDirty(false); toast('Backup restored', true); viewHousehold(); } catch (e) { $('#err').innerHTML = errorsBox(e); }
  });
}
// "+ New tariff": the same page with an empty tariff form (the household's tariff choice is not saved until a tariff is)
async function viewHouseholdNewTariff() {
  const keep = HH.tariff_id;
  HH.tariff_id = null;
  await viewHousehold();
  HH.tariff_id = keep;
}
