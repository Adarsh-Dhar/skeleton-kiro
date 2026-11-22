/* Historical F1: OpenF1 race analysis views (running order, results, grid, gaps, overtakes, pit & strategy, race control,
   team radio) and the import-coverage / provenance panel. Every text value is escaped with esc(). */
const HistRace = (() => {
  const esc = Charts.esc, fmt = Charts.f;
  const PALETTE = ['#e10600', '#4aa8ff', '#2ecc71', '#ffb400', '#c99bff', '#00cec9', '#ff7ab8', '#a3e635', '#fb923c', '#94a3b8'];
  const colour = (i) => (i < PALETTE.length ? PALETTE[i] : `hsl(${(i * 47) % 360} 70% 60%)`);
  const COMPOUND = { SOFT: '#e10600', MEDIUM: '#ffd23f', HARD: '#f1f1f1', INTERMEDIATE: '#39b54a', WET: '#2f7fd8' };
  const FLAG = { GREEN: '#2ecc71', YELLOW: '#ffd23f', 'DOUBLE YELLOW': '#ffd23f', RED: '#e10600', BLUE: '#4aa8ff', CHEQUERED: '#f1f1f1', CLEAR: '#2ecc71', 'BLACK AND WHITE': '#94a3b8' };

  /* "m:ss.mmm" for a duration in seconds, "h:mm:ss.mmm" above an hour. */
  const dur = (s) => {
    if (s === null || s === undefined || Number.isNaN(s)) return '&mdash;';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s - h * 3600 - m * 60;
    const sec = r.toFixed(3).padStart(6, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
  };
  const clock = (s) => (s === null || s === undefined ? '&mdash;' : `${s < 0 ? '-' : ''}${Math.floor(Math.abs(s) / 60)}:${String(Math.floor(Math.abs(s) % 60)).padStart(2, '0')}`);
  const entry = (m, name) => (m.coverage?.endpoints || {})[name];

  /* Why a dataset has no rows, in the words of the import coverage (never a silent blank). */
  const unavailable = (m, name, label) => {
    const e = entry(m, name);
    let why;
    if (!m.race_data_included && !['pit_stops', 'race_control'].includes(name)) why = 'Race data was switched off for this import. Re-import with "race data" ticked to load it.';
    else if (!e) why = 'This import did not request it.';
    else if (e.status === 'not_applicable') why = `OpenF1 publishes ${esc(label)} only for race and sprint sessions.`;
    else if (e.status === 'error') why = `The request failed: ${esc(e.note || 'unknown error')}. Re-import to try again.`;
    else why = `OpenF1 returned no rows for this session${e.note ? ` (${esc(e.note)})` : ''}.`;
    return `<div class="empty">No ${esc(label)}. ${why}</div>`;
  };
  const getSet = async (api, id, name, query = '') => api('GET', `/api/historical/sessions/${id}/datasets/${name}?${query}`);

  /* ------------------------------------------------------------------ coverage / provenance */
  const STATUS_CLASS = { ok: 'good', empty: 'dim', error: 'badc', not_applicable: 'dim' };
  function coverage(m) {
    const c = m.coverage;
    if (!c) return '';
    const rows = Object.entries(c.endpoints || {}).map(([name, e]) => {
      const where = e.endpoint && e.endpoint !== name ? ` <span class="dim">(${esc(e.endpoint)})</span>` : '';
      const note = e.note ? `<div class="dim">${esc(e.note)}</div>` : '';
      return `<tr><td class="mono">${esc(name)}${where}</td><td><span class="${STATUS_CLASS[e.status] || 'dim'}">${esc(String(e.status).replace('_', ' '))}</span>${note}</td>
        <td class="num">${esc(e.rows)}</td><td class="num">${esc(e.requests ?? 0)} / ${esc(e.cache_hits ?? 0)}</td><td class="dim mono">${esc((e.fetched_at || '').replace('T', ' ').slice(0, 19) || '—')}</td></tr>`;
    }).join('');
    const empties = Object.entries(c.drivers_empty || {}).filter(([, v]) => (v || []).length)
      .map(([k, v]) => `<tr><td class="dim">${esc(k)} missing for</td><td class="mono" colspan="4">${v.map(esc).join(', ')}</td></tr>`).join('');
    const up = m.upstream || {};
    const list = (title, items) => (items && items.length ? `<h3>${title}</h3><ul class="hint">${items.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '');
    const units = Object.entries(m.units || {}).filter(([k]) => k !== 'y_m' && k !== 'z_m').map(([k, v]) => `${k === 'x_m' ? 'x_m / y_m / z_m' : k}: ${v}`);
    const drs = m.drs_states ? [`DRS codes: ${Object.entries(m.drs_states).map(([k, v]) => `${k} = ${v || 'undocumented'}`).join('; ')}`] : [];
    return `<div class="panel"><h2>Import coverage ${m.partial ? '<span class="tag">partial</span>' : ''}</h2>
      <p class="hint">Imported ${esc((c.requested_at || m.imported_at || '').replace('T', ' ').slice(0, 19))} UTC &middot; ${c.refresh_cache ? 'cache ignored (fresh download)' : 'cache allowed'} &middot; ${esc(up.network_requests ?? '?')} downloads, ${esc(up.cache_hits ?? '?')} cache hits in the client. Each row: rows returned, downloads / cache hits, oldest answer used.</p>
      <div style="overflow:auto"><table><thead><tr><th>Dataset</th><th>Status</th><th class="num">Rows</th><th class="num">Req / cache</th><th>Fetched</th></tr></thead><tbody>${rows}${empties}</tbody></table></div>
      ${(m.warnings || []).length ? `<p class="hint">${m.warnings.map(esc).join('<br>')}</p>` : ''}
      <details><summary class="hint">Which values are provided, estimated or missing</summary>
        ${list('Estimated here from OpenF1 data', m.derived)}${list('Not provided by OpenF1', m.not_provided)}${list('Units and codes', units.concat(drs))}</details></div>`;
  }

  /* ------------------------------------------------------------------ race tab */
  const driverOptions = (drivers, sel) => drivers.map((d) => `<option value="${esc(d.code)}" ${d.code === sel ? 'selected' : ''}>${esc(d.code)} &middot; ${esc(d.name)}</option>`).join('');

  function resultsTable(m, res, grid) {
    if (!res.length) return unavailable(m, 'session_result', 'classification');
    const gridPos = Object.fromEntries(grid.map((g) => [g.driver, g.position]));
    const team = Object.fromEntries((m.drivers || []).map((d) => [d.code, d.team]));
    const isQ = m.session_type === 'Q' || m.session_type === 'SQ';
    const body = res.map((r) => {
      const status = r.dnf ? '<span class="tag">DNF</span>' : r.dns ? '<span class="tag">DNS</span>' : r.dsq ? '<span class="tag">DSQ</span>' : '';
      const g = gridPos[r.driver];
      const change = g != null && r.position != null ? g - r.position : null;
      const chg = change === null ? '&mdash;' : change > 0 ? `<span class="good">+${change}</span>` : change < 0 ? `<span class="badc">${change}</span>` : '0';
      let time;
      if (Array.isArray(r.duration)) time = r.duration.map((v) => (v === null ? '&mdash;' : dur(v))).join(' &middot; ');
      else time = dur(r.duration);
      let gap = '&mdash;';
      if (r.gap_to_leader_text) gap = esc(r.gap_to_leader_text);
      else if (r.gap_to_leader_s !== null && r.gap_to_leader_s !== undefined) gap = r.gap_to_leader_s === 0 ? 'leader' : `+${fmt(r.gap_to_leader_s, 3)} s`;
      else if (Array.isArray(r.gap_to_leader)) gap = r.gap_to_leader.map((v) => (v === null ? '&mdash;' : `+${fmt(v, 3)}`)).join(' &middot; ');
      return `<tr><td class="num"><b>${r.position ?? '&mdash;'}</b></td><td class="mono"><b>${esc(r.driver)}</b> ${status}</td><td class="dim">${esc(team[r.driver] || '')}</td>
        ${isQ ? '' : `<td class="num">${g ?? '&mdash;'}</td><td class="num">${chg}</td>`}<td class="num">${esc(r.laps ?? '—')}</td><td class="num">${time}</td><td class="num">${gap}</td>${isQ ? '' : `<td class="num">${r.points ?? '&mdash;'}</td>`}</tr>`;
    }).join('');
    const head = isQ ? '<th>Pos</th><th>Driver</th><th>Team</th><th class="num">Laps</th><th class="num">Q1 &middot; Q2 &middot; Q3 best</th><th class="num">Gap Q1 &middot; Q2 &middot; Q3</th>'
      : '<th>Pos</th><th>Driver</th><th>Team</th><th class="num">Grid</th><th class="num">+/&minus;</th><th class="num">Laps</th><th class="num">Time</th><th class="num">Gap</th><th class="num">Pts</th>';
    return `<div style="overflow:auto"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
      <p class="hint">${isQ ? '' : `Grid: ${grid.length ? 'as published by OpenF1 under the qualifying session' : 'not published for this session'}. `}A driver with no classified position is shown by status (DNF, DNS, DSQ) as OpenF1 reports it.</p>`;
  }

  function positionChart(m, pos, picks, drivers) {
    if (!pos.length) return unavailable(m, 'race_position', 'running order');
    const maxP = Math.max(...pos.map((p) => p.position));
    const series = picks.map((code) => {
      const idx = drivers.findIndex((d) => d.code === code);
      const pts = pos.filter((p) => p.driver === code && p.time_s !== null).map((p) => ({ x: p.time_s / 60, y: p.position }));
      return { name: code, color: colour(idx), pts };
    }).filter((s) => s.pts.length);
    if (!series.length) return '<div class="empty">Select at least one driver.</div>';
    return Charts.lines({ series, stepped: true, noDots: true, reverseY: true, ymin: 0.5, ymax: maxP + 0.5, xLabel: 'minutes since the session start time OpenF1 publishes', yLabel: 'running order (1 = leader)', height: 360 });
  }

  function gapChart(m, rows, code) {
    if (!rows.length) return unavailable(m, 'intervals', 'gap data');
    const num = (key) => rows.filter((r) => r[key] !== null && r[key] !== undefined && r.time_s !== null).map((r) => ({ x: r.time_s / 60, y: r[key] }));
    const lapped = rows.filter((r) => r.gap_to_leader_text).length;
    const series = [{ name: 'gap to leader (s)', color: '#e10600', pts: num('gap_to_leader_s') }, { name: 'gap to car ahead (s)', color: '#4aa8ff', pts: num('interval_s') }].filter((s) => s.pts.length);
    return `${series.length ? Charts.lines({ series, noDots: true, xLabel: 'minutes since the session start time OpenF1 publishes', yLabel: 'seconds', ymin: 0 }) : '<div class="empty">Only text gaps (lapped) for this driver.</div>'}
      <p class="hint">${esc(code)}: ${rows.length} readings${lapped ? `, ${lapped} while lapped (OpenF1 gives text such as "+1 LAP", not seconds, so those are not plotted)` : ''}. The leader has no gap to car ahead.</p>`;
  }

  function overtakesPanel(m, rows) {
    if (!rows.length) return unavailable(m, 'overtakes', 'overtakes');
    const made = {}, lost = {};
    rows.forEach((r) => { made[r.overtaking] = (made[r.overtaking] || 0) + 1; lost[r.overtaken] = (lost[r.overtaken] || 0) + 1; });
    const codes = [...new Set([...Object.keys(made), ...Object.keys(lost)])].sort((a, b) => (made[b] || 0) - (made[a] || 0));
    return `<div class="grid g2"><div><h3>Per driver</h3><table><thead><tr><th>Driver</th><th class="num">Made</th><th class="num">Lost</th></tr></thead><tbody>
      ${codes.map((c) => `<tr><td class="mono"><b>${esc(c)}</b></td><td class="num">${made[c] || 0}</td><td class="num">${lost[c] || 0}</td></tr>`).join('')}</tbody></table></div>
      <div><h3>Timeline (${rows.length})</h3><div style="overflow:auto;max-height:340px"><table><thead><tr><th class="num">Time</th><th>Overtaker</th><th>Passed</th><th class="num">New pos</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td class="num mono">${clock(r.time_s)}</td><td class="mono"><b>${esc(r.overtaking)}</b></td><td class="mono">${esc(r.overtaken)}</td><td class="num">${r.position ?? '&mdash;'}</td></tr>`).join('')}</tbody></table></div></div></div>
      <p class="hint">Events as detected and published by OpenF1; time is since the session start time OpenF1 publishes (m:ss).</p>`;
  }

  async function race(body, api, id, m) {
    const drivers = m.drivers || [];
    const [res, grid, pos, over] = await Promise.all(['session_result', 'starting_grid', 'race_position', 'overtakes'].map((n) => getSet(api, id, n, 'limit=0')));
    const finishers = res.rows.filter((r) => r.position != null).map((r) => r.driver);
    let picks = (finishers.length ? finishers : drivers.map((d) => d.code)).slice(0, 6);
    const isRace = ['R', 'S'].includes(m.session_type);
    body.innerHTML = `<div class="panel"><h2>${isRace ? 'Starting grid vs final classification' : 'Classification'}</h2>${resultsTable(m, res.rows, grid.rows)}</div>
      <div class="panel"><h2>Running order</h2><div class="row" id="hrpick" style="gap:12px;flex-wrap:wrap"></div><div id="hrchart"></div>
        <p class="hint">Running order is OpenF1's <code>position</code> dataset (P1, P2, ...). It is not the car's track location (the x/y data on the Track map tab). A position holds until the next change, so the chart is a step line.</p></div>
      ${isRace ? `<div class="panel"><h2>Gap to leader and to the car ahead</h2><div class="field" style="max-width:280px"><label for="hgapdrv">Driver</label><select id="hgapdrv">${driverOptions(drivers, picks[0] || drivers[0]?.code)}</select></div><div id="hgapout"><div class="dim">Loading&hellip;</div></div></div>
      <div class="panel"><h2>Overtakes</h2>${overtakesPanel(m, over.rows)}</div>` : `<div class="note info">Gaps, overtakes and the starting grid are published by OpenF1 for race and sprint sessions only.</div>`}`;
    const draw = () => {
      $pick().innerHTML = drivers.map((d, i) => `<label style="text-transform:none;letter-spacing:0"><input type="checkbox" class="hrp" value="${esc(d.code)}" ${picks.includes(d.code) ? 'checked' : ''}> <span style="color:${colour(i)}">&#9632;</span> ${esc(d.code)}</label>`).join('')
        + '<button class="btn small" id="hrtop" type="button">Top 6</button><button class="btn small" id="hrall" type="button">All</button>';
      document.getElementById('hrchart').innerHTML = positionChart(m, pos.rows, picks, drivers) + `<div class="legend">${picks.map((c) => `<span><i style="background:${colour(drivers.findIndex((d) => d.code === c))}"></i>${esc(c)}</span>`).join('')}</div>`;
      document.querySelectorAll('.hrp').forEach((c) => c.addEventListener('change', () => { picks = [...document.querySelectorAll('.hrp:checked')].map((x) => x.value); draw(); }));
      document.getElementById('hrtop').addEventListener('click', () => { picks = (finishers.length ? finishers : drivers.map((d) => d.code)).slice(0, 6); draw(); });
      document.getElementById('hrall').addEventListener('click', () => { picks = drivers.map((d) => d.code); draw(); });
    };
    const $pick = () => document.getElementById('hrpick');
    draw();
    if (isRace) {
      const showGap = async () => {
        const code = document.getElementById('hgapdrv').value;
        const r = await getSet(api, id, 'intervals', `driver=${encodeURIComponent(code)}&limit=3000`);
        document.getElementById('hgapout').innerHTML = gapChart(m, r.rows, code);
      };
      document.getElementById('hgapdrv').addEventListener('change', showGap);
      showGap();
    }
  }

  /* ------------------------------------------------------------------ pit & strategy tab */
  function stintStrip(laps, drivers) {
    const by = {};
    laps.forEach((l) => {
      if (l.stint == null || l.lap_number == null) return;
      const k = `${l.driver}|${l.stint}`;
      const s = by[k] || (by[k] = { driver: l.driver, stint: l.stint, compound: l.compound, a: l.lap_number, b: l.lap_number });
      s.a = Math.min(s.a, l.lap_number); s.b = Math.max(s.b, l.lap_number); s.compound = s.compound || l.compound;
    });
    const stints = Object.values(by);
    if (!stints.length) return '<div class="empty">No stint data (OpenF1 returned no stints).</div>';
    const order = drivers.map((d) => d.code).filter((c) => stints.some((s) => s.driver === c));
    const maxLap = Math.max(...stints.map((s) => s.b)), W = 760, left = 54, rowH = 22, H = order.length * rowH + 34;
    const X = (lap) => left + ((lap - 1) / maxLap) * (W - left - 12);
    let svg = '';
    order.forEach((c, i) => {
      const y = 8 + i * rowH;
      svg += `<text x="${left - 8}" y="${y + 14}" text-anchor="end">${esc(c)}</text>`;
      stints.filter((s) => s.driver === c).forEach((s) => {
        const col = COMPOUND[String(s.compound || '').toUpperCase()] || '#64748b';
        svg += `<rect x="${X(s.a)}" y="${y}" width="${Math.max(2, X(s.b + 1) - X(s.a))}" height="${rowH - 5}" rx="3" fill="${col}" opacity=".9"><title>${esc(c)} stint ${esc(s.stint)}: ${esc(s.compound || 'unknown compound')}, laps ${s.a}-${s.b}</title></rect>`;
      });
    });
    svg += `<text x="${(left + W) / 2}" y="${H - 6}" text-anchor="middle">lap</text>`;
    const legend = Object.entries(COMPOUND).map(([k, v]) => `<span><i style="background:${v}"></i>${k.toLowerCase()}</span>`).join('') + '<span><i style="background:#64748b"></i>unknown</span>';
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="tyre stints per driver">${svg}</svg><div class="legend">${legend}</div>`;
  }

  function pitTable(m, rows, hasStints) {
    if (!rows.length) return `${unavailable(m, 'pit_stops', 'pit stops')}${hasStints ? '<p class="hint">The tyre stints above still show where compounds changed; their timing is not a pit-lane measurement.</p>' : ''}`;
    return `<div style="overflow:auto"><table><thead><tr><th>Driver</th><th class="num">Lap</th><th class="num">Pit lane (s)</th><th class="num">Stationary (s)</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td class="mono"><b>${esc(r.driver)}</b></td><td class="num">${r.lap_number ?? '&mdash;'}</td><td class="num">${r.lane_duration_s === null ? '&mdash;' : fmt(r.lane_duration_s, 1)}</td><td class="num">${r.stop_duration_s === null ? '&mdash;' : fmt(r.stop_duration_s, 1)}</td></tr>`).join('')}</tbody></table></div>
      <p class="hint">Pit lane time is entry to exit as published by OpenF1; stationary time is only published from the 2024 United States GP onwards, so older sessions show &mdash;.</p>`;
  }

  /* Who a race control message is about: the driver OpenF1 provides, else the car named in the text ("CAR 44 (HAM) ..."). Works on
     sessions imported before the backend extracted it. */
  const CAR_RE = /\bCARS? (\d{1,2}) \(([A-Z]{3})\)/g;
  const subjectOf = (r, drivers) => {
    const code = (n) => (drivers.find((d) => String(d.number) === String(n)) || {}).code;
    if (r.driver) return { code: r.driver, source: r.driver_source || 'provided' };
    if (r.driver_number != null) return { code: code(r.driver_number) || String(r.driver_number), source: 'provided' };
    const hit = [...String(r.message || '').matchAll(CAR_RE)][0];
    return hit ? { code: code(hit[1]) || hit[2], source: 'message' } : null;
  };
  /* Times are relative to the session start OpenF1 publishes; negative = a message sent before the session began. */
  const rcTime = (s) => (s === null || s === undefined ? '&mdash;' : s < 0 ? `${clock(-s)} <span class="dim">before start</span>` : clock(s));
  function raceControlPanel(m, rows) {
    if (!rows.length) return unavailable(m, 'race_control', 'race control messages');
    const cats = [...new Set(rows.map((r) => r.category).filter(Boolean))].sort();
    const who = [...new Set(rows.map((r) => (subjectOf(r, m.drivers || []) || {}).code).filter(Boolean))].sort();
    return `<div class="row" style="gap:12px"><div class="field" style="max-width:240px"><label for="hrccat">Category</label><select id="hrccat"><option value="">All (${rows.length})</option>${cats.map((c) => `<option>${esc(c)}</option>`).join('')}</select></div>
      <div class="field" style="max-width:240px"><label for="hrcdrv">Driver</label><select id="hrcdrv"><option value="">All messages</option><option value="*track">Track / sector only (no driver)</option>${who.map((c) => `<option>${esc(c)}</option>`).join('')}</select></div></div>
      <div style="overflow:auto;max-height:420px"><table><thead><tr><th class="num">Time</th><th class="num">Lap</th><th>Category</th><th>Flag</th><th>Scope</th><th>Driver</th><th>Message</th></tr></thead><tbody id="hrcbody"></tbody></table></div>
      <p class="hint">Time is measured from the session start OpenF1 publishes; messages sent before the session began show "before start". A driver is shown when OpenF1 provides one or when the message names the car (marked "from message"); track and sector flags concern no single driver.</p>`;
  }
  function fillRaceControl(rows, drivers) {
    const cat = document.getElementById('hrccat')?.value || '', who = document.getElementById('hrcdrv')?.value || '';
    const body = document.getElementById('hrcbody');
    if (!body) return;
    body.innerHTML = rows.filter((r) => {
      const s = subjectOf(r, drivers);
      return (!cat || r.category === cat) && (!who || (who === '*track' ? !s : s && s.code === who));
    }).map((r) => {
      const col = FLAG[String(r.flag || '').toUpperCase()], s = subjectOf(r, drivers);
      const driver = s ? `<span class="mono"><b>${esc(s.code)}</b></span>${s.source === 'message' ? ' <span class="dim">from message</span>' : ''}` : '<span class="dim">&mdash;</span>';
      return `<tr><td class="num mono">${rcTime(r.time_s)}</td><td class="num">${r.lap_number ?? ''}</td><td>${esc(r.category || '')}</td>
        <td>${r.flag ? `<span style="color:${col || 'inherit'}">&#9632;</span> ${esc(r.flag)}` : ''}</td><td class="dim">${esc(r.scope || '')}${r.sector ? ` ${esc(r.sector)}` : ''}</td><td>${driver}</td><td>${esc(r.message)}</td></tr>`;
    }).join('') || '<tr><td colspan="7" class="dim">No message matches this filter.</td></tr>';
  }

  function radioPanel(m, rows) {
    if (!rows.length) return unavailable(m, 'team_radio', 'team radio clips');
    return `<div style="overflow:auto;max-height:340px"><table><thead><tr><th class="num">Time</th><th>Driver</th><th>Recording</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td class="num mono">${clock(r.time_s)}</td><td class="mono"><b>${esc(r.driver)}</b></td><td><a href="${esc(r.recording_url)}" target="_blank" rel="noopener noreferrer">open recording</a></td></tr>`).join('')}</tbody></table></div>
      <p class="hint">${rows.length} clip(s). OpenF1 gives each clip's driver and time but no transcript, so what was said is only in the recording. It also publishes only a limited selection of radio messages, and fewer in 2026. The audio is hosted by Formula 1's live-timing server and opens in a new tab; this app does not copy it.</p>`;
  }

  async function strategy(body, api, id, m, laps) {
    const [pit, rc, radio] = await Promise.all(['pit_stops', 'race_control', 'team_radio'].map((n) => getSet(api, id, n, 'limit=0')));
    body.innerHTML = `<div class="panel"><h2>Tyre strategy</h2>${stintStrip(laps, m.drivers || [])}</div>
      <div class="panel"><h2>Pit stops</h2>${pitTable(m, pit.rows, laps.some((l) => l.stint != null))}</div>
      <div class="panel"><h2>Race control</h2>${raceControlPanel(m, rc.rows)}</div>
      <div class="panel"><h2>Team radio</h2>${radioPanel(m, radio.rows)}</div>`;
    const drv = m.drivers || [];
    ['hrccat', 'hrcdrv'].forEach((i) => document.getElementById(i)?.addEventListener('change', () => fillRaceControl(rc.rows, drv)));
    fillRaceControl(rc.rows, drv);
  }

  return { coverage, race, strategy, dur, clock, stintStrip, unavailable };
})();
