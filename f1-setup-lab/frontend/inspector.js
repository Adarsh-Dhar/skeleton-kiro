/* Telemetry inspector for archived (SIM-RECORDED) stint telemetry.
   - works on a copy of the measured samples; the stored data is never modified
   - charts draw min/max-bucket downsampled points; the readout always shows the nearest MEASURED sample
   - channels without any value in the recording are listed as missing (never drawn as zeros)
   - two-stint comparison is plotted against distance derived by integrating measured speed over time
     (the stored UDP samples carry no lap-distance field), and is labelled as such. */
const Inspector = (() => {
  const esc = Charts.esc, f = Charts.f;
  const WHEELS = ['RL', 'RR', 'FL', 'FR'];
  const CHANNELS = [
    { key: 'speed_kph', label: 'Speed', unit: 'km/h', get: (s) => s.speed_kph },
    { key: 'throttle', label: 'Throttle', unit: '%', get: (s) => (s.throttle == null ? null : s.throttle * 100) },
    { key: 'brake', label: 'Brake', unit: '%', get: (s) => (s.brake == null ? null : s.brake * 100) },
    { key: 'steer', label: 'Steering', unit: '% (left -, right +)', get: (s) => (s.steer == null ? null : s.steer * 100) },
    { key: 'rpm', label: 'Engine RPM', unit: 'rpm', get: (s) => s.rpm },
    { key: 'gear', label: 'Gear', unit: 'gear', get: (s) => s.gear },
    { key: 'clutch', label: 'Clutch', unit: '%', get: (s) => s.clutch },
    { key: 'drs', label: 'DRS', unit: 'state', get: (s) => s.drs },
    { key: 'rev_lights_percent', label: 'Rev lights', unit: '%', get: (s) => s.rev_lights_percent },
    { key: 'engine_temperature_c', label: 'Engine temperature', unit: '°C', get: (s) => s.engine_temperature_c },
    { key: 'tyre_surface_temperature_c', label: 'Tyre surface temperature', unit: '°C', wheels: true },
    { key: 'tyre_inner_temperature_c', label: 'Tyre inner temperature', unit: '°C', wheels: true },
    { key: 'tyre_pressure_psi', label: 'Tyre pressure', unit: 'psi', wheels: true },
    { key: 'brake_temperature_c', label: 'Brake temperature', unit: '°C', wheels: true },
  ];
  const DEFAULT_ON = ['speed_kph', 'throttle', 'brake', 'steer'];
  const COLORS = { a: '#e10600', b: '#4aa8ff', wheels: ['#ff9f43', '#e17055', '#00cec9', '#c99bff'] };
  const MAX_DRAWN = 600;
  const finite = (v) => v !== null && v !== undefined && Number.isFinite(v);

  /** sorted copy + derived distance (m) by trapezoid integration of measured speed */
  function prepare(samples) {
    const s = samples.map((x) => ({ ...x })).sort((p, q) => p.session_time - q.session_time);
    let d = 0;
    s.forEach((x, i) => {
      if (i) {
        const p = s[i - 1], dt = x.session_time - p.session_time;
        if (dt > 0 && dt < 2 && finite(x.speed_kph) && finite(p.speed_kph)) d += ((x.speed_kph + p.speed_kph) / 2 / 3.6) * dt;
      }
      x._dist = d;
    });
    return s;
  }

  function availability(samples) {
    const out = {};
    CHANNELS.forEach((c) => {
      out[c.key] = c.wheels ? samples.some((s) => Array.isArray(s[c.key]) && s[c.key].some(finite)) : samples.some((s) => finite(c.get(s)));
    });
    return out;
  }

  /** values of one channel as series [{name,color,pts}] for samples, x from xOf */
  function seriesOf(c, samples, xOf, color, tag) {
    if (c.wheels) {
      return WHEELS.map((w, i) => ({ name: `${tag}${w}`, color: tag ? color : COLORS.wheels[i], dash: tag && i ? '4 3' : '',
        pts: samples.map((s) => ({ x: xOf(s), y: Array.isArray(s[c.key]) ? s[c.key][i] : null })).filter((p) => finite(p.y)) }));
    }
    return [{ name: tag || c.label, color, pts: samples.map((s) => ({ x: xOf(s), y: c.get(s) })).filter((p) => finite(p.y)) }];
  }

  /** min/max bucket downsampling (keeps peaks); returns {pts, measured} */
  function thin(pts, max = MAX_DRAWN) {
    if (pts.length <= max) return pts;
    const per = Math.ceil(pts.length / (max / 2)), out = [];
    for (let i = 0; i < pts.length; i += per) {
      const b = pts.slice(i, i + per);
      let lo = b[0], hi = b[0];
      b.forEach((p) => { if (p.y < lo.y) lo = p; if (p.y > hi.y) hi = p; });
      out.push(...(lo.x <= hi.x ? [lo, hi] : [hi, lo]));
    }
    return out;
  }

  function nearest(samples, x, xOf) {
    let lo = 0, hi = samples.length - 1;
    if (hi < 0) return null;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xOf(samples[mid]) < x) lo = mid; else hi = mid; }
    return Math.abs(xOf(samples[lo]) - x) <= Math.abs(xOf(samples[hi]) - x) ? samples[lo] : samples[hi];
  }

  /* ------------------------------------------------------------------ chart with playhead + drag-to-zoom */
  const W = 640, H = 220, M = { l: 58, r: 14, t: 10, b: 34 };
  function chart(c, series, x0, x1, xLabel, showDots) {
    const ys = series.flatMap((s) => s.pts.map((p) => p.y));
    if (!ys.length) return `<div class="empty">No measured values in this window.</div>`;
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    const py = (y1 - y0) * 0.08 || 1; y0 -= py; y1 += py;
    const X = (v) => M.l + ((v - x0) / (x1 - x0 || 1)) * (W - M.l - M.r), Y = (v) => H - M.b - ((v - y0) / (y1 - y0 || 1)) * (H - M.t - M.b);
    let g = '';
    const yd = y1 - y0 >= 40 ? 0 : y1 - y0 >= 4 ? 1 : 2; // enough decimals that the five labels differ
    for (let i = 0; i <= 4; i++) { const v = y0 + ((y1 - y0) * i) / 4; g += `<line class="grid" x1="${M.l}" x2="${W - M.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${M.l - 6}" y="${Y(v) + 4}" text-anchor="end">${f(v, yd)}</text>`; }
    for (let i = 0; i <= 6; i++) { const v = x0 + ((x1 - x0) * i) / 6; g += `<text x="${X(v)}" y="${H - M.b + 15}" text-anchor="middle">${f(v, x1 - x0 < 20 ? 1 : 0)}</text>`; }
    g += `<line class="axis" x1="${M.l}" x2="${W - M.r}" y1="${H - M.b}" y2="${H - M.b}"/><text x="${(M.l + W - M.r) / 2}" y="${H - 4}" text-anchor="middle">${esc(xLabel)}</text>`;
    series.forEach((s) => {
      const pts = thin(s.pts);
      g += `<polyline fill="none" stroke="${s.color}" stroke-width="1.6" ${s.dash ? `stroke-dasharray="${s.dash}"` : ''} points="${pts.map((p) => `${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ')}"/>`;
      if (showDots && s.pts.length <= 2000) s.pts.forEach((p) => { g += `<circle cx="${X(p.x).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="1.6" fill="${s.color}"/>`; });
    });
    return `<svg class="chart insp" viewBox="0 0 ${W} ${H}" data-x0="${x0}" data-x1="${x1}" role="img" aria-label="${esc(c.label)}">${g}
      <rect class="insp-sel" x="0" y="${M.t}" width="0" height="${H - M.t - M.b}" fill="#ffffff" opacity=".08" visibility="hidden"/>
      <line class="insp-head" x1="0" x2="0" y1="${M.t}" y2="${H - M.b}" stroke="#ffb400" stroke-width="1.2" visibility="hidden"/>
      <rect class="insp-hit" x="${M.l}" y="${M.t}" width="${W - M.l - M.r}" height="${H - M.t - M.b}" fill="transparent" style="cursor:crosshair"/></svg>
      <div class="legend">${series.filter((s) => s.pts.length).map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join('')}</div>`;
  }
  const svgX = (svg, clientX) => {
    const r = svg.getBoundingClientRect(), vx = ((clientX - r.left) / r.width) * W;
    const x0 = +svg.dataset.x0, x1 = +svg.dataset.x1;
    return x0 + ((Math.min(Math.max(vx, M.l), W - M.r) - M.l) / (W - M.l - M.r)) * (x1 - x0);
  };
  const toPx = (svg, x) => M.l + ((x - +svg.dataset.x0) / (+svg.dataset.x1 - +svg.dataset.x0 || 1)) * (W - M.l - M.r);

  /* ------------------------------------------------------------------ mount
     opts: { a: {label, samples}, b?: {label, samples}, downloads?: html } */
  function mount(root, opts) {
    const A = prepare(opts.a.samples), B = opts.b ? prepare(opts.b.samples) : null;
    const avA = availability(A), avB = B ? availability(B) : null;
    const st = { on: new Set(DEFAULT_ON.filter((k) => avA[k])), dots: false, head: null, timer: null,
      winA: [A[0]?.session_time ?? 0, A.at(-1)?.session_time ?? 1], winB: B ? [B[0].session_time, B.at(-1).session_time] : null };
    const cmp = () => !!B;
    const inWin = (s, w) => s.filter((x) => x.session_time >= w[0] && x.session_time <= w[1]);
    const xOfA = () => (cmp() ? (s) => s._dist - (inWin(A, st.winA)[0]?._dist ?? 0) : (s) => s.session_time);
    const missing = CHANNELS.filter((c) => !avA[c.key] || (avB && !avB[c.key]));
    root.innerHTML = `<div class="insp-bar row">
        <label class="chk">A window (session s) <input id="ia0" type="number" step="0.1" value="${f(st.winA[0], 1)}"> &ndash; <input id="ia1" type="number" step="0.1" value="${f(st.winA[1], 1)}"></label>
        ${B ? `<label class="chk">B window <input id="ib0" type="number" step="0.1" value="${f(st.winB[0], 1)}"> &ndash; <input id="ib1" type="number" step="0.1" value="${f(st.winB[1], 1)}"></label>` : ''}
        <button class="btn small" id="iapply" type="button">Apply</button><button class="btn small" id="ireset" type="button">Full stint</button>
        <button class="btn small" id="iplay" type="button">&#9654; Replay</button><select id="ispeed" style="width:auto"><option value="1">1x</option><option value="4" selected>4x</option><option value="10">10x</option></select>
        <label class="chk"><input type="checkbox" id="idots"> show every measured sample</label></div>
      <div class="insp-ch row">${CHANNELS.map((c) => `<label class="chk ${avA[c.key] ? '' : 'dim'}"><input type="checkbox" data-ch="${c.key}" ${st.on.has(c.key) ? 'checked' : ''} ${avA[c.key] ? '' : 'disabled'}> ${esc(c.label)}${avA[c.key] ? '' : ' (missing)'}</label>`).join('')}</div>
      ${missing.length ? `<div class="note">Not in this recording: ${missing.map((c) => `<b>${esc(c.label)}</b>${avB && avA[c.key] && !avB[c.key] ? ' (B only)' : ''}`).join(', ')}. Missing channels are not drawn and never shown as zero.</div>` : ''}
      ${cmp() ? '<div class="note info">Comparison x-axis: distance from the start of each window, <b>derived</b> by integrating the measured speed over time (the recorded UDP samples have no lap-distance field). Pick windows that cover the same part of the track.</div>' : ''}
      <div id="istat" class="hint"></div><div id="iread" class="insp-read"><p class="hint" style="padding:8px">Hover a chart (or press Replay) to read the exact measured values.</p></div><div id="icharts" class="grid g2"></div>${opts.downloads || ''}`;
    const draw = () => {
      const a = inWin(A, st.winA), b = B ? inWin(B, st.winB) : null, xa = xOfA();
      const xb = (s) => s._dist - (b?.[0]?._dist ?? 0);
      const xs = a.map(xa).concat(b ? b.map(xb) : []);
      const x0 = Math.min(...xs), x1 = Math.max(...xs);
      const lbl = cmp() ? 'distance from window start (m, derived)' : 'session time (s)';
      let drawn = 0;
      $('#icharts', root).innerHTML = CHANNELS.filter((c) => st.on.has(c.key)).map((c) => {
        const ser = [...seriesOf(c, a, xa, COLORS.a, B ? `${opts.a.label} ` : ''), ...(b && avB[c.key] ? seriesOf(c, b, xb, COLORS.b, `${opts.b.label} `) : [])];
        drawn += ser.reduce((n, s) => n + thin(s.pts).length, 0);
        return `<div class="panel" style="margin-bottom:0"><h2>${esc(c.label)} <span class="dim">${esc(c.unit)}</span></h2>${xs.length ? chart(c, ser, x0, x1, lbl, st.dots) : '<div class="empty">No samples in this window.</div>'}</div>`;
      }).join('') || '<div class="empty" style="grid-column:1/-1">Select at least one channel.</div>';
      $('#istat', root).innerHTML = `<b>${a.length}</b> measured samples in window A${b ? `, <b>${b.length}</b> in window B` : ''} (of ${A.length}${B ? ` / ${B.length}` : ''} recorded). Charts draw ${drawn} points (min/max downsampled when a window has more than ${MAX_DRAWN} samples); hover for the exact measured values. Drag across a chart to zoom.`;
      bind(a, b, xa, xb);
    };
    const readout = (x, a, b, xa, xb) => {
      const sa = nearest(a, x, xa), sb = b ? nearest(b, x, xb) : null;
      const val = (c, s) => {
        if (!s) return '&mdash;';
        if (c.wheels) return Array.isArray(s[c.key]) && s[c.key].length ? s[c.key].map((v, i) => `${WHEELS[i]} ${f(v, 1)}`).join(' ') : '<span class="dim">missing</span>';
        const v = c.get(s); return finite(v) ? f(v, 2) : '<span class="dim">missing</span>';
      };
      const rows = CHANNELS.filter((c) => st.on.has(c.key)).map((c) => `<tr><td>${esc(c.label)}</td><td class="num">${val(c, sa)}</td>${b ? `<td class="num">${val(c, sb)}</td>` : ''}</tr>`).join('');
      $('#iread', root).innerHTML = `<table><thead><tr><th>Measured sample at ${cmp() ? `${f(x, 0)} m` : `${f(x, 2)} s`}</th><th class="num">${esc(opts.a.label)}${sa ? ` &middot; t=${f(sa.session_time, 3)} s, frame ${esc(sa.frame_id)}` : ''}</th>${b ? `<th class="num">${esc(opts.b.label)}${sb ? ` &middot; t=${f(sb.session_time, 3)} s` : ''}</th>` : ''}</tr></thead><tbody>${rows}</tbody></table>`;
    };
    const setHead = (x, a, b, xa, xb) => {
      st.head = x;
      root.querySelectorAll('svg.insp').forEach((svg) => { const l = svg.querySelector('.insp-head'); const px = toPx(svg, x); l.setAttribute('x1', px); l.setAttribute('x2', px); l.setAttribute('visibility', 'visible'); });
      readout(x, a, b, xa, xb);
    };
    const bind = (a, b, xa, xb) => {
      root.querySelectorAll('svg.insp').forEach((svg) => {
        const hit = svg.querySelector('.insp-hit'), sel = svg.querySelector('.insp-sel');
        let drag = null;
        hit.addEventListener('mousemove', (e) => {
          const x = svgX(svg, e.clientX);
          if (drag !== null) { const p0 = toPx(svg, drag), p1 = toPx(svg, x); sel.setAttribute('x', Math.min(p0, p1)); sel.setAttribute('width', Math.abs(p1 - p0)); sel.setAttribute('visibility', 'visible'); }
          if (!st.timer) setHead(x, a, b, xa, xb);
        });
        hit.addEventListener('mousedown', (e) => { drag = svgX(svg, e.clientX); });
        hit.addEventListener('mouseup', (e) => {
          const x = svgX(svg, e.clientX), from = drag; drag = null; sel.setAttribute('visibility', 'hidden');
          if (from === null || Math.abs(x - from) < 1e-6) return;
          zoomTo(Math.min(from, x), Math.max(from, x), a, xa);
        });
        hit.addEventListener('mouseleave', () => { drag = null; sel.setAttribute('visibility', 'hidden'); });
      });
    };
    // zoom: x range -> session-time windows (comparison windows are mapped through each stint's derived distance)
    const zoomTo = (x0, x1, a, xa) => {
      if (!cmp()) { st.winA = [x0, x1]; }
      else {
        const pick = (arr, xo, lo, hi) => { const s = arr.filter((v) => xo(v) >= lo && xo(v) <= hi); return s.length ? [s[0].session_time, s.at(-1).session_time] : null; };
        const b = inWin(B, st.winB), xb = (s) => s._dist - (b[0]?._dist ?? 0);
        st.winA = pick(a, xa, x0, x1) || st.winA; st.winB = pick(b, xb, x0, x1) || st.winB;
      }
      syncInputs(); draw();
    };
    const syncInputs = () => {
      $('#ia0', root).value = f(st.winA[0], 2); $('#ia1', root).value = f(st.winA[1], 2);
      if (B) { $('#ib0', root).value = f(st.winB[0], 2); $('#ib1', root).value = f(st.winB[1], 2); }
    };
    const stop = () => { clearInterval(st.timer); st.timer = null; $('#iplay', root).innerHTML = '&#9654; Replay'; };
    $('#iapply', root).addEventListener('click', () => {
      const r = (id) => parseFloat($(id, root).value);
      if (r('#ia1') > r('#ia0')) st.winA = [r('#ia0'), r('#ia1')];
      if (B && r('#ib1') > r('#ib0')) st.winB = [r('#ib0'), r('#ib1')];
      draw();
    });
    $('#ireset', root).addEventListener('click', () => { st.winA = [A[0].session_time, A.at(-1).session_time]; if (B) st.winB = [B[0].session_time, B.at(-1).session_time]; syncInputs(); draw(); });
    $('#idots', root).addEventListener('change', (e) => { st.dots = e.target.checked; draw(); });
    root.querySelectorAll('[data-ch]').forEach((cb) => cb.addEventListener('change', () => { cb.checked ? st.on.add(cb.dataset.ch) : st.on.delete(cb.dataset.ch); draw(); }));
    $('#iplay', root).addEventListener('click', () => {
      if (st.timer) return stop();
      const a = inWin(A, st.winA), b = B ? inWin(B, st.winB) : null, xa = xOfA(), xb = (s) => s._dist - (b?.[0]?._dist ?? 0);
      if (!a.length) return;
      let i = Math.max(0, a.findIndex((s) => xa(s) >= (st.head ?? -Infinity)));
      const tick = 50, speed = () => +$('#ispeed', root).value;
      $('#iplay', root).innerHTML = '&#10074;&#10074; Pause';
      st.timer = setInterval(() => {
        const target = a[i].session_time + (tick / 1000) * speed(); // replay at recorded time: 1x = real time
        while (i < a.length - 1 && a[i].session_time < target) i++;
        setHead(xa(a[i]), a, b, xa, xb);
        if (i >= a.length - 1) stop();
      }, tick);
    });
    draw();
    return { stop };
  }
  return { mount, prepare, availability, thin, CHANNELS };
})();
