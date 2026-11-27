/* WATTWISE LAB: "My usage" = historical meter-data library, measured baseline, and actual-vs-optimised comparison.
   Loaded before app.js; uses its helpers (api, esc, fmt, money, kpi, errorsBox, toast, $, $$, view, HH, META, saveHousehold) at call time. */
const PROV = {
  historical_measured: ['MEASURED', 'prov-m'], historical_measured_priced: ['MEASURED × TARIFF', 'prov-mt'], owner_entered_base_load: ['OWNER-ENTERED', 'prov-s'],
  projected: ['PROJECTED', 'prov-p'], measured_schedule: ['MEASURED SCHEDULE', 'prov-ms'],
};
const provChip = (p) => { const [t, c] = PROV[p] || [p, '']; return `<span class="chip ${c}" title="${esc(p)}">${esc(t)}</span>`; };
// One badge per file label, from /api/meta (META.provenance_badges); MEASURED only for a real_meter file. A meter export is the
// household's own data: it is never shown as independently verified.
const FALLBACK_BADGES = { real_meter: { text: 'METER EXPORT · DECLARED BY YOU', css: 'prov-m' }, synthetic_demo: { text: 'SYNTHETIC · NOT USABLE', css: 'prov-s' }, unknown: { text: 'UNVERIFIED SOURCE', css: 'prov-u' } };
const labelBadge = (label) => {
  const l = label || 'unknown', b = ((typeof META !== 'undefined' && META && META.provenance_badges) || FALLBACK_BADGES)[l] || FALLBACK_BADGES.unknown;
  return `<span class="chip ${esc(b.css)}" title="${esc(l)}">${esc(b.text)}</span>`;
};
const pv = (v, d = 2) => (!v ? '&mdash;' : `${v.value === null || v.value === undefined ? '<span class="dim">not estimated</span>' : esc(fmt(v.value, d))} ${esc(v.unit || '')} ${provChip(v.provenance)}${v.note ? `<div class="hint">${esc(v.note)}</div>` : ''}`);

function importOptions() {
  const o = {};
  const val = (id) => $(id).value.trim();
  if (val('#u-tscol')) o.timestamp_column = val('#u-tscol');
  if (val('#u-valcol')) o.value_column = val('#u-valcol');
  if (val('#u-kind')) o.reading_kind = val('#u-kind');
  if (val('#u-unit')) o.unit = val('#u-unit');
  o.timezone = val('#u-tz') || 'UTC';
  o.timestamp_marks = val('#u-marks');
  o.decimal = val('#u-dec');
  if (val('#u-delim')) o.delimiter = val('#u-delim');
  if (val('#u-fmt')) o.timestamp_format = val('#u-fmt');
  if (val('#u-resample')) o.resample_minutes = parseFloat(val('#u-resample'));
  if (val('#u-roll')) o.rollover_kwh = parseFloat(val('#u-roll'));
  return o;
}

