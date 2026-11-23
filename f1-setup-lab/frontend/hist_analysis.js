/* Historical F1 phase 1: shared helpers for the analysis views (wind compass point, team colours, dash patterns for series that
   share a colour, driver headshot / coded-circle avatar). Uses Charts.esc for all text; declares no top-level globals other than
   HistAnalysis. The views (standings, season, mini-sectors, tyres, export) are added to this namespace by later tasks. */
const HistAnalysis = (() => {
  const esc = (s) => (typeof Charts !== 'undefined' ? Charts.esc(s) : String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const PALETTE = ['#e10600', '#4aa8ff', '#2ecc71', '#ffb400', '#c99bff', '#00cec9', '#ff7ab8', '#a3e635', '#fb923c', '#94a3b8'];
  const NEUTRAL = '#64748b';
  const SECTORS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  const HEX6 = /^[0-9A-Fa-f]{6}$/;

  /* Design D6: 45-degree sectors, wrapping at 360. Negative degrees are normalised first. Non-numbers give null. */
  const compass = (deg) => {
    if (deg === null || deg === undefined || deg === '' || !Number.isFinite(Number(deg))) return null;
    const d = ((Number(deg) % 360) + 360) % 360;
    return SECTORS[Math.floor((d + 22.5) / 45) % 8];
  };

  const cleanHex = (v) => {
    const s = String(v ?? '').replace(/^#/, '');
    return HEX6.test(s) ? `#${s.toUpperCase()}` : null;
  };

  /* driverOrTeam: a driver object, a driver code or a team name. meta: session manifest ({drivers:[...]}) or a drivers array.
     Returns "#RRGGBB" from the stored team_colour, else a palette colour by driver position, else neutral grey. */
  const teamColour = (driverOrTeam, meta) => {
    const list = Array.isArray(meta) ? meta : (meta && meta.drivers) || [];
    const key = driverOrTeam && typeof driverOrTeam === 'object' ? driverOrTeam.code : driverOrTeam;
    let d = driverOrTeam && typeof driverOrTeam === 'object' ? driverOrTeam : null;
    let idx = list.findIndex((x) => x.code === key);
    if (idx < 0 && key != null) idx = list.findIndex((x) => x.team === key);
    if (!d && idx >= 0) d = list[idx];
    const own = d && cleanHex(d.team_colour);
    if (own) return own;
    if (d && d.team) {
      const mate = list.find((x) => x.team === d.team && cleanHex(x.team_colour));
      if (mate) return cleanHex(mate.team_colour);
    }
    if (idx < 0 && d) idx = list.findIndex((x) => x.code === d.code);
    return idx >= 0 ? PALETTE[idx % PALETTE.length] : NEUTRAL;
  };

  /* seriesList: [{name, color, ...}]. Returns the same series with a dash property: the first series of a colour gets "0" (solid),
     later ones alternate "6 3", "2 3", "10 3 2 3". Series with a unique colour stay solid. Input is not mutated. */
  const DASHES = ['0', '6 3', '2 3', '10 3 2 3'];
  const distinguish = (seriesList) => {
    const seen = {};
    return (seriesList || []).map((s) => {
      const c = String(s.color || '').toLowerCase();
      const n = seen[c] || 0;
      seen[c] = n + 1;
      return { ...s, dash: DASHES[n % DASHES.length] };
    });
  };

  const initials = (d) => String((d && (d.code || d.name)) || '?').slice(0, 3).toUpperCase();
  const circle = (d, colour) => `<span class="avatar" role="img" aria-label="${esc(initials(d))}" style="display:inline-flex;align-items:center;justify-content:center;`
    + `width:32px;height:32px;border-radius:50%;background:${esc(colour)};color:#fff;font:600 11px/1 sans-serif">${esc(initials(d))}</span>`;

  /* 32 px headshot, or a coded circle when the URL is missing or fails to load (team colour, else neutral grey). */
  const avatar = (driver, meta) => {
    const colour = (driver && cleanHex(driver.team_colour)) || (driver && meta ? teamColour(driver, meta) : NEUTRAL);
    const url = driver && typeof driver.headshot_url === 'string' ? driver.headshot_url : '';
    if (!/^https:\/\/[^\s"'<>]+$/.test(url)) return circle(driver, colour);
    return `<img class="avatar" src="${esc(url)}" width="32" height="32" alt="${esc(initials(driver))}" referrerpolicy="no-referrer" loading="lazy" `
      + `style="border-radius:50%;object-fit:cover" data-fallback="${esc(circle(driver, colour))}" onerror="this.onerror=null;this.outerHTML=this.dataset.fallback">`;
  };

  /* ------------------------------------------------------------------ standings view (#/standings[/session_id]) */
  const NO_STANDINGS = 'Standings are published for race and sprint sessions only';
  const isRaceOrSprint = (s) => {
    const t = String(s.session_type || '').trim().toUpperCase();
    if (t === 'R' || t === 'S') return true;
    if (t) return t === 'RACE' || t === 'SPRINT';
    return /^(race|sprint)$/i.test(String(s.session || '').trim());
  };
  const num = (v) => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));
  const pts = (v) => (num(v) === null ? '&mdash;' : esc(String(Math.round(Number(v) * 100) / 100)));
  const gained = (r) => {
    const a = num(r.points_current), b = num(r.points_start);
    return a === null || b === null ? null : Math.round((a - b) * 100) / 100;
  };
  const byPosition = (rows) => [...rows].sort((a, b) => (num(a.position_current) === null) - (num(b.position_current) === null) || num(a.position_current) - num(b.position_current));

  /* The coverage note and status shown in place of a table that is not ok or has no rows. */
  const coverageNote = (label, ds) => {
    const c = ds && ds.coverage;
    const status = c && c.status ? String(c.status).replace('_', ' ') : 'not requested';
    const note = c && c.note ? c.note : (c ? 'No rows were returned for this session.' : 'This import did not request this dataset.');
    return `<div class="empty"><b>${esc(label)}</b> &middot; status: <span class="mono">${esc(status)}</span><br>${esc(note)}</div>`;
  };
  const usable = (ds) => !!ds && Array.isArray(ds.rows) && ds.rows.length > 0 && !!ds.coverage && ds.coverage.status === 'ok';

  const swatch = (colour) => `<span style="color:${colour}" aria-hidden="true">&#9632;</span> `;
  const driversTable = (rows, drivers) => {
    const byCode = {};
    drivers.forEach((d) => { byCode[d.code] = d; });
    return `<table><thead><tr><th class="num">Pos</th><th></th><th>Code</th><th>Team</th><th class="num">Points</th><th class="num">Gained</th></tr></thead><tbody>${
      byPosition(rows).map((r) => {
        const d = byCode[r.driver] || { code: r.driver };
        const g = gained(r);
        return `<tr><td class="num">${num(r.position_current) === null ? '&mdash;' : esc(r.position_current)}</td><td>${avatar(d, drivers)}</td>`
          + `<td class="mono">${swatch(teamColour(d, drivers))}<b>${esc(r.driver ?? '')}</b></td><td>${esc(d.team || '')}</td><td class="num">${pts(r.points_current)}</td>`
          + `<td class="num">${g === null ? '&mdash;' : `${g > 0 ? '+' : ''}${esc(g)}`}</td></tr>`;
      }).join('')}</tbody></table>`;
  };
  const teamsTable = (rows, drivers) => `<table><thead><tr><th class="num">Pos</th><th>Team</th><th class="num">Points</th></tr></thead><tbody>${
    byPosition(rows).map((r) => `<tr><td class="num">${num(r.position_current) === null ? '&mdash;' : esc(r.position_current)}</td><td>${swatch(teamColour(r.team, drivers))}${esc(r.team || '')}</td><td class="num">${pts(r.points_current)}</td></tr>`).join('')}</tbody></table>`;

  /* deps (all optional, default to the app.js globals): api, view, errorsBox. sessionId: from the route, optional. */
  async function viewStandings(sessionId, deps = {}) {
    const apiFn = deps.api || (typeof api === 'function' ? api : null);
    const viewEl = (deps.view || (typeof view === 'function' ? view : null))();
    const errBox = deps.errorsBox || (typeof errorsBox === 'function' ? errorsBox : (e) => `<div class="errors" role="alert">${esc((e && e.message) || e)}</div>`);
    viewEl.innerHTML = `<div class="row"><div><h1>Standings</h1><p class="sub">Drivers and teams championship after a race or sprint session, from the stored OpenF1 data.</p></div></div>
      <div id="stand-err"></div><div class="panel"><div class="field" style="max-width:480px"><label for="stand-pick">Session</label><select id="stand-pick" disabled><option>Loading&hellip;</option></select></div></div>
      <div id="stand-body"></div>`;
    const errEl = viewEl.querySelector('#stand-err'), pick = viewEl.querySelector('#stand-pick'), body = viewEl.querySelector('#stand-body');
    let seq = 0;
    const fail = (e) => { errEl.innerHTML = errBox(e); };
    let sessions = [];
    try { sessions = await apiFn('GET', '/api/historical/sessions'); } catch (e) { fail(e); pick.innerHTML = '<option value="">unavailable</option>'; return; }
    const eligible = sessions.filter(isRaceOrSprint);
    const label = (s) => `${s.year || ''} ${s.event || ''} \u00b7 ${s.session || s.session_type || ''}`.trim();
    if (!eligible.length) {
      pick.innerHTML = '<option value="">no race or sprint sessions</option>';
      body.innerHTML = `<div class="empty">No race or sprint sessions are stored yet. <a href="#/hist">Import a session</a>. ${sessions.length ? esc(NO_STANDINGS) : ''}</div>`;
      return;
    }
    // A qualifying/practice session id from the route is still shown with the fixed text rather than a table.
    const requested = sessionId ? sessions.find((s) => s.session_id === sessionId) : null;
    const options = requested && !isRaceOrSprint(requested) ? [...eligible, requested] : eligible;
    pick.disabled = false;
    pick.innerHTML = options.map((s) => `<option value="${esc(s.session_id)}">${esc(label(s))}</option>`).join('');
    pick.value = (requested || eligible[0]).session_id;

    const load = async () => {
      const my = ++seq, s = sessions.find((x) => x.session_id === pick.value);
      errEl.innerHTML = '';
      if (!s) { body.innerHTML = ''; return; }
      if (!isRaceOrSprint(s)) { body.innerHTML = `<div class="empty">${esc(NO_STANDINGS)}</div>`; return; }
      body.innerHTML = '<div class="dim">Loading&hellip;</div>';
      const base = `/api/historical/sessions/${encodeURIComponent(s.session_id)}`;
      try {
        const [dr, tm, drivers] = await Promise.all([
          apiFn('GET', `${base}/datasets/drivers_championship?limit=0`),
          apiFn('GET', `${base}/datasets/teams_championship?limit=0`),
          apiFn('GET', `${base}/drivers`)]);
        if (my !== seq) return;
        body.innerHTML = `<div class="grid g2"><div class="panel"><h2>Drivers</h2>${usable(dr) ? driversTable(dr.rows, drivers || []) : coverageNote('Drivers championship', dr)}</div>`
          + `<div class="panel"><h2>Teams</h2>${usable(tm) ? teamsTable(tm.rows, drivers || []) : coverageNote('Teams championship', tm)}</div></div>`;
      } catch (e) { if (my === seq) { body.innerHTML = ''; fail(e); } }
    };
    pick.addEventListener('change', () => { location.hash = `#/standings/${encodeURIComponent(pick.value)}`; });
    await load();
  }

  /* ------------------------------------------------------------------ wind direction (weather tab) */
  const NO_WIND = 'wind direction not published for this session';
  /* samples: the weather route's samples ({time_s, wind_direction_deg, ...}). Returns HTML. A null (or non-numeric) direction is
     left out of the line and shown as "no data" in the table, never as 0. When every sample is null: one note, no chart, no table. */
  const windDirection = (samples) => {
    const s = Array.isArray(samples) ? samples : [];
    const deg = (p) => num(p && p.wind_direction_deg);
    if (!s.length || s.every((p) => deg(p) === null)) return `<div class="note info">${esc(NO_WIND)}</div>`;
    const pts = s.map((p) => ({ x: p.time_s ?? 0, y: deg(p) })).filter((p) => p.y !== null);
    const chart = Charts.lines({ series: [{ name: 'Wind direction', color: '#c99bff', pts }], xLabel: 'session time (s)', yLabel: 'degrees', ymin: 0, ymax: 360 });
    const rows = s.map((p) => {
      const d = deg(p);
      const cell = d === null ? '<span class="dim">no data</span>' : `${esc(Math.round(d * 10) / 10)}&deg; ${esc(compass(d))}`;
      return `<tr><td class="num">${num(p.time_s) === null ? '&mdash;' : esc(p.time_s)}</td><td>${cell}</td></tr>`;
    }).join('');
    return `<div class="grid g2"><div class="panel"><h2>Wind direction</h2>${chart}</div>`
      + `<div class="panel"><h2>Wind direction by sample</h2><table><thead><tr><th class="num">Time (s)</th><th>Direction</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  };

  /* ------------------------------------------------------------------ shared SVG helpers for the mini-sector and tyre tabs */
  const GAIN = '#2ecc71', LOSS = '#ff4d4d', EVEN = '#5b6572';
  const r3 = (v) => Math.round(Number(v) * 1000) / 1000;
  const signed = (v) => `${v > 0 ? '+' : ''}${r3(v).toFixed(3)}`;
  const linScale = (d0, d1, r0, r1) => (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);
  const ticks = (lo, hi, n = 5) => {
    const raw = (hi - lo) / n || 1, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw, out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toPrecision(10));
    return out;
  };
  const getErr = (e) => `<div class="note bad" role="alert">${esc((e && e.message) || e)}</div>`;
  const histApi = (deps) => deps.api || (typeof api === 'function' ? api : null);

  /* One bar per slice. With a reference: delta_s, green when negative (gain), red when positive (loss). Without: time_s in grey.
     Each bar carries its value as text (rotated), so colour is never the only carrier. */
  const sliceBars = (slices, hasRef) => {
    const W = 900, H = 340, m = { l: 62, r: 14, t: 14, b: 40 };
    const val = (s) => (hasRef ? s.delta_s : s.time_s);
    const vs = slices.map(val).filter((v) => num(v) !== null).map(Number);
    if (!vs.length) return '<div class="empty">No slice values.</div>';
    const lo = Math.min(0, ...vs), hi = Math.max(0, ...vs), pad = (hi - lo) * 0.18 || 1;
    const Y = linScale(lo - (hasRef || lo < 0 ? pad : 0), hi + pad, H - m.b, m.t);
    const bw = (W - m.l - m.r) / slices.length, y0 = Y(0);
    let b = '';
    ticks(lo - pad, hi + pad).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${esc(t)}</text>`; });
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${y0}" y2="${y0}"/>`;
    slices.forEach((s, i) => {
      const v = num(val(s));
      const x = m.l + i * bw + bw * 0.12, w = bw * 0.76;
      if (v === null) { b += `<text x="${x + w / 2}" y="${y0 - 4}" text-anchor="middle" style="font-size:9px">n/a</text>`; return; }
      const colour = !hasRef || v === 0 ? EVEN : (v < 0 ? GAIN : LOSS);
      const y = Math.min(Y(v), y0), h = Math.max(Math.abs(Y(v) - y0), 1);
      const label = hasRef ? signed(v) : r3(v).toFixed(3);
      const tip = `Slice ${s.index + 1}: ${hasRef ? `${signed(v)} s (${v < 0 ? 'gain' : v > 0 ? 'loss' : 'level'})` : `${label} s`}`;
      b += `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${colour}" opacity=".9"><title>${esc(tip)}</title></rect>`;
      const tx = x + w / 2, up = v >= 0 || !hasRef;
      b += `<text transform="translate(${tx + 3} ${up ? y - 4 : y + h + 4}) rotate(-90)" text-anchor="${up ? 'start' : 'end'}" style="font-size:9px;fill:#e6e9ee">${esc(label)}</text>`;
      if (slices.length <= 30 || i % 2 === 0) b += `<text x="${tx}" y="${H - m.b + 14}" text-anchor="middle" style="font-size:9px">${esc(s.index + 1)}</text>`;
    });
    b += `<text x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">slice (start to finish)</text>`
      + `<text transform="translate(14 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">${hasRef ? 'delta vs reference (s)' : 'slice time (s)'}</text>`;
    const legend = hasRef ? `<div class="legend"><span><i style="background:${GAIN}"></i>gain (negative, faster than reference)</span><span><i style="background:${LOSS}"></i>loss (positive, slower)</span></div>` : '';
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="mini-sector bars">${b}</svg>${legend}`;
  };

  /* Track outline from position points, each segment coloured by the slice it falls in (fraction of lap by distance_m, else by index). */
  const sliceTrack = (points, slices, hasRef) => {
    const raw = (points || []).filter((p) => num(p.x_m) !== null && num(p.y_m) !== null);
    if (raw.length < 2) return '';
    const step = Math.max(1, Math.ceil(raw.length / 900));
    const ds = raw.filter((_, i) => i % step === 0 || i === raw.length - 1);
    const dist = ds.map((p) => num(p.distance_m));
    const useDist = dist.every((d) => d !== null) && dist[dist.length - 1] > dist[0];
    const frac = (i) => (useDist ? (dist[i] - dist[0]) / (dist[dist.length - 1] - dist[0]) : i / (ds.length - 1));
    const sliceAt = (f) => slices.find((s) => f >= s.start_frac && f < s.end_frac) || slices[slices.length - 1];
    const W = 660, H = 460, m = 24;
    const xs = ds.map((p) => p.x_m), ys = ds.map((p) => p.y_m);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const sc = Math.min((W - 2 * m) / Math.max(x1 - x0, 1e-6), (H - 2 * m) / Math.max(y1 - y0, 1e-6));
    const ox = m + ((W - 2 * m) - (x1 - x0) * sc) / 2, oy = m + ((H - 2 * m) - (y1 - y0) * sc) / 2;
    const X = (p) => ox + (p.x_m - x0) * sc, Y = (p) => oy + (y1 - p.y_m) * sc;
    let b = '';
    for (let i = 0; i < ds.length - 1; i++) {
      const s = sliceAt(frac(i)), v = hasRef ? num(s.delta_s) : null;
      const colour = v === null || v === 0 ? EVEN : (v < 0 ? GAIN : LOSS);
      const tip = `Slice ${s.index + 1}${v === null ? '' : `: ${signed(v)} s (${v < 0 ? 'gain' : v > 0 ? 'loss' : 'level'})`}`;
      b += `<line x1="${X(ds[i])}" y1="${Y(ds[i])}" x2="${X(ds[i + 1])}" y2="${Y(ds[i + 1])}" stroke="${colour}" stroke-width="4" stroke-linecap="round" opacity=".9"><title>${esc(tip)}</title></line>`;
    }
    // Slice numbers at each slice start so the map can be read against the bars.
    slices.forEach((s) => {
      if (slices.length > 30 && s.index % 5) return;
      let k = 0;
      for (let i = 0; i < ds.length; i++) { if (frac(i) >= s.start_frac) { k = i; break; } }
      b += `<text x="${X(ds[k]) + 5}" y="${Y(ds[k]) - 5}" style="font-size:9px;fill:#e6e9ee">${esc(s.index + 1)}</text>`;
    });
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="track map shaded by mini-sector">${b}</svg>`;
  };

  const driverOptions = (drivers, sel, blank) => (blank ? `<option value="">${esc(blank)}</option>` : '')
    + drivers.map((d) => `<option value="${esc(d.code)}"${d.code === sel ? ' selected' : ''}>${esc(d.code)}${d.name ? ` &middot; ${esc(d.name)}` : ''}</option>`).join('');

  /* ------------------------------------------------------------------ Mini-sectors tab (hist session tab "minisectors") */
  /* body: the tab container element. meta: session manifest ({drivers}). deps.api optional. Returns a promise. */
  async function miniSectorsTab(body, apiArg, id, meta) {
    const apiFn = typeof apiArg === 'function' ? apiArg : histApi({});
    const drivers = (meta && meta.drivers) || [];
    const base = `/api/historical/sessions/${encodeURIComponent(id)}`;
    if (!drivers.length) { body.innerHTML = '<div class="empty">This session has no drivers stored.</div>'; return; }
    body.innerHTML = `<div class="panel"><div class="grid g3">
      <div class="field"><label for="msdrv">Driver</label><select id="msdrv">${driverOptions(drivers, drivers[0].code)}</select></div>
      <div class="field"><label for="mslap">Lap (optional)</label><input id="mslap" type="number" min="1" placeholder="fastest valid lap"></div>
      <div class="field"><label for="msref">Reference driver</label><select id="msref">${driverOptions(drivers, '', 'no reference')}</select></div>
      <div class="field"><label for="msreflap">Reference lap (optional)</label><input id="msreflap" type="number" min="1" placeholder="fastest valid lap"></div>
      <div class="field"><label for="mscount">Slices (10 to 50)</label><input id="mscount" type="number" min="10" max="50" value="25"></div></div>
      <button class="btn primary" id="msgo" type="button">Analyse</button></div><div id="msout"></div>`;
    const q = (sel) => body.querySelector(sel), out = q('#msout');
    let seq = 0;
    const run = async () => {
      const my = ++seq;
      const params = new URLSearchParams({ driver: q('#msdrv').value, count: String(q('#mscount').value || 25) });
      if (q('#mslap').value) params.set('lap', q('#mslap').value);
      if (q('#msref').value) {
        params.set('ref_driver', q('#msref').value);
        if (q('#msreflap').value) params.set('ref_lap', q('#msreflap').value);
      }
      out.innerHTML = '<div class="dim">Loading&hellip;</div>';
      let r;
      try { r = await apiFn('GET', `${base}/mini-sectors?${params}`); } catch (e) { if (my === seq) out.innerHTML = getErr(e); return; }
      if (my !== seq) return;
      const slices = r.slices || [], hasRef = slices.some((s) => s.delta_s !== undefined && s.delta_s !== null);
      let track = '';
      try {
        const lapQ = r.lap != null ? `&lap=${encodeURIComponent(r.lap)}` : '';
        const tm = await apiFn('GET', `${base}/track-map?driver=${encodeURIComponent(r.driver || q('#msdrv').value)}${lapQ}`);
        track = sliceTrack(tm && tm.points, slices, hasRef);
      } catch (e) { track = ''; }
      if (my !== seq) return;
      const lastCum = slices.length ? num(slices[slices.length - 1].cum_delta_s) : null;
      out.innerHTML = `<div class="note info">${esc(r.distance_note || 'Distance is an estimate (speed integrated within each lap).')}</div>
        <div class="kpis"><div class="kpi"><b>${esc(r.driver ?? '')} lap ${esc(r.lap ?? '')}</b><span>${r.lap_source === 'requested' ? 'requested lap' : 'fastest valid lap'}</span></div>
        <div class="kpi"><b>${num(r.lap_time_s) === null ? '&mdash;' : esc(r3(r.lap_time_s).toFixed(3))}</b><span>telemetry lap time (s)</span></div>
        <div class="kpi"><b>${num(r.sum_slices_s) === null ? '&mdash;' : esc(r3(r.sum_slices_s).toFixed(3))}</b><span>sum of slices (s)</span></div>
        ${hasRef && lastCum !== null ? `<div class="kpi"><b>${esc(signed(lastCum))}</b><span>total delta vs reference (s)</span></div>` : ''}</div>
        <div class="grid g2"><div class="panel"><h2>${hasRef ? 'Gain or loss per slice' : 'Time per slice'}</h2>${sliceBars(slices, hasRef)}</div>
        <div class="panel"><h2>Track map by slice</h2>${track || '<div class="empty">No track position data for this driver and lap.</div>'}</div></div>`;
    };
    q('#msgo').addEventListener('click', run);
    await run();
  }

  /* ------------------------------------------------------------------ Tyres tab (hist session tab "tyres") */
  const CAVEAT = 'Fuel burn is not corrected; a negative slope can be caused by falling fuel load.';
  const cell = (v, d) => (num(v) === null ? '<span class="dim">&mdash;</span>' : esc(d === undefined ? v : r3(v).toFixed(d)));

  /* groups: [{key, label, colour, pts:[{x,y}], slope}] -> scatter of lap time vs tyre age with one fitted line per group. */
  const tyreScatter = (groups) => {
    const all = groups.flatMap((g) => g.pts);
    if (!all.length) return '<div class="empty">No lap points to plot for this selection.</div>';
    const W = 640, H = 340, m = { l: 62, r: 18, t: 14, b: 44 };
    const xl = Math.min(...all.map((p) => p.x)), xh = Math.max(...all.map((p) => p.x));
    const yl = Math.min(...all.map((p) => p.y)), yh = Math.max(...all.map((p) => p.y));
    const px = (xh - xl) * 0.06 || 1, py = (yh - yl) * 0.08 || 1;
    const X = linScale(xl - px, xh + px, m.l, W - m.r), Y = linScale(yl - py, yh + py, H - m.b, m.t);
    let b = '';
    ticks(yl - py, yh + py).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${esc(t)}</text>`; });
    ticks(xl - px, xh + px, 7).forEach((t) => { b += `<line class="grid" y1="${m.t}" y2="${H - m.b}" x1="${X(t)}" x2="${X(t)}"/><text x="${X(t)}" y="${H - m.b + 16}" text-anchor="middle">${esc(t)}</text>`; });
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><line class="axis" x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${H - m.b}"/>`
      + `<text x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">tyre age (laps)</text>`
      + `<text transform="translate(14 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">lap time (s)</text>`;
    groups.forEach((g, gi) => {
      g.pts.forEach((p) => { b += `<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="3.5" fill="${g.colour}" opacity=".85"><title>${esc(g.label)} age ${esc(p.x)}: ${esc(r3(p.y).toFixed(3))} s</title></circle>`; });
      if (g.fit) b += `<line x1="${X(g.fit.x0)}" y1="${Y(g.fit.y0)}" x2="${X(g.fit.x1)}" y2="${Y(g.fit.y1)}" stroke="${g.colour}" stroke-width="2" stroke-dasharray="${g.dash || '0'}"><title>${esc(g.label)} fit: ${esc(signed(g.slope))} s/lap</title></line>`;
    });
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="lap time against tyre age">${b}</svg>`
      + `<div class="legend">${groups.map((g, gi) => `<span><i style="background:${g.colour}"></i>${esc(g.label)}${g.fit ? ` (fit ${esc(signed(g.slope))} s/lap, dash ${esc(g.dash || '0')})` : ' (no fit)'}</span>`).join('')}</div>`;
  };

  async function tyresTab(body, apiArg, id, meta) {
    const apiFn = typeof apiArg === 'function' ? apiArg : histApi({});
    const base = `/api/historical/sessions/${encodeURIComponent(id)}`;
    body.innerHTML = '<div class="dim">Loading&hellip;</div>';
    let r, laps;
    try {
      [r, laps] = await Promise.all([apiFn('GET', `${base}/tyre-degradation`), apiFn('GET', `${base}/laps`)]);
    } catch (e) { body.innerHTML = getErr(e); return; }
    const stints = (r && r.stints) || [];
    const caveat = `<div class="note info" id="tyre-caveat"><b>Fuel caveat:</b> ${esc((r && r.caveat) || CAVEAT)}</div>`;
    if (!stints.length) {
      body.innerHTML = `${caveat}<div class="empty">No tyre stint data is stored for this session.${r && r.coverage_note ? `<br>${esc(r.coverage_note)}` : ''}</div>`;
      return;
    }
    const drivers = [...new Set(stints.map((s) => s.driver))];
    body.innerHTML = `${caveat}<div class="panel"><div class="field" style="max-width:260px"><label for="tydrv">Driver (scatter)</label><select id="tydrv">`
      + `${drivers.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('')}<option value="">all drivers</option></select></div><div id="typlot"></div></div>
      <div class="panel"><h2>Degradation by stint</h2><div style="overflow:auto"><table><thead><tr><th>Driver</th><th class="num">Stint</th><th>Compound</th><th class="num">First age</th><th class="num">Last age</th><th class="num">Laps used</th><th>Excluded</th><th class="num">Slope (s/lap)</th><th class="num">R&sup2;</th><th>Note</th></tr></thead><tbody>`
      + stints.map((s) => {
        const ex = s.laps_excluded || {};
        const exTxt = Object.keys(ex).map((k) => `${k.replace('_', ' ')}: ${ex[k]}`).join(', ');
        return `<tr><td class="mono">${swatch(teamColour(s.driver, meta))}<b>${esc(s.driver)}</b></td><td class="num">${cell(s.stint)}</td><td>${esc(s.compound || '')}</td><td class="num">${cell(s.tyre_age_first)}</td><td class="num">${cell(s.tyre_age_last)}</td>`
          + `<td class="num">${cell(s.laps_used)}</td><td class="dim">${esc(exTxt)}</td><td class="num">${num(s.slope_s_per_lap) === null ? '<span class="dim">&mdash;</span>' : esc(signed(s.slope_s_per_lap))}</td>`
          + `<td class="num">${cell(s.r_squared, 3)}</td><td class="dim">${esc(s.reason || '')}</td></tr>`;
      }).join('')
      + `</tbody></table></div></div>`
      + `<div class="panel"><h2>By compound</h2><table><thead><tr><th>Compound</th><th class="num">Median slope (s/lap)</th><th class="num">Stints</th></tr></thead><tbody>${
        ((r && r.by_compound) || []).map((c) => `<tr><td>${esc(c.compound || '')}</td><td class="num">${num(c.median_slope_s_per_lap) === null ? '<span class="dim">&mdash;</span>' : esc(signed(c.median_slope_s_per_lap))}</td><td class="num">${cell(c.stints)}</td></tr>`).join('')
        || '<tr><td colspan="3" class="dim">No compound summary.</td></tr>'}</tbody></table></div>`;
    const plot = body.querySelector('#typlot'), pick = body.querySelector('#tydrv');
    const draw = () => {
      const want = pick.value;
      const shown = stints.filter((s) => !want || s.driver === want);
      const groups = shown.map((s, i) => {
        const pts = (laps || []).filter((l) => l.driver === s.driver && l.stint === s.stint && num(l.lap_time_s) !== null && num(l.tyre_life) !== null
          && l.is_valid !== false && !l.is_deleted && !l.pits).map((l) => ({ x: Number(l.tyre_life), y: Number(l.lap_time_s) }));
        const colour = teamColour(s.driver, meta);
        const label = `${s.driver} stint ${s.stint ?? '?'}${s.compound ? ` ${s.compound}` : ''}`;
        const slope = num(s.slope_s_per_lap);
        let fit = null;
        if (slope !== null && pts.length >= 2) {
          // The route returns the slope, not the intercept: draw the slope through the centroid of the plotted laps.
          const mx = pts.reduce((a, p) => a + p.x, 0) / pts.length, my = pts.reduce((a, p) => a + p.y, 0) / pts.length;
          const x0 = Math.min(...pts.map((p) => p.x)), x1 = Math.max(...pts.map((p) => p.x));
          fit = { x0, x1, y0: my + slope * (x0 - mx), y1: my + slope * (x1 - mx) };
        }
        return { key: i, label, colour, pts, slope, fit };
      });
      // Same-coloured stints (a driver's own stints, team mates) are told apart by dash pattern via distinguish().
      const dist = distinguish(groups.map((g) => ({ color: g.colour })));
      groups.forEach((g, i) => { g.dash = dist[i].dash; });
      plot.innerHTML = tyreScatter(groups);
    };
    pick.addEventListener('change', draw);
    draw();
  }

  /* ------------------------------------------------------------------ Season comparison (#/season) */
  const METRICS = [['best_lap', 'Best lap (s)'], ['median_lap', 'Median lap (s)'], ['finish_position', 'Finish position'],
    ['grid_position', 'Grid position'], ['points', 'Points']];
  const MIX_WARNING = 'The selected sessions have different session types. Tick "Allow mixing session types" to compare them in one series, or pick sessions of one type.';
  const typeOf = (s) => String((s && s.session_type) || '').trim() || 'unknown';
  const shortLabel = (s) => `${s.year || ''} ${s.event || ''} ${typeOf(s)}`.replace(/\s+/g, ' ').trim();

  /* result: the compare-sessions response ({metric, drivers, sessions:[{id,label,session_type,...}], cells:[{session_id,driver,value,reason}]}).
     Returns {svg, reasons, series}. One point per session in the response order (start time ascending); a null cell is a gap in the line
     (the path restarts after it) and is never drawn as 0. Every point label carries the session type; hover titles carry the reason. */
  const compareChart = (result, meta) => {
    const sessions = (result && result.sessions) || [], cells = (result && result.cells) || [];
    const codes = (result && result.drivers) || [];
    const metricName = (METRICS.find((m) => m[0] === (result && result.metric)) || [0, result && result.metric])[1];
    const cellOf = (sid, code) => cells.find((c) => c.session_id === sid && c.driver === code) || { value: null, reason: 'no data returned' };
    const series = distinguish(codes.map((code) => ({ code, color: teamColour(code, meta) })));
    const reasons = [];
    const vals = [];
    series.forEach((s) => {
      s.points = sessions.map((ss, i) => {
        const c = cellOf(ss.id, s.code), v = num(c.value);
        if (v === null) reasons.push({ driver: s.code, session: ss.label || shortLabel(ss), session_type: typeOf(ss), reason: c.reason || 'no value' });
        else vals.push(v);
        return { i, v, ss, reason: c.reason };
      });
    });
    if (!sessions.length) return { svg: '<div class="empty">No sessions returned.</div>', reasons, series };
    const W = 900, H = 380, m = { l: 62, r: 16, t: 14, b: 96 };
    const lo = vals.length ? Math.min(...vals) : 0, hi = vals.length ? Math.max(...vals) : 1, pad = (hi - lo) * 0.1 || 1;
    const X = (i) => (sessions.length === 1 ? (m.l + W - m.r) / 2 : linScale(0, sessions.length - 1, m.l + 10, W - m.r - 10)(i));
    const Y = linScale(lo - pad, hi + pad, H - m.b, m.t);
    let b = '';
    ticks(lo - pad, hi + pad).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${esc(t)}</text>`; });
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/>`
      + `<text transform="translate(14 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">${esc(metricName)}</text>`;
    sessions.forEach((ss, i) => {
      const lab = `${shortLabel(ss)}`;
      b += `<text transform="translate(${X(i) + 3} ${H - m.b + 10}) rotate(60)" text-anchor="start" style="font-size:9px"><title>${esc(ss.label || lab)}</title>${esc(lab.length > 26 ? `${lab.slice(0, 25)}\u2026` : lab)}</text>`;
    });
    series.forEach((s) => {
      // Split on gaps: a new "M" starts after every null cell.
      const segs = []; let cur = [];
      s.points.forEach((p) => { if (p.v === null) { if (cur.length) segs.push(cur); cur = []; } else cur.push(p); });
      if (cur.length) segs.push(cur);
      segs.forEach((seg) => { b += `<path d="${seg.map((p, k) => `${k ? 'L' : 'M'}${X(p.i)} ${Y(p.v)}`).join('')}" fill="none" stroke="${s.color}" stroke-width="2" stroke-dasharray="${s.dash}"/>`; });
      s.points.forEach((p) => {
        if (p.v === null) {
          b += `<text x="${X(p.i)}" y="${H - m.b - 6}" text-anchor="middle" style="font-size:10px"><title>${esc(`${s.code} \u00b7 ${shortLabel(p.ss)}: no value (${p.reason || 'no reason given'})`)}</title>\u00d7</text>`;
        } else {
          b += `<circle cx="${X(p.i)}" cy="${Y(p.v)}" r="4" fill="${s.color}"><title>${esc(`${s.code} \u00b7 ${shortLabel(p.ss)} (${typeOf(p.ss)}): ${r3(p.v)}`)}</title></circle>`;
        }
      });
    });
    const legend = `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.code)}${s.dash !== '0' ? ` (dash ${esc(s.dash)})` : ''}</span>`).join('')}<span>&times; = no value</span></div>`;
    return { svg: `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(metricName)} per session">${b}</svg>${legend}`, reasons, series };
  };

  const reasonList = (reasons) => (reasons.length
    ? `<h2>Missing values</h2><ul id="cmp-reasons">${reasons.map((r) => `<li><b class="mono">${esc(r.driver)}</b> &middot; ${esc(r.session)} (${esc(r.session_type)}): ${esc(r.reason)}</li>`).join('')}</ul>`
    : '');

  /* deps (optional): api, view, errorsBox. Sessions and drivers come from the stored list; nothing is imported. */
  async function viewSeason(deps = {}) {
    const apiFn = deps.api || (typeof api === 'function' ? api : null);
    const viewEl = (deps.view || (typeof view === 'function' ? view : null))();
    const errBox = deps.errorsBox || (typeof errorsBox === 'function' ? errorsBox : getErr);
    viewEl.innerHTML = `<div class="row"><div><h1>Season comparison</h1><p class="sub">One metric for up to 6 drivers across 2 to 24 stored sessions. Missing values are gaps with a reason, never 0.</p></div></div>
      <div id="cmp-err"></div><div class="panel"><div class="grid g3">
      <div class="field"><label for="cmp-sessions">Sessions (2 to 24)</label><select id="cmp-sessions" multiple size="10"><option>Loading&hellip;</option></select></div>
      <div class="field"><label for="cmp-drivers">Drivers (1 to 6)</label><select id="cmp-drivers" multiple size="10" disabled></select></div>
      <div><div class="field"><label for="cmp-metric">Metric</label><select id="cmp-metric">${METRICS.map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join('')}</select></div>
      <label class="dim"><input type="checkbox" id="cmp-mix"> Allow mixing session types</label></div></div>
      <button class="btn primary" id="cmp-go" type="button" disabled>Compare</button></div><div id="cmp-note"></div><div id="cmp-out"></div>`;
    const q = (s) => viewEl.querySelector(s);
    const errEl = q('#cmp-err'), selS = q('#cmp-sessions'), selD = q('#cmp-drivers'), go = q('#cmp-go'), note = q('#cmp-note'), out = q('#cmp-out');
    let sessions = [], seq = 0, dseq = 0, driverMeta = [];
    try { sessions = await apiFn('GET', '/api/historical/sessions'); } catch (e) { errEl.innerHTML = errBox(e); selS.innerHTML = ''; return; }
    if (!sessions.length) { selS.innerHTML = ''; out.innerHTML = '<div class="empty">No sessions are stored yet. <a href="#/hist">Import a session</a>.</div>'; return; }
    sessions = [...sessions].sort((a, b) => String(a.session_date || '').localeCompare(String(b.session_date || '')));
    selS.innerHTML = sessions.map((s) => `<option value="${esc(s.session_id)}">${esc(shortLabel(s))}</option>`).join('');
    const chosen = () => [...selS.selectedOptions].map((o) => o.value);
    const chosenDrivers = () => [...selD.selectedOptions].map((o) => o.value);
    const picked = () => sessions.filter((s) => chosen().includes(s.session_id));
    const mixed = () => new Set(picked().map(typeOf)).size > 1;
    const validate = () => {
      const ns = chosen().length, nd = chosenDrivers().length, msgs = [];
      if (ns < 2 || ns > 24) msgs.push('Select 2 to 24 sessions.');
      if (nd < 1 || nd > 6) msgs.push('Select 1 to 6 drivers.');
      if (mixed() && !q('#cmp-mix').checked) msgs.push(MIX_WARNING);
      note.innerHTML = msgs.length && (ns || nd) ? `<div class="note info" role="status">${msgs.map(esc).join('<br>')}</div>` : '';
      go.disabled = msgs.length > 0;
      return msgs.length === 0;
    };
    const loadDrivers = async () => {
      const my = ++dseq, keep = new Set(chosenDrivers()), ids = chosen();
      if (!ids.length) { selD.innerHTML = ''; selD.disabled = true; validate(); return; }
      const lists = await Promise.all(ids.map((id) => apiFn('GET', `/api/historical/sessions/${encodeURIComponent(id)}/drivers`).catch(() => [])));
      if (my !== dseq) return;
      const byCode = {};
      lists.flat().forEach((d) => { if (d && d.code && !byCode[d.code]) byCode[d.code] = d; });
      driverMeta = Object.values(byCode);
      selD.disabled = false;
      selD.innerHTML = Object.values(byCode).sort((a, b) => a.code.localeCompare(b.code))
        .map((d) => `<option value="${esc(d.code)}"${keep.has(d.code) ? ' selected' : ''}>${esc(d.code)}${d.name ? ` \u00b7 ${esc(d.name)}` : ''}</option>`).join('');
      validate();
    };
    selS.addEventListener('change', loadDrivers);
    selD.addEventListener('change', validate);
    q('#cmp-mix').addEventListener('change', validate);
    go.addEventListener('click', async () => {
      if (!validate()) return;
      const my = ++seq;
      errEl.innerHTML = ''; out.innerHTML = '<div class="dim">Loading&hellip;</div>';
      try {
        const r = await apiFn('POST', '/api/historical/compare-sessions', { sessions: chosen(), drivers: chosenDrivers(), metric: q('#cmp-metric').value });
        if (my !== seq) return;
        const meta = { drivers: (r.drivers || []).map((code) => driverMeta.find((d) => d.code === code) || { code }) };
        const c = compareChart(r, meta);
        out.innerHTML = `<div class="panel">${c.svg}${reasonList(c.reasons)}</div>`;
      } catch (e) { if (my === seq) { out.innerHTML = ''; errEl.innerHTML = errBox(e); } }
    });
  }

  /* ------------------------------------------------------------------ Export buttons (session header) */
  const EXPORT_TABLES = [['laps', 'Laps'], ['results', 'Results'], ['standings', 'Standings'], ['weather', 'Weather'],
    ['tyre_degradation', 'Tyre degradation'], ['mini_sectors', 'Mini-sectors']];
  const MS_HINT = 'Pick a driver to enable the mini-sectors CSV.';

  /* Returns HTML for the export buttons of one session. opts.drivers: [{code,name}] for the mini-sectors picker. opts.container: when
     given, the HTML is written into it and bound. Otherwise the caller inserts the HTML and calls HistAnalysis.bindExport(root).
     Task 7.7 hook: call exportButtons(sessionId, {drivers: meta.drivers, container}) from the session header render. */
  const exportButtons = (sessionId, opts = {}) => {
    const base = `/api/historical/sessions/${encodeURIComponent(sessionId)}`;
    const drivers = opts.drivers || [];
    const links = EXPORT_TABLES.map(([t, l]) => (t === 'mini_sectors'
      ? `<a class="btn" data-ms-link data-base="${esc(`${base}/export.csv?table=mini_sectors&driver=`)}" aria-disabled="true" title="${esc(MS_HINT)}">${esc(l)} CSV</a>`
      : `<a class="btn" href="${esc(`${base}/export.csv?table=${t}`)}" download>${esc(l)} CSV</a>`)).join(' ');
    const picker = drivers.length
      ? `<select data-ms-driver aria-label="Mini-sectors driver">${driverOptions(drivers, '', 'driver\u2026')}</select>`
      : '';
    const hint = `<span class="dim" data-ms-hint>${esc(drivers.length ? MS_HINT : 'Mini-sectors CSV needs a driver; none are stored for this session.')}</span>`;
    const html = `<div class="export-buttons" data-export-session="${esc(sessionId)}">${links} ${picker} ${hint} `
      + `<a class="btn primary" href="${esc(`${base}/report.html`)}" target="_blank" rel="noopener">Open report</a></div>`;
    if (opts.container) { opts.container.innerHTML = html; bindExport(opts.container); }
    return html;
  };

  /* Enables the mini-sectors link once a driver is chosen. Safe to call more than once. */
  const bindExport = (root) => {
    const pick = root && root.querySelector && root.querySelector('[data-ms-driver]');
    const link = root && root.querySelector && root.querySelector('[data-ms-link]');
    if (!link) return;
    const sync = () => {
      const code = pick ? pick.value : '';
      if (code) {
        link.setAttribute('href', link.dataset.base + encodeURIComponent(code));
        link.setAttribute('download', '');
        link.removeAttribute('aria-disabled');
        link.title = 'Download mini-sectors CSV';
      } else {
        link.removeAttribute('href');
        link.removeAttribute('download');
        link.setAttribute('aria-disabled', 'true');
        link.title = MS_HINT;
      }
    };
    if (pick) pick.addEventListener('change', sync);
    sync();
  };

  const exported = { compass, teamColour, distinguish, avatar, PALETTE, viewStandings, windDirection, miniSectorsTab, tyresTab,
    viewSeason, compareChart, exportButtons, bindExport };
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  return exported;
})();
