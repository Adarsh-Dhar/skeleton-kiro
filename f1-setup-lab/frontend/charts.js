/* Minimal dependency-free SVG charts. Every function returns an SVG string (all text is escaped). */
const Charts = (() => {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const f = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? 'n/a' : Number(v).toFixed(d));

  function niceTicks(min, max, n = 5) {
    if (!(max > min)) { max = min + 1; }
    const raw = (max - min) / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(+v.toPrecision(12));
    return out;
  }
  const scale = (d0, d1, r0, r1) => (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);

  function frame(o, xs, ys) {
    const W = o.width || 640, H = o.height || 320, m = { l: 62, r: 18, t: 14, b: 44, ...(o.margin || {}) };
    const xd = [Math.min(...xs), Math.max(...xs)], yd = [Math.min(...ys), Math.max(...ys)];
    const px = (xd[1] - xd[0]) * 0.06 || 1, py = (yd[1] - yd[0]) * 0.08 || 1;
    const x0 = o.xmin ?? xd[0] - px, x1 = o.xmax ?? xd[1] + px, y0 = o.ymin ?? yd[0] - py, y1 = o.ymax ?? yd[1] + py;
    const X = scale(x0, x1, m.l, W - m.r), Y = o.reverseY ? scale(y0, y1, m.t, H - m.b) : scale(y0, y1, H - m.b, m.t);  // reverseY: small values on top (running order)
    let g = '';
    niceTicks(y0, y1, 5).forEach((t) => { g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${f(t, Math.abs(t) < 10 ? 2 : 1)}</text>`; });
    niceTicks(x0, x1, 7).forEach((t) => { g += `<line class="grid" y1="${m.t}" y2="${H - m.b}" x1="${X(t)}" x2="${X(t)}"/><text x="${X(t)}" y="${H - m.b + 16}" text-anchor="middle">${f(t, Number.isInteger(t) ? 0 : 1)}</text>`; });
    g += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><line class="axis" x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${H - m.b}"/>`;
    if (o.xLabel) g += `<text x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">${esc(o.xLabel)}</text>`;
    if (o.yLabel) g += `<text transform="translate(14 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">${esc(o.yLabel)}</text>`;
    return { W, H, m, X, Y, g, x0, x1, y0, y1 };
  }
  const wrap = (W, H, body, label) => `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label || 'chart')}">${body}</svg>`;



  /** series:[{name,color,pts:[{x,y}]}] */
  function lines(o) {
    const all = (o.series || []).flatMap((s) => s.pts);
    if (!all.length) return '<div class="empty">No data.</div>';
    const fr = frame(o, all.map((p) => p.x), all.map((p) => p.y)), { X, Y } = fr;
    let b = fr.g;
    o.series.forEach((s) => {
      const path = o.stepped ? s.pts.flatMap((p, i) => (i ? [`${X(p.x)},${Y(s.pts[i - 1].y)}`, `${X(p.x)},${Y(p.y)}`] : [`${X(p.x)},${Y(p.y)}`])) : s.pts.map((p) => `${X(p.x)},${Y(p.y)}`);
      b += `<polyline fill="none" stroke="${s.color}" stroke-width="2" points="${path.join(' ')}"/>`;
      if (!o.noDots) s.pts.forEach((p) => { b += `<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="${p.flag ? 6 : 3.5}" fill="${p.flag ? '#ff4d4d' : s.color}"><title>${esc(s.name)} ${f(p.x, 2)}: ${f(p.y, 3)}${p.flag ? ' (flagged)' : ''}</title></circle>`; });
    });
    return wrap(fr.W, fr.H, b, o.label) + `<div class="legend">${o.series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join('')}</div>`;
  }

  /** laps:[{lap,sector_1,sector_2,sector_3,outlier}] stacked sector bars (y starts at 0) */
  function sectors(o) {
    const laps = o.laps || [];
    if (!laps.length) return '<div class="empty">No laps.</div>';
    const W = o.width || 640, H = o.height || 300, m = { l: 58, r: 14, t: 12, b: 34 };
    const tot = laps.map((l) => (l.sector_1 || 0) + (l.sector_2 || 0) + (l.sector_3 || 0));
    const Y = scale(0, Math.max(...tot) * 1.05, H - m.b, m.t), bw = Math.min(46, (W - m.l - m.r) / laps.length - 6);
    const X = (i) => m.l + ((i + 0.5) * (W - m.l - m.r)) / laps.length;
    let b = '';
    niceTicks(0, Math.max(...tot) * 1.05, 5).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${f(t, 0)}</text>`; });
    const cols = ['#4aa8ff', '#e10600', '#ffb400'];
    laps.forEach((l, i) => {
      let acc = 0;
      [1, 2, 3].forEach((k, j) => {
        const v = l['sector_' + k] || 0;
        b += `<rect x="${X(i) - bw / 2}" y="${Y(acc + v)}" width="${bw}" height="${Y(acc) - Y(acc + v)}" fill="${cols[j]}" opacity="${l.outlier ? 1 : 0.8}" ${l.outlier ? 'stroke="#fff" stroke-width="1.5"' : ''}><title>Lap ${l.lap} sector ${k}: ${f(v, 3)} s</title></rect>`;
        acc += v;
      });
      b += `<text x="${X(i)}" y="${H - m.b + 15}" text-anchor="middle">${l.lap}</text>`;
    });
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><text x="${(m.l + W - m.r) / 2}" y="${H - 4}" text-anchor="middle">lap</text>`;
    return wrap(W, H, b, 'sector times per lap') + `<div class="legend"><span><i style="background:#4aa8ff"></i>sector 1</span><span><i style="background:#e10600"></i>sector 2</span><span><i style="background:#ffb400"></i>sector 3</span><span>white outline = flagged outlier lap</span></div>`;
  }

  /** Track map from real position samples: points:[{x_m,y_m,speed_kph,distance_m}], corners:[{number,name,distance_m}]. Equal aspect, speed-coloured segments. */
  function trackMap(o) {
    const raw = (o.points || []).filter((p) => p.x_m !== null && p.y_m !== null && p.x_m !== undefined);
    if (!raw.length) return '<div class="empty">No track position data for this selection.</div>';
    const step = Math.max(1, Math.ceil(raw.length / 900));
    const ds = raw.filter((_, i) => i % step === 0 || i === raw.length - 1);
    const W = o.width || 660, H = o.height || 460, m = { l: 24, r: 24, t: 24, b: 30 };
    const xs = ds.map((p) => p.x_m), ys = ds.map((p) => p.y_m);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const sc = Math.min((W - m.l - m.r) / Math.max(x1 - x0, 1e-6), (H - m.t - m.b) / Math.max(y1 - y0, 1e-6));
    const ox = m.l + ((W - m.l - m.r) - (x1 - x0) * sc) / 2, oy = m.t + ((H - m.t - m.b) - (y1 - y0) * sc) / 2;
    const X = (p) => ox + (p.x_m - x0) * sc, Y = (p) => oy + (y1 - p.y_m) * sc;
    const sp = ds.map((p) => p.speed_kph).filter((v) => v !== null && v !== undefined);
    const s0 = Math.min(...sp), s1 = Math.max(...sp), sn = s1 - s0 || 1;
    const col = (v) => { const t = Math.max(0, Math.min(1, (v - s0) / sn)); return `rgb(${Math.round(74 + t * (225 - 74))},${Math.round(168 - t * 120)},${Math.round(255 - t * 200)})`; };
    let b = '';
    for (let i = 0; i < ds.length - 1; i++) {
      const p = ds[i], q = ds[i + 1];
      b += `<line x1="${X(p)}" y1="${Y(p)}" x2="${X(q)}" y2="${Y(q)}" stroke="${col(p.speed_kph ?? s0)}" stroke-width="3" stroke-linecap="round" opacity=".85"><title>${f(p.speed_kph, 0)} km/h</title></line>`;
    }
    const st = ds[0];
    b += `<circle cx="${X(st)}" cy="${Y(st)}" r="6" fill="#fff" stroke="#e10600" stroke-width="2.5"/><text x="${X(st) + 10}" y="${Y(st) - 8}" style="fill:#fff;font-weight:700">S/F</text>`;
    const withDist = ds.filter((p) => p.distance_m !== null && p.distance_m !== undefined);
    (o.corners || []).forEach((c) => {
      if (c.distance_m === null || c.distance_m === undefined || !withDist.length) return;
      let best = withDist[0], bd = Infinity;
      withDist.forEach((p) => { const d = Math.abs(p.distance_m - c.distance_m); if (d < bd) { bd = d; best = p; } });
      b += `<circle cx="${X(best)}" cy="${Y(best)}" r="3.5" fill="none" stroke="#ffb400" stroke-width="1.5"/><text x="${X(best) + 6}" y="${Y(best) + 4}" style="fill:#ffb400">${esc(c.number ?? '')}</text>`;
    });
    return wrap(W, H, b, 'track map') + `<div class="legend"><span><i style="background:#4aa8ff"></i>slow</span><span><i style="background:#e10600"></i>fast</span><span>S/F = start/finish</span><span>amber = corner numbers</span></div>`;
  }

  return { lines, sectors, trackMap, esc, f };
})();