function previewHtml(p, label) {
  const s = p.summary, it = p.interpretation;
  const rows = p.readings_preview.map((r) => `<tr><td class="num">${r.source_row}</td><td class="mono">${esc(r.original_ts)}</td><td class="mono">${esc(r.start_utc.replace('+00:00', 'Z'))}</td>
    <td class="num">${fmt(r.interval_minutes, 0)}</td><td class="num">${fmt(r.kwh, 4)}</td><td class="num">${fmt(r.kw, 3)}</td><td>${esc((r.flags || []).join(', '))}</td></tr>`).join('');
  const log = p.row_log.map((l) => `<tr><td class="num">${l.line ?? ''}</td><td>${esc(l.action)}</td><td>${esc(l.reason)}</td></tr>`).join('');
  const gaps = p.gaps.map((g) => `<tr><td class="mono">${esc(g.from_utc)}</td><td class="mono">${esc(g.to_utc)}</td><td class="num">${g.missing_intervals}</td><td>${esc(g.note || '')}</td></tr>`).join('');
  return `<div class="panel"><h2>How this file will be read ${labelBadge(label)}</h2>
    <p>Source columns: ${p.columns.map((c) => `<code>${esc(c)}</code>`).join(' ')}. Timestamp <code>${esc(it.timestamp_column)}</code>, value <code>${esc(it.value_column)}</code>
      read as <b>${esc(it.reading_kind)}</b> in <b>${esc(it.unit)}</b>; delimiter <code>${esc(it.delimiter === '\t' ? 'tab' : it.delimiter)}</code>; time zone <b>${esc(p.options.timezone)}</b>.</p>
    <div class="kpis">${kpi(`${s.rows_used} / ${s.rows_total}`, 'rows used')}${kpi(String(s.rows_skipped), 'rows skipped')}${kpi(fmt(s.interval_minutes, 0) + ' min', 'interval')}
      ${kpi(fmt(s.energy_kwh, 1) + ' kWh', 'energy')}${kpi(String(s.missing_intervals), 'missing intervals', '', `${s.gaps} gap(s)`)}</div>
    ${(p.warnings || []).length ? `<ul class="warnlist">${p.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    <h3>Converted readings (first ${p.readings_preview.length})</h3><div class="scroll"><table><thead><tr><th>Line</th><th>Original timestamp</th><th>Interval start (UTC)</th><th>Min</th><th>kWh</th><th>Avg kW</th><th>Flags</th></tr></thead><tbody>${rows}</tbody></table></div>
    <h3>Skipped and transformed rows (${p.row_log.length})</h3>${log ? `<div class="scroll"><table><thead><tr><th>Line</th><th>Action</th><th>Reason</th></tr></thead><tbody>${log}</tbody></table></div>` : '<p class="dim">None: every row was used as-is.</p>'}
    ${gaps ? `<h3>Gaps (not filled)</h3><table><thead><tr><th>From</th><th>To</th><th>Missing</th><th></th></tr></thead><tbody>${gaps}</tbody></table>` : ''}
    <button class="btn primary" id="u-import">Import into my usage library</button></div>`;
}

async function viewUsage() {
  view().innerHTML = `<h1>My usage</h1><p class="sub">Your <b>measured</b> historical electricity readings. They are kept separate from experiment runs: past usage is not the outcome of a schedule you did not actually run.</p><div id="err"></div>
  <div class="grid g21"><div class="panel"><h2>Import a meter export</h2>
    <div class="field"><label for="u-file">File</label><input id="u-file" type="file" accept=".csv,.txt,text/csv"></div>
    <div class="field"><label for="u-csv">or paste CSV text</label><textarea id="u-csv" spellcheck="false" placeholder="timestamp,power_kw&#10;2026-03-03T16:00:00,0.40"></textarea></div>
    <p class="hint">Download your readings from your utility's consumer portal or smart-meter app (with your consent, they are your data). No demo data is provided.</p>
    ${Sources.srcFields('msrc', null, 'meter')}</div>
  <div class="panel"><h2>How to read it</h2><div class="grid g2">
    <div class="field"><label for="u-tscol">Timestamp column</label><input id="u-tscol" placeholder="auto"></div>
    <div class="field"><label for="u-valcol">Value column</label><input id="u-valcol" placeholder="auto"></div>
    <div class="field"><label for="u-kind">Readings are</label><select id="u-kind"><option value="">auto</option><option value="power">average power</option><option value="interval_energy">energy per interval</option><option value="cumulative">cumulative meter register</option></select></div>
    <div class="field"><label for="u-unit">Unit</label><select id="u-unit"><option value="">auto</option><option>kW</option><option>W</option><option>kWh</option><option>Wh</option></select></div>
    <div class="field"><label for="u-tz">Time zone of timestamps without an offset</label><input id="u-tz" value="UTC" placeholder="e.g. Europe/London"></div>
    <div class="field"><label for="u-marks">Timestamp marks the interval</label><select id="u-marks"><option value="start">start</option><option value="end">end</option></select></div>
    <div class="field"><label for="u-dec">Decimal separator</label><select id="u-dec"><option value=".">. (1.5)</option><option value=",">, (1,5)</option></select></div>
    <div class="field"><label for="u-delim">Delimiter</label><select id="u-delim"><option value="">auto</option><option value=",">,</option><option value=";">;</option><option value="\\t">tab</option></select></div>
    <div class="field"><label for="u-fmt">Timestamp format (if not ISO-8601)</label><input id="u-fmt" placeholder="e.g. %d/%m/%Y %H:%M"></div>
    <div class="field"><label for="u-resample">Resample to (minutes)</label><input id="u-resample" type="number" min="1" placeholder="keep"></div>
    <div class="field"><label for="u-roll">Register rollover (kWh)</label><input id="u-roll" type="number" min="1" placeholder="none"></div>
    <div class="field"><label for="u-prov">Provenance of this file</label><select id="u-prov"><option value="real_meter">real meter export</option><option value="unknown">origin unknown (labelled unverified)</option></select></div>
    <div class="field"><label for="u-src">Where the file came from</label><input id="u-src" maxlength="120" placeholder="e.g. DISCOM consumer portal, smart-meter app"></div></div>
    <button class="btn primary" id="u-prev" style="width:100%">Preview interpretation</button></div></div>
  <div id="u-res"></div>
  <div class="panel"><h2>Library</h2><div id="u-lib">Loading&hellip;</div></div><div id="u-detail"></div>`;
  let fileName = 'meter.csv';
  $('#u-file').addEventListener('change', async () => { const f = $('#u-file').files[0]; if (!f) return; fileName = f.name; try { $('#u-csv').value = await readFileText(f); } catch (e) { $('#err').innerHTML = errorsBox(e); } });
  $('#u-prev').addEventListener('click', async () => {
    $('#err').innerHTML = ''; $('#u-res').innerHTML = '';
    try {
      const p = await api('POST', '/api/meter/import/preview', { csv: $('#u-csv').value, file_name: fileName, options: importOptions() });
      $('#u-res').innerHTML = previewHtml(p, $('#u-prov').value);
      $('#u-import').addEventListener('click', async () => {
        try {
          const src = $('#u-src').value.trim(), ms = Sources.readSrc('msrc');
          if (ms && !ms.source_name) ms.source_name = src;
          const m = await api('POST', '/api/meter/import', { csv: $('#u-csv').value, file_name: fileName, source: src, provenance: $('#u-prov').value || null,
            meter_source: ms && ms.source_name ? ms : null, options: importOptions() });
          toast(`Imported ${m.rows_used} readings`, true); $('#u-res').innerHTML = ''; await lib(); await detail(m.import_id);
        } catch (e) { $('#err').innerHTML = errorsBox(e); window.scrollTo(0, 0); }
      });
    } catch (e) { $('#err').innerHTML = errorsBox(e); }
  });
  const lib = async () => {
    const list = await api('GET', '/api/meter/imports');
    $('#u-lib').innerHTML = list.length ? `<table><thead><tr><th>File</th><th>Period (UTC)</th><th class="num">Readings</th><th>Kind</th><th></th></tr></thead><tbody>${list.map((m) => `<tr>
      <td><b>${esc(m.file_name)}</b> ${Sources.badge(m.source_status)}<div class="dim">${esc(m.source)} &middot; ${esc(m.import_id)}${HH.baseline_import_id === m.import_id ? ' &middot; <b>baseline</b>' : ''}</div></td>
      <td class="mono">${esc((m.first_utc || '').slice(0, 16))} &rarr; ${esc((m.last_utc || '').slice(0, 16))}</td><td class="num">${m.rows_used}</td><td>${esc(m.reading_kind)} (${esc(m.unit)})</td>
      <td><button class="btn small" data-open="${esc(m.import_id)}">Open</button> <button class="btn small" data-del="${esc(m.import_id)}">Delete</button></td></tr>`).join('')}</tbody></table>`
      : '<div class="empty">No meter data imported yet.</div>';
    $$('[data-open]').forEach((b) => b.addEventListener('click', () => detail(b.dataset.open)));
    $$('[data-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Delete this import and all its readings?')) return;
      try { await api('DELETE', `/api/meter/imports/${b.dataset.del}`); if (HH.baseline_import_id === b.dataset.del) await saveHousehold({ ...HH, baseline_import_id: null }); $('#u-detail').innerHTML = ''; await lib(); } catch (e) { toast(e.message); }
    }));
  };
  const detail = async (id) => {
    const s = await api('GET', `/api/meter/imports/${id}/summary${HH.tariff_id ? `?tariff_id=${encodeURIComponent(HH.tariff_id)}` : ''}`);
    const exps = await api('GET', '/api/experiments');
    const c = s.baseline_cost, d = s.daily_energy_kwh;
    const hours = (s.hourly_profile_kw || []).map((v, h) => ({ hour: h, kw: v ?? 0 }));
    $('#u-detail').innerHTML = `<div class="panel"><h2>${esc(s.import.file_name)} ${labelBadge(s.source_label)}</h2>${(s.warnings || []).length ? `<ul class="warnlist">${s.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
      <div class="kpis">${kpi(fmt(d.mean, 1) + ' kWh', 'mean daily energy', '', `sd ${fmt(d.std, 1)} · min ${fmt(d.min, 1)} · max ${fmt(d.max, 1)}`)}${kpi(fmt(s.peak_demand.kw, 2) + ' kW', 'peak demand', '', s.peak_demand.note)}
        ${kpi(`${s.coverage.days_complete} / ${s.coverage.days_total}`, 'complete days', '', `${fmt(100 * s.coverage.mean_coverage, 0)}% mean coverage`)}
        ${c ? kpi(money(c.total), `cost under ${c.tariff_name}`, '', 'measured usage × tariff: check against your bill') : ''}</div>
      <h3>Average hourly load (complete days)</h3>${Charts.hourly({ profile: hours.map((x) => ({ hour: x.hour, kw: x.kw })), tariff: tariffOf(HH.tariff_id) })}
      <p class="hint">Weekdays: ${s.weekday_days} day(s), weekends: ${s.weekend_days} day(s).</p>
      <div class="row"><button class="btn" id="u-base">${HH.baseline_import_id === id ? 'Stop using as baseline' : 'Use as my household baseline'}</button>
        <span class="dim">New experiments then start from this measured background load instead of the always-on load you entered.</span></div></div>
      <div class="panel"><h2>My actual electricity usage vs optimised schedule</h2>
        <div class="field"><label for="u-exp">Experiment</label><select id="u-exp">${exps.map((e) => `<option value="${esc(e.id)}">${esc(e.name)} (${esc(e.id)})</option>`).join('') || '<option value="">no experiments yet</option>'}</select></div>
        <button class="btn primary" id="u-cmp">Compare</button><div id="u-cmpres"></div></div>
      <div class="panel"><h2>Reconcile with my bill</h2><p class="hint">Copy one billing period from your real bill. WATTWISE prices this file's readings for that period with your tariff and compares the units and the amount. It passes only if the readings cover the whole period and both differ by at most the tolerance.</p>
        <div class="grid g2"><div class="field"><label for="r-from">Billing period from</label><input id="r-from" type="date"></div><div class="field"><label for="r-to">to (inclusive)</label><input id="r-to" type="date"></div>
          <div class="field"><label for="r-kwh">Units billed (kWh)</label><input id="r-kwh" type="number" step="0.01" min="0"></div><div class="field"><label for="r-amt">Amount for this period (before arrears)</label><input id="r-amt" type="number" step="0.01" min="0"></div>
          <div class="field"><label for="r-tol">Tolerance (%)</label><input id="r-tol" type="number" step="0.1" min="0.1" max="20" value="2"></div></div>
        ${Sources.srcFields('rsrc', { source_type: 'user_provided', source_name: 'household owner', verification: 'user_supplied' }, 'tariff')}
        <button class="btn primary" id="r-go" ${HH.tariff_id ? '' : 'disabled'}>Reconcile</button>${HH.tariff_id ? '' : ' <span class="hint">Select your tariff in Household setup first.</span>'}<div id="r-res"></div></div>`;
    $('#u-base').addEventListener('click', async () => {
      try { await saveHousehold({ ...HH, baseline_import_id: HH.baseline_import_id === id ? null : id }); toast('Household baseline updated', true); await lib(); await detail(id); } catch (e) { toast(e.message); }
    });
    $('#r-go').addEventListener('click', async () => {
      try {
        const r = await api('POST', '/api/reconcile', { import_id: id, tariff_id: HH.tariff_id, period_start: $('#r-from').value, period_end: $('#r-to').value,
          billed_kwh: parseFloat($('#r-kwh').value), billed_amount: parseFloat($('#r-amt').value), tolerance_pct: parseFloat($('#r-tol').value) || 2, bill_source: Sources.readSrc('rsrc') });
        $('#r-res').innerHTML = `<div class="${r.passed ? 'note info' : 'errors'}"><b>${r.passed ? 'Reconciled' : 'Does not reconcile'}</b>: priced ${esc(fmt(r.computed.kwh, 2))} kWh / ${esc(money(r.computed.amount))}
          vs bill ${esc(fmt(r.billed.kwh, 2))} kWh / ${esc(money(r.billed.amount))} (${esc(fmt(r.difference_pct.kwh, 2))}% / ${esc(fmt(r.difference_pct.amount, 2))}%), coverage ${esc(fmt(100 * r.coverage, 1))}%
          ${r.reasons.length ? `<ul>${r.reasons.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}<div class="hint">energy ${esc(money(r.computed.energy_charge))} &middot; fixed ${esc(money(r.computed.fixed_charge))} &middot; demand ${esc(money(r.computed.demand_charge))} &middot; tax ${esc(money(r.computed.tax))}</div></div>`;
      } catch (e) { $('#r-res').innerHTML = errorsBox(e); }
    });
    $('#u-cmp').addEventListener('click', async () => {
      const exp = $('#u-exp').value; if (!exp) return;
      try { $('#u-cmpres').innerHTML = comparisonHtml(await api('GET', `/api/comparison?import_id=${id}&exp_id=${encodeURIComponent(exp)}`), id, exp); } catch (e) { $('#u-cmpres').innerHTML = errorsBox(e); }
    });
  };
  await lib();
  if (HH.baseline_import_id) { try { await detail(HH.baseline_import_id); } catch (_) { /* deleted */ } }
}

function comparisonHtml(c, id, exp) {
  const h = c.historical, s = c.schedules;
  const sched = [['Current schedule', s.current], ['Best projected schedule', s.best_projected], ...s.measured.map((m) => [`Measured run ${m.run_id}`, m])].filter(([, v]) => v)
    .map(([n, v]) => `<tr><th>${esc(n)}<div class="hint">${esc(v.schedule)}</div></th><td>${pv(v.cost)}</td><td>${pv(v.kwh, 1)}</td><td>${pv(v.peak_kw, 2)}</td><td>${pv(v.ev_charge_at_departure_pct, 0)}</td></tr>`).join('');
  const per = Object.entries(c.cost_by_period).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${pv(v.historical_per_day)}</td><td>${pv(v.projected_best)}</td></tr>`).join('');
  const sav = Object.entries(c.savings).map(([k, v]) => `<tr><th>${esc(k.replaceAll('_', ' '))}</th><td>${pv(v)}</td></tr>`).join('');
  return `<p class="hint">Background load in the projections: <b>${esc(c.experiment.baseline)}</b>${c.experiment.calibrated_from_import ? ` (from ${esc(c.experiment.calibrated_from_import)})` : ''}.</p>
    <table><tbody><tr><th>Mean daily energy</th><td>${pv(h.mean_daily_kwh, 1)}</td></tr><tr><th>Mean daily cost</th><td>${pv(h.mean_daily_cost)}</td></tr><tr><th>Peak demand</th><td>${pv(h.peak_kw)}</td></tr></tbody></table>
    <table><thead><tr><th></th><th>Cost / day</th><th>Energy</th><th>Peak</th><th>EV at departure</th></tr></thead><tbody>${sched}</tbody></table>
    <h3>Cost by tariff period</h3><table><thead><tr><th></th><th>Historical per day</th><th>Best projected</th></tr></thead><tbody>${per}</tbody></table>
    <h3>Estimated savings</h3><table><tbody>${sav}</tbody></table>
    <h3>Assumptions</h3><ul>${c.assumptions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>
    <a class="btn" href="/api/comparison/report.html?import_id=${encodeURIComponent(id)}&exp_id=${encodeURIComponent(exp)}" target="_blank" rel="noopener">Open printable report</a>`;
}
