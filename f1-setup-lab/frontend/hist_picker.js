/* Shared "choose a real Formula 1 session and import it" component. Used by New experiment (Historical F1 data) and by the
   Historical F1 page. Everything is loaded from the API: seasons are the provider's range, Grand Prix / session / driver lists come from
   /api/historical/events and /api/historical/drivers, never from constants. All dynamic text is escaped with Charts.esc(). */
const HistPicker = (() => {
  const esc = Charts.esc;
  const FIRST_YEAR = { openf1: 2023, fastf1: 2018 };
  const LABEL = { openf1: 'OpenF1 (free, no account, 2023 onwards)', fastf1: 'FastF1 (needs an F1TV account, 2018 onwards)' };
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
      <div class="grid g2"><div class="field"><label for="hprov">Data provider</label><select id="hprov">${Object.entries(LABEL).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('')}</select></div>
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
        <div class="row hopen" style="gap:14px;flex-wrap:wrap;margin-top:6px">${DATASET_CHOICES.map(([id, label, ds]) => cb(id, label, `data-ds="${ds}"`)).join('')}</div>
        <div class="hint hopen" id="hdshint"></div></div>
      <div class="field hopen">${cb('hfresh', 'download again, ignoring the OpenF1 cache', '', false)}</div>
      <button class="btn primary" id="himport" type="button" disabled>Load selected F1 data</button><span class="dim" id="hmsg"></span><div id="hprog"></div></div>
      <div class="note info">Imports real timing data into the historical library. No experiment runs are created, and this data never enters the setup design or optimiser: it contains no car setup. Results are cached locally; OpenF1 is throttled to 30 requests a minute, so a full field can take several minutes.</div>
      <p class="hint" id="hcache"></p>`;

    let events = [], drivers = [], evSeq = 0, drvSeq = 0, timer = null;
    const provider = () => $('#hprov', root).value;
    const ev = () => events[+$('#hevent', root).value];
    const sess = () => (ev()?.sessions || []).find((s) => s.type === $('#hsession', root).value);
    const err = (e) => { $('#hist-err', root).innerHTML = e ? errorsBox(e) : ''; };

    const syncProvider = () => {
      root.querySelectorAll('.hopen').forEach((e) => { e.hidden = provider() !== 'openf1'; });
      const first = FIRST_YEAR[provider()], y = $('#hyear', root);
      y.innerHTML = Array.from({ length: year0 - first + 1 }, (_, i) => year0 - i).map((v) => `<option value="${v}">${v}</option>`).join('');
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
        const ds = await api('GET', `/api/historical/drivers?year=${+$('#hyear', root).value}&event=${encodeURIComponent(e.event)}&session=${encodeURIComponent(s)}&provider=${provider()}`);
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
        const list = await api('GET', `/api/historical/events?year=${y}&provider=${provider()}`);
        if (seq !== evSeq) return;
        events = list.filter((e) => (e.sessions || []).length);
        if (!events.length && stepBack < 2 && y - 1 >= FIRST_YEAR[provider()] && opts.autoLatest !== false && !root.dataset.userYear) {
          $('#hyear', root).value = String(y - 1);  // the newest season may not have a published event yet
          return loadEvents(stepBack + 1);
        }
        err(null); fillEvents(); await loadDrivers();
      } catch (x) { if (seq === evSeq) { events = []; fillEvents(); err(x); } }
    };

    root.querySelector('#hprov').addEventListener('change', () => { delete root.dataset.userYear; syncProvider(); loadEvents(); });
    root.querySelector('#hyear').addEventListener('change', () => { root.dataset.userYear = '1'; loadEvents(); });
    root.querySelector('#hevent').addEventListener('change', () => { fillSessions(); loadDrivers(); });
    root.querySelector('#hsession').addEventListener('change', () => { syncChannels(); loadDrivers(); });
    root.querySelector('#hdrvsearch').addEventListener('input', drawDrivers);
    root.querySelector('#hdrvall').addEventListener('click', () => { visibleDrivers().forEach((d) => selected.add(d.code)); drawDrivers(); });
    root.querySelector('#hdrvnone').addEventListener('click', () => { visibleDrivers().forEach((d) => selected.delete(d.code)); drawDrivers(); });

    /* ------------------------------------------------------------- import + progress */
    const request = () => {
      const e = ev(), open = provider() === 'openf1';
      const codes = [...selected];
      const body = { provider: provider(), year: +$('#hyear', root).value, event: e.event, session: $('#hsession', root).value,
        drivers: codes.length === drivers.length ? [] : codes,  // empty list = the whole field
        include_telemetry: $('#htele', root).checked, include_position: $('#hpos', root).checked, include_weather: $('#hwx', root).checked };
      if (open) {
        const ds = [];
        root.querySelectorAll('[data-ds] input').forEach((i) => { if (i.checked && !i.disabled) ds.push(...i.closest('[data-ds]').dataset.ds.split(',')); });
        body.race_datasets = ds;
        body.include_race_data = ds.some((n) => !['stints', 'pit', 'race_control'].includes(n));
        body.refresh_cache = $('#hfresh', root).checked;
      }
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

    $('#hprov', root).value = 'openf1';  // real data from OpenF1 is the default: free, no account
    syncProvider();
    loadEvents();
    loadCache();
    return { track, stop: () => clearTimeout(timer) };
  }
  return { mount };
})();
