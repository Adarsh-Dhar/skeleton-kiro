/* "Choose a real Formula 1 session and import it" component of the Historical F1 page. Everything is loaded from the API: seasons
   run from OpenF1's first season to the current one, and the Grand Prix / session / driver lists come from
   /api/historical/events and /api/historical/drivers, never from constants. All dynamic text is escaped with Charts.esc(). */
const HistPicker = (() => {
  const esc = Charts.esc;
  const FIRST_YEAR = 2023;  // OpenF1 historical data starts with the 2023 season
  const RACE_ONLY = ['starting_grid', 'intervals,overtakes'];
  /* [element id, label, datasets it stands for (OpenF1 optional datasets), race/sprint only?] */
  const DATASET_CHOICES = [
    ['dsresult', 'Race results', 'session_result'], ['dspos', 'Running position', 'race_position'], ['dsgrid', 'Starting grid', 'starting_grid'],
    ['dsstrat', 'Tyres & pit stops', 'stints,pit'], ['dsgaps', 'Overtakes & intervals', 'intervals,overtakes'], ['dsrc', 'Race control', 'race_control'],
    ['dsradio', 'Team radio', 'team_radio']
  ];
  const TERMINAL = ['COMPLETED', 'FAILED', 'CANCELLED'];
  const $ = (s, r = document) => r.querySelector(s);
  const cb = (id, label, extra = '', checked = true) => `<label style="text-transform:none;letter-spacing:0" ${extra}><input type="checkbox" id="${id}" ${checked ? 'checked' : ''}> ${label}</label>`;

  function mount(root, opts = {}) {
    const api = opts.api, errorsBox = opts.errorsBox, toast = opts.toast;
    const year0 = new Date().getFullYear();
    root.innerHTML = `<div class="panel" id="hist-form"><h2>Choose a real session</h2><div id="hist-err"></div>
      <div class="field"><label for="hprov">Data provider</label><select id="hprov"><option value="openf1" selected>OpenF1</option><option value="csv_upload">CSV Upload</option></select></div>
      <div id="hopenf1"><div class="grid g2">
        <div class="field"><label for="hyear">Season</label><select id="hyear"></select></div>
        <div class="field"><label for="hevent">Grand Prix</label><select id="hevent"><option>Loading&hellip;</option></select></div>
        <div class="field"><label for="hsession">Session</label><select id="hsession"><option>&mdash;</option></select></div></div>
      <div class="field"><label for="hdrvsearch">Driver / car</label>
        <div class="row" style="gap:8px"><input id="hdrvsearch" type="search" placeholder="Search by name, code, number or team" aria-label="Search drivers" autocomplete="off" style="flex:1">
          <button class="btn small" id="hdrvall" type="button">All</button><button class="btn small" id="hdrvnone" type="button">None</button></div>
        <div id="hdrivers" style="max-height:230px;overflow:auto;margin-top:6px"><span class="dim">Pick a session to load its drivers.</span></div>
        <div class="hint" id="hdrvsum"></div></div>
      <div class="field"><label>Data to import</label><div class="row" style="gap:14px;flex-wrap:wrap">
        ${cb('hlaps', 'Lap times (required)', '', true).replace('<input ', '<input disabled ')}${cb('htele', 'Car telemetry')}${cb('hpos', 'Track location (x/y)')}${cb('hwx', 'Weather')}</div>
        <div class="row" style="gap:14px;flex-wrap:wrap;margin-top:6px">${DATASET_CHOICES.map(([id, label, ds]) => cb(id, label, `data-ds="${ds}"`)).join('')}</div>
        <div class="hint" id="hdshint"></div></div>
      <div class="field">${cb('hfresh', 'download again, ignoring the OpenF1 cache', '', false)}</div>
      <button class="btn primary" id="himport" type="button" disabled>Load selected F1 data</button><span class="dim" id="hmsg"></span><div id="hprog"></div></div>
      <div id="hcsv" hidden></div></div>
      <div id="hopenf1-foot"><div class="note info">Imports real timing data into the local historical library. Results are cached locally; OpenF1 is throttled to 30 requests a minute, so a full field can take several minutes (free, no account, 2023 onwards).</div>
      <p class="hint" id="hcache"></p></div>`;

    let events = [], drivers = [], evSeq = 0, drvSeq = 0, timer = null;
    const ev = () => events[+$('#hevent', root).value];
    const sess = () => (ev()?.sessions || []).find((s) => s.type === $('#hsession', root).value);
    const err = (e) => { $('#hist-err', root).innerHTML = e ? errorsBox(e) : ''; };

    const fillYears = () => {
      const y = $('#hyear', root);
      y.innerHTML = Array.from({ length: year0 - FIRST_YEAR + 1 }, (_, i) => year0 - i).map((v) => `<option value="${v}">${v}</option>`).join('');
    };
    const syncChannels = () => {  // grid, gaps, and overtakes exist for race and sprint sessions only
      const s = sess(), raceLike = !s || ['R', 'S'].includes(s.type);
      root.querySelectorAll('[data-ds]').forEach((l) => {
        const only = RACE_ONLY.includes(l.dataset.ds), input = l.querySelector('input');
        input.disabled = only && !raceLike;
        l.title = input.disabled ? 'OpenF1 publishes this for race and sprint sessions only' : '';
      });
      $('#hdshint', root).textContent = raceLike ? '' : 'Starting grid, overtakes and intervals are published by OpenF1 for race and sprint sessions only.';
    };
    const fillSessions = () => {
      const e = ev();
      $('#hsession', root).innerHTML = (e?.sessions || []).filter((s) => s.type).map((s) => `<option value="${esc(s.type)}">${esc(s.name || s.type)} (${esc(s.type)})</option>`).join('') || '<option value="">no sessions</option>';
      // prefer the race, the most natural session to analyse
      const race = [...$('#hsession', root).options].find((o) => o.value === 'R');
      if (race) race.selected = true;
      syncChannels();
    };
    const fillEvents = () => {
      $('#hevent', root).innerHTML = events.map((e, i) => `<option value="${i}">${e.round ? `R${esc(e.round)} · ` : ''}${esc(e.event)}${e.location ? ` (${esc(e.location)})` : ''}</option>`).join('') || '<option value="">No events published</option>';
      // the latest Grand Prix that has already started (a published but future event has no data yet)
      const now = Date.now(), held = events.map((e, i) => [e, i]).filter(([e]) => (e.sessions || []).some((s) => s.date && Date.parse(s.date) <= now));
      if (events.length) $('#hevent', root).selectedIndex = held.length ? held[held.length - 1][1] : events.length - 1;
      fillSessions();
    };

    /* ------------------------------------------------------------- drivers */
    const visibleDrivers = () => {
      const q = $('#hdrvsearch', root).value.trim().toLowerCase();
      return drivers.filter((d) => !q || [d.code, d.number, d.name, d.team].some((v) => String(v ?? '').toLowerCase().includes(q)));
    };
    const checkedCodes = () => [...root.querySelectorAll('.hdrv')].filter((c) => c.checked).map((c) => c.value);
    const summary = () => {
      const n = drivers.filter((d) => selected.has(d.code)).length;
      $('#hdrvsum', root).textContent = drivers.length ? (n === drivers.length ? `All ${n} drivers selected (the whole field).` : `${n} of ${drivers.length} drivers selected.`) : '';
      $('#himport', root).disabled = !drivers.length || n === 0 || !ev();
    };
    let selected = new Set();
    const drawDrivers = () => {
      const list = visibleDrivers();
      $('#hdrivers', root).innerHTML = drivers.length ? (list.map((d) => `<label style="display:flex;gap:8px;align-items:center;text-transform:none;letter-spacing:0;padding:2px 0">
        <input type="checkbox" class="hdrv" value="${esc(d.code)}" ${selected.has(d.code) ? 'checked' : ''}> <span class="mono"><b>${esc(d.code)}</b> #${esc(d.number)}</span> ${esc(d.name)} <span class="dim">${esc(d.team || '')}</span></label>`).join('')
        || '<span class="dim">No driver matches the search.</span>') : '<span class="dim">No drivers returned for this session.</span>';
      root.querySelectorAll('.hdrv').forEach((c) => c.addEventListener('change', () => { if (c.checked) selected.add(c.value); else selected.delete(c.value); summary(); }));
      summary();
    };
    const loadDrivers = async () => {
      const seq = ++drvSeq, e = ev(), s = $('#hsession', root).value;
      drivers = []; selected = new Set(); drawDrivers();
      if (!e || !s) return;
      $('#hdrivers', root).innerHTML = '<span class="dim">Loading drivers&hellip;</span>';
      try {
        const ds = await api('GET', `/api/historical/drivers?year=${+$('#hyear', root).value}&event=${encodeURIComponent(e.event)}&session=${encodeURIComponent(s)}`);
        if (seq !== drvSeq) return;  // a newer selection superseded this answer
        drivers = ds; selected = new Set(ds.map((d) => d.code)); drawDrivers(); err(null);
      } catch (x) {
        if (seq !== drvSeq) return;
        $('#hdrivers', root).innerHTML = `<span class="badc">${esc(x.message)}</span>`;
      }
    };
    const loadEvents = async (stepBack = 0) => {
      const seq = ++evSeq, y = +$('#hyear', root).value;
      events = []; fillEvents(); drivers = []; selected = new Set(); drawDrivers();
      $('#hevent', root).innerHTML = '<option>Loading&hellip;</option>';
      try {
        const list = await api('GET', `/api/historical/events?year=${y}`);
        if (seq !== evSeq) return;
        events = list.filter((e) => (e.sessions || []).length);
        if (!events.length && stepBack < 2 && y - 1 >= FIRST_YEAR && opts.autoLatest !== false && !root.dataset.userYear) {
          $('#hyear', root).value = String(y - 1);  // the newest season may not have a published event yet
          return loadEvents(stepBack + 1);
        }
        err(null); fillEvents(); await loadDrivers();
      } catch (x) { if (seq === evSeq) { events = []; fillEvents(); err(x); } }
    };

    root.querySelector('#hyear').addEventListener('change', () => { root.dataset.userYear = '1'; loadEvents(); });
    root.querySelector('#hevent').addEventListener('change', () => { fillSessions(); loadDrivers(); });
    root.querySelector('#hsession').addEventListener('change', () => { syncChannels(); loadDrivers(); });
    root.querySelector('#hdrvsearch').addEventListener('input', drawDrivers);
    root.querySelector('#hdrvall').addEventListener('click', () => { visibleDrivers().forEach((d) => selected.add(d.code)); drawDrivers(); });
    root.querySelector('#hdrvnone').addEventListener('click', () => { visibleDrivers().forEach((d) => selected.delete(d.code)); drawDrivers(); });

    /* ------------------------------------------------------------- import + progress */
    const request = () => {
      const e = ev();
      const codes = [...selected];
      const body = { provider: 'openf1', year: +$('#hyear', root).value, event: e.event, session: $('#hsession', root).value,
        drivers: codes.length === drivers.length ? [] : codes,  // empty list = the whole field
        include_telemetry: $('#htele', root).checked, include_position: $('#hpos', root).checked, include_weather: $('#hwx', root).checked };
      const ds = [];
      root.querySelectorAll('[data-ds] input').forEach((i) => { if (i.checked && !i.disabled) ds.push(...i.closest('[data-ds]').dataset.ds.split(',')); });
      body.race_datasets = ds;
      body.include_race_data = ds.some((n) => !['stints', 'pit', 'race_control'].includes(n));
      body.refresh_cache = $('#hfresh', root).checked;
      return body;
    };
    const coverageLines = (m) => Object.entries(m.coverage?.endpoints || {}).map(([k, v]) => `${esc(k)}: ${esc(String(v.status).replace('_', ' '))}${v.rows ? ` (${esc(v.rows)})` : ''}`).join(' &middot; ');
    /* The picker belongs to one page. Once the user has left it (the root is no longer in the document, or the hash
       changed), polling stops and a finished import never pulls the user away from where they are now. */
    const mountedHash = location.hash;
    const onPage = () => root.isConnected && location.hash === mountedHash;
    const track = (jid) => {
      clearTimeout(timer);
      const tick = async () => {
        if (!onPage()) return;
        let j;
        try { j = await api('GET', `/api/historical/imports/${jid}`); } catch (e) { $('#hprog', root).innerHTML = errorsBox(e); return; }
        const active = !TERMINAL.includes(j.state);
        $('#hprog', root).innerHTML = `<div class="note ${j.state === 'FAILED' ? '' : 'info'}"><b>${esc(j.state)}</b> &middot; ${esc(j.detail || '')}${j.error ? ` &middot; <span class="badc">${esc(j.error_code || '')}: ${esc(j.error)}</span>` : ''}
          ${active ? ' <button class="btn small" id="hcancel" type="button">Cancel</button>' : ''}${['FAILED', 'CANCELLED'].includes(j.state) ? ' <button class="btn small" id="hretry" type="button">Retry</button>' : ''}<div id="hcov" class="hint"></div></div>`;
        $('#hcancel', root)?.addEventListener('click', async () => { try { await api('POST', `/api/historical/imports/${jid}/cancel`); } catch (e) { err(e); } });
        $('#hretry', root)?.addEventListener('click', async () => { try { track((await api('POST', `/api/historical/imports/${jid}/retry`)).import_id); } catch (e) { err(e); } });
        if (j.state === 'COMPLETED') {
          $('#hmsg', root).textContent = 'done';
          let m = null;
          try { m = await api('GET', `/api/historical/sessions/${j.session_id}`); } catch (_) { /* the link below still works */ }
          const href = `#/hist/${encodeURIComponent(j.session_id)}/overview`;
          $('#hcov', root).innerHTML = `${m ? `${coverageLines(m)}${m.partial ? ' &middot; <b>partial</b>' : ''}<br>` : ''}Opening the session&hellip; <a href="${href}">open now</a>`;
          opts.onChange?.();
          if (opts.navigate) setTimeout(() => { if (onPage()) location.hash = href; }, 900);
          return;
        }
        if (!active) { $('#hmsg', root).textContent = j.state.toLowerCase(); opts.onChange?.(); return; }
        if (onPage()) timer = setTimeout(tick, 1500);
      };
      tick();
    };
    $('#himport', root).addEventListener('click', async () => {
      if (!ev() || !selected.size) { err(new Error('pick a Grand Prix, a session and at least one driver')); return; }
      try {
        const r = await api('POST', '/api/historical/import', request());
        err(null);
        $('#hmsg', root).textContent = r.duplicate ? (r.state === 'COMPLETED' ? 'already imported (no download needed)' : 'this import is already running') : 'import started';
        track(r.import_id);
      } catch (e) { err(e); }
    });
    const loadCache = async () => {
      try {
        const c = await api('GET', '/api/historical/openf1/cache');
        $('#hcache', root).innerHTML = `OpenF1 cache: ${esc(c.files)} answers (${esc((c.bytes / 1e6).toFixed(1))} MB), kept ${esc(c.ttl_days)} days${c.newest ? `, newest ${esc(c.newest.replace('T', ' ').slice(0, 16))} UTC` : ''}. <button class="btn small" id="hclear" type="button">Clear cache</button>`;
        $('#hclear', root).addEventListener('click', async () => {
          if (!window.confirm('Delete the cached OpenF1 downloads? Imported sessions are kept; the next import downloads again.')) return;
          try { await api('DELETE', '/api/historical/openf1/cache'); toast('OpenF1 cache cleared', true); loadCache(); } catch (e) { err(e); }
        });
      } catch (_) { $('#hcache', root).textContent = ''; }
    };

    /* ------------------------------------------------------------- provider selector */
    let csv = null;
    $('#hprov', root).addEventListener('change', (e) => {
      const isCsv = e.target.value === 'csv_upload';
      $('#hopenf1', root).hidden = isCsv; $('#hopenf1-foot', root).hidden = isCsv; $('#hcsv', root).hidden = !isCsv;
      err(null);
      if (isCsv && !csv) csv = mountCsv($('#hcsv', root), { api, errorsBox, opts, onPage: () => root.isConnected && location.hash === mountedHash, err });
    });

    fillYears();
    loadEvents();
    loadCache();
    return { track, stop: () => { clearTimeout(timer); csv?.stop(); } };
  }
  /* ================================================================ CSV Upload workflow (Requirement 16)
     Six steps: upload, column mapping, units, metadata, validation and preview, import start. All entered values live in the
     state object `S`, so going back never loses them, and a failed or unreachable import leaves them in place. */
  const CSV_STEPS = ['Upload', 'Column mapping', 'Units', 'Metadata', 'Validation & preview', 'Import'];
  const CSV_FIELDS = ['timestamp', 'session_time_s', 'lap_number', 'distance_m', 'speed_kph', 'throttle_pct', 'brake_pct', 'steer_pct', 'gear', 'rpm', 'drs',
    'lap_time_s', 'sector_1_s', 'sector_2_s', 'sector_3_s', 'sector_4_s', 'driver'];
  const NOT_CHANNELS = ['timestamp', 'session_time_s', 'lap_number', 'lap', 'driver'];
  const CSV_META = [['year', 'Year', 'number'], ['event', 'Grand Prix / event', 'text'], ['circuit', 'Circuit', 'text'], ['session_type', 'Session type', 'text'],
    ['driver', 'Driver', 'text'], ['session_date', 'Session date (YYYY-MM-DD)', 'text'], ['timezone', 'Timezone (IANA, e.g. Europe/London)', 'text']];
  const UNIT_CHOICES = { time: ['s', 'ms'], speed: ['kph', 'mph', 'm/s'], distance: ['m', 'km', 'ft'] };
  const PROGRESS = { QUEUED: 5, DOWNLOADING: 25, PROCESSING: 50, SAVING: 85, COMPLETED: 100 };
  const POLL_MS = 1000;  // Req 16.8: refresh at least every 2 seconds
  const GRAPH_COLORS = ['#1f77b4', '#d62728', '#2ca02c', '#ff7f0e', '#9467bd', '#17becf'];

  function mountCsv(box, ctx) {
    const { api, errorsBox, opts, onPage } = ctx;
    const S = { step: 0, file: null, colField: {}, dataType: '', units: { time: 's', speed: 'kph', distance: 'm' }, tz: '',
      meta: Object.fromEntries(CSV_META.map(([k]) => [k, ''])), unav: new Set(), dup: 'reject', order: 'reject',
      validation: null, validating: false, graphsDrawn: false, uploading: false, notice: '', existing: null };
    let timer = null, valSeq = 0, jobId = null, lastSig = '';
    box.innerHTML = '<div id="hcsteps"></div><div id="hcjob"></div>';
    const steps = $('#hcsteps', box), jobBox = $('#hcjob', box);
    const connMsg = 'Cannot reach the backend (connection failure). Your entries are kept; press the button again to retry.';
    const isConn = (e) => e && e.status === 0;
    const show = (e) => (isConn(e) ? `<div class="errors" role="alert">${esc(connMsg)}</div>` : errorsBox(e));

    const mappingObj = () => { const m = {}; Object.entries(S.colField).forEach(([c, f]) => { if (f) m[f] = c; }); return m; };
    const dupFields = () => { const seen = {}, d = new Set(); Object.values(S.colField).forEach((f) => { if (f) { if (seen[f]) d.add(f); seen[f] = 1; } }); return [...d]; };
    const kind = () => (S.file ? (S.dataType || S.file.detected_type) : '');
    const unitsOk = () => Object.entries(UNIT_CHOICES).every(([k, v]) => v.includes(S.units[k]));
    const metaDone = () => CSV_META.every(([k]) => String(S.meta[k]).trim() !== '' || S.unav.has(k));
    const complete = (i) => {
      if (i === 0) return !!S.file;
      if (i === 1) return !!S.file && !dupFields().length && Object.keys(mappingObj()).length > 0 && ['lap_summary', 'sampled_telemetry'].includes(kind());
      if (i === 2) return unitsOk();
      if (i === 3) return metaDone();
      if (i === 4) return !!S.validation && S.validation.ok === true && S.graphsDrawn && !S.validating;
      return true;
    };
    const body = () => {
      const meta = { unavailable: [...S.unav] };
      CSV_META.forEach(([k, , t]) => { const v = String(S.meta[k]).trim(); meta[k] = S.unav.has(k) || v === '' ? null : (t === 'number' ? Number(v) : v); });
      meta.original_filename = S.file.original_filename;
      const b = { file_hash: S.file.file_hash, mapping: mappingObj(), units: { ...S.units }, metadata: meta, duplicate_policy: S.dup, order_policy: S.order };
      if (S.dataType) b.data_type = S.dataType;
      if (S.tz.trim()) b.tz_offset = S.tz.trim();
      return b;
    };

    /* ---------------------------------------------------------------- graphs from the uploaded rows */
    const toNum = (v) => { const n = Number(String(v).trim()); return String(v).trim() !== '' && Number.isFinite(n) ? n : null; };
    const graphs = () => {
      const f = S.file, m = mappingObj(), col = (n) => f.columns.indexOf(m[n]);
      const channels = Object.keys(m).filter((k) => !NOT_CHANNELS.includes(k));
      let xi = col('session_time_s'), xLabel = 'session time', tsMode = false;
      if (xi < 0) { xi = col('timestamp'); tsMode = xi >= 0; }
      if (xi < 0) { xi = col('lap_number'); xLabel = 'lap'; } else if (tsMode) xLabel = 'time (s from first sample)';
      let t0 = null;
      const xs = f.sample_rows.map((r, i) => {
        if (xi < 0) return i + 1;
        let v = toNum(r[xi]);
        if (v === null && tsMode) { const d = Date.parse(r[xi]); v = Number.isNaN(d) ? null : d / 1000; }
        if (v !== null && tsMode) { if (t0 === null) t0 = v; v -= t0; }
        return v;
      });
      if (!channels.length) return '<p class="hint">No telemetry channel is mapped, so there is nothing to plot.</p>';
      return `<div class="grid g2">${channels.map((ch, k) => {
        const ci = col(ch), pts = f.sample_rows.map((r, i) => ({ x: xs[i], y: toNum(r[ci]) })).filter((p) => p.x !== null && p.y !== null);
        return `<div><b class="mono">${esc(ch)}</b> <span class="dim">(${esc(m[ch])}, first ${pts.length} rows)</span>${pts.length > 1
          ? Charts.lines({ series: [{ name: ch, color: GRAPH_COLORS[k % GRAPH_COLORS.length], pts }], width: 420, height: 200, xLabel, yLabel: ch })
          : '<div class="empty">Not enough numeric rows to plot.</div>'}</div>`;
      }).join('')}</div>`;
    };

    /* ---------------------------------------------------------------- steps */
    const bodyFor = () => {
      const f = S.file;
      if (S.step === 0) return `<div class="field"><label for="hcfile">CSV file</label><input type="file" id="hcfile" accept=".csv,text/csv">
          <div class="hint">${S.uploading ? 'Uploading&hellip;' : (f ? `<b>${esc(f.original_filename)}</b> &middot; ${esc(f.row_count)} rows &middot; ${esc(f.column_count)} columns &middot; detected: ${esc(f.detected_type)}` : 'Choose the CSV file to import.')}</div></div>`;
      if (S.step === 1) {
        const opt = (sel) => ['', ...new Set([...CSV_FIELDS, ...Object.keys(f.suggested_mapping || {})])].map((o) => `<option value="${esc(o)}" ${o === sel ? 'selected' : ''}>${o ? esc(o) : '(not mapped)'}</option>`).join('');
        const dups = dupFields(), choices = f.data_type_choices || [];
        return `${choices.length > 1 ? `<div class="field"><label for="hcdtype">The file looks like both types. Choose one</label><select id="hcdtype"><option value="">Choose&hellip;</option>${choices.map((c) => `<option value="${esc(c)}" ${S.dataType === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></div>` : ''}
          ${!choices.length ? '<div class="errors" role="alert">The columns of this file are not recognised as lap summary or sampled telemetry.</div>' : ''}
          ${dups.length ? `<div class="errors" role="alert">Each standard field can be mapped to one column only: ${dups.map(esc).join(', ')}</div>` : ''}
          <table class="tbl"><thead><tr><th>CSV column</th><th>First value</th><th>Standard field</th></tr></thead><tbody>${f.columns.map((c, i) => `<tr><td class="mono">${esc(c)}</td><td class="dim">${esc((f.sample_rows[0] || [])[i] ?? '')}</td>
          <td><select class="hcmap" data-col="${esc(c)}" aria-label="Field for ${esc(c)}">${opt(S.colField[c] || '')}</select></td></tr>`).join('')}</tbody></table>`;
      }
      if (S.step === 2) return `<div class="grid g2">${Object.entries(UNIT_CHOICES).map(([k, v]) => `<div class="field"><label for="hcu-${k}">${esc(k)} unit</label><select id="hcu-${k}" data-unit="${k}">${v.map((u) => `<option value="${esc(u)}" ${S.units[k] === u ? 'selected' : ''}>${esc(u)}</option>`).join('')}</select></div>`).join('')}
          <div class="field"><label for="hctz">UTC offset for timestamps without one (optional, e.g. +02:00)</label><input id="hctz" value="${esc(S.tz)}"></div></div>`;
      if (S.step === 3) return `<div class="grid g2">${CSV_META.map(([k, label, t]) => `<div class="field"><label for="hcm-${k}">${esc(label)}</label>
          <input id="hcm-${k}" data-meta="${k}" type="${t}" value="${esc(S.meta[k])}" ${S.unav.has(k) ? 'disabled' : ''}>
          <label style="text-transform:none;letter-spacing:0"><input type="checkbox" data-unav="${k}" ${S.unav.has(k) ? 'checked' : ''}> not available</label></div>`).join('')}</div>`;
      if (S.step === 4) {
        const v = S.validation, rep = v?.report;
        const probs = [];
        if (v) {
          (v.mapping?.errors || []).forEach((e) => probs.push({ row: '', text: String(e) }));
          if (v.metadata && v.metadata.ok === false) probs.push({ row: '', text: v.metadata.message || 'session metadata rejected' });
          if (v.existing_session_id) probs.push({ row: '', text: `this file is already imported as session ${v.existing_session_id}` });
          (rep?.errors || []).forEach((e) => probs.push({ row: e.row, text: `${e.driver ? `${e.driver}: ` : ''}${e.column || ''} ${e.reason || ''}${e.value !== undefined && e.value !== null ? ` (value ${e.value})` : ''}` }));
        }
        const total = (rep?.error_count ?? 0) + probs.filter((p) => p.row === '').length;
        return `<div class="grid g2"><div class="field"><label for="hcdup">Duplicate rows</label><select id="hcdup"><option value="reject" ${S.dup === 'reject' ? 'selected' : ''}>Reject (blocking error)</option><option value="drop_exact_duplicates" ${S.dup === 'drop_exact_duplicates' ? 'selected' : ''}>Drop exact duplicates</option></select></div>
          <div class="field"><label for="hcord">Out-of-order samples</label><select id="hcord"><option value="reject" ${S.order === 'reject' ? 'selected' : ''}>Reject (blocking error)</option><option value="sort" ${S.order === 'sort' ? 'selected' : ''}>Sort by time</option></select></div></div>
          <div id="hcgraphs">${graphs()}</div>
          <div id="hcval">${S.validating ? '<span class="dim">Validating&hellip;</span>' : (!v ? '' : (v.ok ? '<div class="note info">Validation passed: no blocking errors.</div>'
            : `<div class="errors" role="alert" id="hcerrs"><b>${esc(total)} blocking error${total === 1 ? '' : 's'}</b> (showing ${esc(Math.min(probs.length, 200))})<ul style="max-height:240px;overflow:auto">${probs.slice(0, 200).map((p) => `<li>${p.row !== '' ? `row <b>${esc(p.row)}</b>: ` : ''}${esc(p.text)}</li>`).join('')}</ul></div>`))}</div>`;
      }
      return `<div class="note info">Ready to import <b>${esc(S.file.original_filename)}</b> as <b>${esc(kind())}</b> (${esc(Object.keys(mappingObj()).length)} mapped columns).</div>
        ${S.notice ? `<div class="note">${S.notice}</div>` : ''}<div id="hcerr"></div>`;
    };
    const render = () => {
      const last = S.step === 5;
      steps.innerHTML = `<ol class="row" style="gap:14px;flex-wrap:wrap;list-style:none;padding:0" id="hcstepper">${CSV_STEPS.map((n, i) => `<li ${i === S.step ? 'aria-current="step"' : ''} style="${i === S.step ? 'font-weight:bold' : 'opacity:.6'}">${i + 1}. ${esc(n)}</li>`).join('')}</ol>
        <div id="hcbody">${bodyFor()}</div>
        <div class="row" style="gap:8px;margin-top:10px"><button class="btn" id="hcback" type="button" ${S.step === 0 ? 'disabled' : ''}>Back</button>
        ${last ? '<button class="btn primary" id="hcstart" type="button">Start import</button>' : `<button class="btn primary" id="hcnext" type="button" ${complete(S.step) ? '' : 'disabled'}>Next</button>`}</div>`;
      wire();
      if (last) { const b = $('#hcstart', steps); b.disabled = !complete(4) || !!jobId && ACTIVE_JOB; }
    };
    let ACTIVE_JOB = false;
    const refreshNext = () => { const n = $('#hcnext', steps); if (n) n.disabled = !complete(S.step); };

    const wire = () => {
      $('#hcback', steps).addEventListener('click', () => { S.step -= 1; render(); });
      $('#hcnext', steps)?.addEventListener('click', () => { if (!complete(S.step)) return; S.step += 1; render(); if (S.step === 4) validate(); });
      $('#hcstart', steps)?.addEventListener('click', start);
      $('#hcfile', steps)?.addEventListener('change', (e) => upload(e.target.files[0]));
      $('#hcdtype', steps)?.addEventListener('change', (e) => { S.dataType = e.target.value; render(); });
      steps.querySelectorAll('.hcmap').forEach((s) => s.addEventListener('change', () => { S.colField[s.dataset.col] = s.value; render(); }));
      steps.querySelectorAll('[data-unit]').forEach((s) => s.addEventListener('change', () => { S.units[s.dataset.unit] = s.value; refreshNext(); }));
      $('#hctz', steps)?.addEventListener('input', (e) => { S.tz = e.target.value; });
      steps.querySelectorAll('[data-meta]').forEach((i) => i.addEventListener('input', () => { S.meta[i.dataset.meta] = i.value; refreshNext(); }));
      steps.querySelectorAll('[data-unav]').forEach((c) => c.addEventListener('change', () => { if (c.checked) S.unav.add(c.dataset.unav); else S.unav.delete(c.dataset.unav); render(); }));
      $('#hcdup', steps)?.addEventListener('change', (e) => { S.dup = e.target.value; validate(); });
      $('#hcord', steps)?.addEventListener('change', (e) => { S.order = e.target.value; validate(); });
    };

    /* ---------------------------------------------------------------- upload + validation */
    async function upload(file) {
      if (!file) return;
      ctx.err(null); S.uploading = true; render();
      const fd = new FormData(); fd.append('file', file, file.name);
      try {
        let res;
        try { res = await fetch('/api/historical/csv/preview', { method: 'POST', body: fd }); } catch (_) { const e = new Error(connMsg); e.status = 0; throw e; }
        const data = await res.json().catch(() => null);
        if (!res.ok) { const d = data && data.detail; throw new Error(Array.isArray(d) ? d.map((i) => i.message).join('; ') : String(d || 'upload rejected')); }
        const same = S.file && S.file.file_hash === data.file_hash;
        S.file = data;
        if (!same) {  // a different file: its columns need a new mapping; entered units and metadata stay
          S.colField = {};
          Object.entries(data.suggested_mapping || {}).forEach(([fld, c]) => { S.colField[c] = fld; });
          S.dataType = (data.data_type_choices || []).length === 1 ? data.data_type_choices[0] : '';
          S.validation = null; S.graphsDrawn = false;
        }
      } catch (e) { ctx.err(isConn(e) ? new Error(connMsg) : e); }
      S.uploading = false; render();
    }
    async function validate() {
      const seq = ++valSeq;
      S.validating = true; S.validation = null; S.graphsDrawn = false; render();
      const gb = $('#hcgraphs', steps);
      if (gb) S.graphsDrawn = true;  // graphs come from the rows already uploaded, so they are on screen before validation returns
      try {
        const v = await api('POST', '/api/historical/csv/validate', body());
        if (seq !== valSeq) return;
        S.validation = v; ctx.err(null);
      } catch (e) { if (seq !== valSeq) return; S.validation = { ok: false, mapping: { errors: [isConn(e) ? connMsg : e.message] } }; }
      S.validating = false; render();
    }

    /* ---------------------------------------------------------------- import start, duplicate handling and job tracking */
    const metaKeyComplete = () => ['year', 'event', 'session_type', 'driver'].every((k) => !S.unav.has(k) && String(S.meta[k]).trim() !== '');
    const sessionDetails = async (sid) => {
      let m = null;
      try { m = await api('GET', `/api/historical/sessions/${encodeURIComponent(sid)}`); } catch (_) { /* the id alone is still shown */ }
      return `<div class="hint">Existing session <a href="#/hist/${encodeURIComponent(sid)}/overview" class="mono">${esc(sid)}</a>${m ? ` &middot; ${[m.year, m.event, m.session_type || m.session, m.driver].filter((x) => x !== undefined && x !== null && x !== '').map(esc).join(' &middot; ')}` : ''}</div>`;
    };
    const decisionBox = async (sid, options, onChoice) => {
      const info = await sessionDetails(sid);
      jobBox.innerHTML = `<div class="note" id="hcdup-dialog" role="dialog" aria-label="Duplicate session"><b>A session with the same event, session type, driver and year already exists.</b>${info}
        <div class="row" style="gap:8px;margin-top:6px">${options.includes('replace') ? '<button class="btn small primary" id="hcreplace" type="button">Replace existing session</button>' : ''}
        ${options.includes('cancel') ? '<button class="btn small" id="hccancelopt" type="button">Cancel and choose a different driver or session type</button>' : ''}</div></div>`;
      $('#hcreplace', jobBox)?.addEventListener('click', () => onChoice('replace'));
      $('#hccancelopt', jobBox)?.addEventListener('click', () => onChoice('cancel'));
    };
    async function start() {
      const b = $('#hcstart', steps), errBox = $('#hcerr', steps);
      errBox.innerHTML = '';
      b.disabled = true;
      try {
        const hashHit = await api('GET', `/api/historical/csv/session-exists?file_hash=${S.file.file_hash}`);
        if (hashHit.exists) {
          b.disabled = false;
          jobBox.innerHTML = `<div class="errors" role="alert">This exact file is already imported. Nothing was started.${await sessionDetails(hashHit.session_id)}</div>`;
          return;
        }
        if (metaKeyComplete()) {
          const q = new URLSearchParams({ event: S.meta.event.trim(), session_type: S.meta.session_type.trim(), driver: S.meta.driver.trim(), year: String(Number(S.meta.year)) });
          const keyHit = await api('GET', `/api/historical/csv/session-exists?${q}`);
          if (keyHit.exists) {  // do not start until the user picks an option (Req 16.12)
            await decisionBox(keyHit.session_id, ['replace', 'cancel'], (c) => {
              jobBox.innerHTML = '';
              if (c === 'replace') launch(true);
              else { b.disabled = false; S.step = 3; render(); }
            });
            return;
          }
        }
        await launch(false);
      } catch (e) { b.disabled = false; errBox.innerHTML = show(e); }  // start stays enabled for a retry, values untouched
    }
    async function launch(replace) {
      const b = $('#hcstart', steps), errBox = $('#hcerr', steps);
      try {
        const r = await api('POST', '/api/historical/import', { provider: 'csv_upload', csv: { ...body(), replace_existing: replace } });
        jobId = r.import_id; track();
      } catch (e) { if (b) b.disabled = false; if (errBox) errBox.innerHTML = show(e); }
    }
    function track() {
      clearTimeout(timer);
      const tick = async () => {
        if (!onPage() || !jobId) return;
        let j;
        try { j = await api('GET', `/api/historical/imports/${jobId}`); }
        catch (e) { jobBox.insertAdjacentHTML('afterbegin', `<div class="errors" role="alert" data-conn>${esc(isConn(e) ? connMsg : e.message)}</div>`); timer = setTimeout(tick, POLL_MS * 2); return; }
        const active = !TERMINAL.includes(j.state);
        ACTIVE_JOB = active;
        const st = $('#hcstart', steps); if (st) st.disabled = active || !complete(4);
        const rowPct = Number.isInteger(j.processed_rows) && Number.isInteger(j.total_rows) && j.total_rows > 0 ? Math.min(100, Math.floor(j.processed_rows * 100 / j.total_rows)) : null;
        const pct = j.state === 'COMPLETED' ? 100 : (rowPct !== null && j.state === 'PROCESSING' ? rowPct : (PROGRESS[j.state] ?? 0));
        const aw = j.awaiting_decision;
        const sig = JSON.stringify([j.state, j.detail, j.processed_rows, j.error, aw && aw.existing_session_id]);
        if (sig !== lastSig) {
          lastSig = sig;
          const retryable = j.state === 'FAILED' && j.error_code !== 'UPLOAD_NOT_FOUND' || j.state === 'CANCELLED';
          if (aw) { await decisionBox(aw.existing_session_id, aw.options || ['replace', 'cancel'], async (c) => {
            try { await api('POST', `/api/historical/imports/${jobId}/decision?choice=${c}`); lastSig = ''; } catch (e) { jobBox.insertAdjacentHTML('afterbegin', show(e)); }
            if (c === 'cancel') { S.step = 3; render(); }
          }); } else {
            jobBox.innerHTML = `<div class="note ${j.state === 'FAILED' ? '' : 'info'}" id="hcstatus"><b id="hcstate">${esc(j.state)}</b> &middot; <span id="hcpct">${pct}%</span> <progress max="100" value="${pct}"></progress> &middot; ${esc(j.detail || '')}
              ${j.state === 'FAILED' ? `<div class="badc" role="alert">Import failed: ${esc(j.error_code || '')} ${esc(j.error || 'no reason given')}</div>` : ''}
              ${active ? '<button class="btn small" id="hccancel" type="button">Cancel</button>' : ''}${retryable ? ' <button class="btn small" id="hcretry" type="button">Retry</button>' : ''}
              ${j.state === 'FAILED' && !retryable ? '<div class="hint">The stored upload is gone; upload the file again to retry. Your mapping, units and metadata are kept.</div>' : ''}<div id="hcjerr"></div></div>`;
            $('#hccancel', jobBox)?.addEventListener('click', async () => {
              try { const c = await api('POST', `/api/historical/imports/${jobId}/cancel`); lastSig = ''; if (c && c.state) $('#hcstate', jobBox).textContent = c.state; } catch (e) { $('#hcjerr', jobBox).innerHTML = show(e); }
            });
            $('#hcretry', jobBox)?.addEventListener('click', async () => {
              try { jobId = (await api('POST', `/api/historical/imports/${jobId}/retry`)).import_id; lastSig = ''; track(); } catch (e) { $('#hcjerr', jobBox).innerHTML = show(e); }
            });
          }
        }
        if (j.state === 'COMPLETED') {
          opts.onChange?.();
          const href = `#/hist/${encodeURIComponent(j.session_id)}/overview`;
          jobBox.insertAdjacentHTML('beforeend', `<div class="hint">Opening the session&hellip; <a href="${href}">open now</a></div>`);
          if (opts.navigate) setTimeout(() => { if (onPage()) location.hash = href; }, 500);
          return;
        }
        if (!active) { opts.onChange?.(); return; }
        if (onPage()) timer = setTimeout(tick, POLL_MS);
      };
      tick();
    }
    render();
    return { stop: () => clearTimeout(timer), track: (id) => { jobId = id; track(); } };
  }

  return { mount };
})();
