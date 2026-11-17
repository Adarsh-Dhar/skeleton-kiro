/* Dependency-free SVG charts for WATTWISE LAB. Every function returns an SVG/HTML string; all dynamic text is escaped. */
const Charts = (() => {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const f = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v)) ? 'n/a' : Number(v).toFixed(d));
  /** Absolute hour (16..40) -> "HH:MM" ("+1d" after midnight), same convention as the backend. */
  const clock = (h) => { const m = Math.round(h * 60); return `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}${m >= 1440 ? '+1d' : ''}`; };

  function niceTicks(min, max, n = 5) {
    if (!(max > min)) max = min + 1;
    const raw = (max - min) / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(+v.toPrecision(12));
    return out;
  }
  /** Smallest "nice" axis maximum that is >= v (niceTicks alone can return a top tick below the data). */
  function niceMax(v, n = 5) {
    const t = niceTicks(0, v, n), step = t.length > 1 ? t[1] - t[0] : 1;
    let top = t[t.length - 1];
    while (top < v - 1e-9) top += step;
    return +top.toPrecision(12);
  }
  const scale = (d0, d1, r0, r1) => (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);
  const wrap = (W, H, body, label) => `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label || 'chart')}">${body}</svg>`;

  /** Price colour: cheap = blue, expensive = vermilion (returned as an rgb string). */
  function priceColour(price, lo, hi) {
    const t = hi > lo ? (price - lo) / (hi - lo) : 0.5;
    const a = [58, 90, 155], b = [209, 53, 31];
    return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(',')})`;
  }
  const periodAt = (tariff, t) => {
    const h = ((t % 24) + 24) % 24;
    return (tariff.periods || []).find((p) => (p.start < p.end ? h >= p.start && h < p.end : h >= p.start || h < p.end));
  };

  /**
   * The signature view: a 24 h window (16:00 -> 16:00) with the tariff price bands behind stacked household load.
   * o: { steps:[{t,base,appliances,ev,kw,price}], tariff, departure, compare:[{t,kw}], height, label }
   */
  function dayProfile(o) {
    const st = o.steps || [];
    if (!st.length) return '<div class="empty">No data yet.</div>';
    const W = o.width || 860, H = o.height || 340, m = { l: 52, r: 14, t: 16, b: 58 };
    const peak = Math.max(...st.map((s) => s.kw), ...(o.compare || []).map((s) => s.kw), 1);
    const ymax = niceMax(peak * 1.08);
    const X = scale(16, 40, m.l, W - m.r), Y = scale(0, ymax, H - m.b, m.t);
    const prices = st.map((s) => s.price), lo = Math.min(...prices), hi = Math.max(...prices);
    const dt = 0.25;
    let b = '';
    st.forEach((s) => { b += `<rect x="${X(s.t)}" y="${m.t}" width="${X(s.t + dt) - X(s.t) + 0.4}" height="${H - m.b - m.t}" fill="${priceColour(s.price, lo, hi)}" opacity=".13"/>`; });
    niceTicks(0, ymax, 5).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 7}" y="${Y(t) + 4}" text-anchor="end">${f(t, t % 1 ? 1 : 0)}</text>`; });
    for (let h = 16; h <= 40; h += 2) b += `<line class="grid v" y1="${m.t}" y2="${H - m.b}" x1="${X(h)}" x2="${X(h)}"/><text x="${X(h)}" y="${H - m.b + 15}" text-anchor="middle">${clock(h).replace('+1d', '')}</text>`;
    const layers = [['base', 'c-base'], ['appliances', 'c-app'], ['ev', 'c-ev']];
    st.forEach((s) => {
      let acc = 0;
      layers.forEach(([k, cls]) => {
        const v = s[k] || 0;
        if (v > 0) b += `<rect class="${cls}" x="${X(s.t)}" y="${Y(acc + v)}" width="${X(s.t + dt) - X(s.t) + 0.4}" height="${Y(acc) - Y(acc + v)}"><title>${esc(clock(s.t))} ${esc(k)} ${f(v, 2)} kW</title></rect>`;
        acc += v;
      });
    });
    if (o.compare && o.compare.length) {
      const pts = o.compare.flatMap((s) => [`${X(s.t)},${Y(s.kw)}`, `${X(s.t + dt)},${Y(s.kw)}`]).join(' ');
      b += `<polyline class="cmp" fill="none" points="${pts}"/>`;
    }
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><line class="axis" x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${H - m.b}"/>`;
    b += `<text x="${m.l}" y="${m.t - 4}" text-anchor="start">kW</text>`;
    if (o.departure !== undefined && o.departure !== null) {
      const d = o.departure >= 16 ? o.departure : o.departure + 24;
      b += `<line class="dep" y1="${m.t}" y2="${H - m.b}" x1="${X(d)}" x2="${X(d)}"/><text class="deptxt" x="${X(d) - 4}" y="${m.t + 10}" text-anchor="end">car leaves ${esc(clock(d))}</text>`;
    }
    // tariff ribbon: contiguous runs of the same period under the axis
    let i = 0;
    while (i < st.length) {
      let j = i;
      while (j + 1 < st.length && st[j + 1].price === st[i].price) j++;
      const x0 = X(st[i].t), x1 = X(st[j].t + dt), p = o.tariff ? periodAt(o.tariff, st[i].t) : null;
      b += `<rect x="${x0}" y="${H - m.b + 24}" width="${Math.max(0, x1 - x0 - 1)}" height="22" fill="${priceColour(st[i].price, lo, hi)}" opacity=".85"><title>${esc((p && p.label) || 'price')} ${f(st[i].price, 3)} per kWh</title></rect>`;
      if (x1 - x0 > (((p && p.label) || '').length + 5) * 6.6 + 8) b += `<text class="ribbon" x="${(x0 + x1) / 2}" y="${H - m.b + 39}" text-anchor="middle">${esc((p && p.label) || '')} ${f(st[i].price, 2)}</text>`;
      i = j + 1;
    }
    const leg = `<div class="legend"><span><i class="c-base"></i>base load</span><span><i class="c-app"></i>appliances</span><span><i class="c-ev"></i>EV charging</span>`
      + `${o.compare ? '<span><i class="cmpkey"></i>today\'s schedule</span>' : ''}<span class="dim">background and ribbon: price per kWh, cheap (blue) to dear (red)</span></div>`;
    return wrap(W, H, b, o.label || 'household load over 24 hours against tariff') + leg;
  }

  /** Cost of each schedule, cheapest first. rows:[{id,cost,feasible,best,label}] baseline: number */
  function runCosts(o) {
    const rows = [...(o.rows || [])].sort((a, b) => a.cost - b.cost);
    if (!rows.length) return '<div class="empty">No completed schedules yet.</div>';
    const W = o.width || 560, H = o.height || 300, m = { l: 52, r: 14, t: 14, b: 34 };
    const vals = rows.map((r) => r.cost).concat(o.baseline ? [o.baseline] : []);
    const ymin = 0, ymax = niceMax(Math.max(...vals) * 1.08);
    const Y = scale(ymin, ymax, H - m.b, m.t), bw = Math.max(2, Math.min(26, (W - m.l - m.r) / rows.length - 2));
    const X = (i) => m.l + ((i + 0.5) * (W - m.l - m.r)) / rows.length;
    let b = '';
    niceTicks(ymin, ymax, 5).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 7}" y="${Y(t) + 4}" text-anchor="end">${f(t, 1)}</text>`; });
    rows.forEach((r, i) => { b += `<rect class="${r.best ? 'bar-best' : r.feasible ? 'bar-ok' : 'bar-bad'}" x="${X(i) - bw / 2}" y="${Y(r.cost)}" width="${bw}" height="${Y(ymin) - Y(r.cost)}"><title>${esc(r.label)}: ${f(r.cost, 2)}${r.feasible ? '' : ' (car not charged enough)'}</title></rect>`; });
    if (o.baseline) b += `<line class="base" x1="${m.l}" x2="${W - m.r}" y1="${Y(o.baseline)}" y2="${Y(o.baseline)}"/><text class="basetxt" x="${W - m.r - 4}" y="${Y(o.baseline) - 5}" text-anchor="end">today's schedule ${f(o.baseline, 2)}</text>`;
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><text x="${(m.l + W - m.r) / 2}" y="${H - 8}" text-anchor="middle">schedules, cheapest first</text>`;
    return wrap(W, H, b, 'daily cost of each schedule') + `<div class="legend"><span><i class="bar-ok"></i>meets departure charge</span><span><i class="bar-bad"></i>car not charged enough</span><span><i class="bar-best"></i>cheapest feasible</span></div>`;
  }

  /** Cost vs peak demand. points:[{x:peak,y:cost,feasible,best,title}] */
  function costPeak(o) {
    const pts = o.points || [];
    if (!pts.length) return '<div class="empty">No completed schedules yet.</div>';
    const W = o.width || 520, H = o.height || 320, m = { l: 52, r: 14, t: 14, b: 40 };
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const x0 = Math.min(...xs) * 0.9, x1 = Math.max(...xs) * 1.05 || 1, y0 = Math.min(...ys) * 0.9, y1 = Math.max(...ys) * 1.05 || 1;
    const X = scale(x0, x1, m.l, W - m.r), Y = scale(y0, y1, H - m.b, m.t);
    let b = '';
    niceTicks(y0, y1, 5).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 7}" y="${Y(t) + 4}" text-anchor="end">${f(t, 1)}</text>`; });
    niceTicks(x0, x1, 6).forEach((t) => { b += `<line class="grid v" y1="${m.t}" y2="${H - m.b}" x1="${X(t)}" x2="${X(t)}"/><text x="${X(t)}" y="${H - m.b + 15}" text-anchor="middle">${f(t, 1)}</text>`; });
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><line class="axis" x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${H - m.b}"/>`;
    b += `<text x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">peak demand (kW)</text><text transform="translate(13 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">daily cost</text>`;
    pts.forEach((p) => { b += `<circle class="${p.best ? 'pt-best' : p.feasible ? 'pt-ok' : 'pt-bad'}" cx="${X(p.x)}" cy="${Y(p.y)}" r="${p.best ? 6 : 4.5}"><title>${esc(p.title || '')}</title></circle>`; });
    return wrap(W, H, b, 'daily cost against peak demand');
  }

  /** Factor effects centred on zero; negative (cheaper) is good. items:[{label,value,lo,hi,sig,p}] */
  function effects(o) {
    const items = o.items || [];
    if (!items.length) return '<div class="empty">No effects available.</div>';
    const W = o.width || 520, rowH = 36, H = items.length * rowH + 44, m = { l: 150, r: 16, t: 8, b: 34 };
    const ext = Math.max(...items.flatMap((i) => [Math.abs(i.value), Math.abs(i.lo ?? 0), Math.abs(i.hi ?? 0)])) * 1.1 || 1;
    const X = scale(-ext, ext, m.l, W - m.r);
    let b = '';
    niceTicks(-ext, ext, 6).forEach((t) => { b += `<line class="grid v" x1="${X(t)}" x2="${X(t)}" y1="${m.t}" y2="${H - m.b}"/><text x="${X(t)}" y="${H - m.b + 15}" text-anchor="middle">${f(t, 2)}</text>`; });
    b += `<line class="axis" x1="${X(0)}" x2="${X(0)}" y1="${m.t}" y2="${H - m.b}"/>`;
    items.forEach((it, i) => {
      const y = m.t + i * rowH + 6, h = rowH - 14, cheaper = it.value < 0;
      b += `<text class="lbl" x="${m.l - 10}" y="${y + h / 2 + 4}" text-anchor="end">${esc(it.label)}${it.sig ? ' *' : ''}</text>`;
      b += `<rect class="${it.sig ? (cheaper ? 'eff-good' : 'eff-bad') : 'eff-ns'}" x="${Math.min(X(0), X(it.value))}" y="${y}" width="${Math.abs(X(it.value) - X(0))}" height="${h}"><title>${esc(it.label)}: ${f(it.value, 3)}${it.p !== undefined && it.p !== null ? ` (p=${Number(it.p).toPrecision(3)})` : ''}</title></rect>`;
      if (it.lo !== null && it.lo !== undefined) b += `<g class="ci"><line x1="${X(it.lo)}" x2="${X(it.hi)}" y1="${y + h / 2}" y2="${y + h / 2}"/><line x1="${X(it.lo)}" x2="${X(it.lo)}" y1="${y + 3}" y2="${y + h - 3}"/><line x1="${X(it.hi)}" x2="${X(it.hi)}" y1="${y + 3}" y2="${y + h - 3}"/></g>`;
    });
    b += `<text x="${(m.l + W - m.r) / 2}" y="${H - 2}" text-anchor="middle">change in daily cost, low to high end of range</text>`;
    return wrap(W, H, b, 'factor effects on daily cost');
  }

  /** Average load per hour of day from a meter file. profile:[{hour,kw}] tariff optional (colours bars by price). */
  function hourly(o) {
    const pr = o.profile || [];
    if (!pr.length) return '<div class="empty">No readings.</div>';
    const W = o.width || 560, H = o.height || 240, m = { l: 46, r: 12, t: 12, b: 32 };
    const ymax = niceMax(Math.max(...pr.map((p) => p.kw)) * 1.08, 4);
    const Y = scale(0, ymax, H - m.b, m.t), bw = (W - m.l - m.r) / 24 - 3;
    const X = (h) => m.l + ((h + 0.5) * (W - m.l - m.r)) / 24;
    const price = (h) => (o.tariff ? (periodAt(o.tariff, h) || {}).price : undefined);
    const ps = o.tariff ? o.tariff.periods.map((p) => p.price) : [0, 1], lo = Math.min(...ps), hi = Math.max(...ps);
    let b = '';
    niceTicks(0, ymax, 4).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 6}" y="${Y(t) + 4}" text-anchor="end">${f(t, 1)}</text>`; });
    pr.forEach((p) => { const pc = price(p.hour); b += `<rect x="${X(p.hour) - bw / 2}" y="${Y(p.kw)}" width="${bw}" height="${Y(0) - Y(p.kw)}" fill="${pc === undefined ? '#0f766e' : priceColour(pc, lo, hi)}" opacity=".85"><title>${String(p.hour).padStart(2, '0')}:00 average ${f(p.kw, 2)} kW</title></rect>`; });
    for (let h = 0; h < 24; h += 3) b += `<text x="${X(h)}" y="${H - m.b + 15}" text-anchor="middle">${String(h).padStart(2, '0')}</text>`;
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><text x="${(m.l + W - m.r) / 2}" y="${H - 4}" text-anchor="middle">hour of day (average kW)</text>`;
    return wrap(W, H, b, 'average load by hour of day');
  }

  /** Best cost found so far as runs complete. pts:[{x,y}] */
  function progressLine(o) {
    const pts = o.pts || [];
    if (pts.length < 2) return '<div class="empty">Needs at least two completed schedules.</div>';
    const W = o.width || 520, H = o.height || 260, m = { l: 52, r: 14, t: 14, b: 34 };
    const y0 = Math.min(...pts.map((p) => p.y)) * 0.95, y1 = Math.max(...pts.map((p) => p.y)) * 1.03;
    const X = scale(1, pts.length, m.l, W - m.r), Y = scale(y0, y1, H - m.b, m.t);
    let b = '';
    niceTicks(y0, y1, 4).forEach((t) => { b += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 7}" y="${Y(t) + 4}" text-anchor="end">${f(t, 2)}</text>`; });
    b += `<polyline class="prog" fill="none" points="${pts.map((p) => `${X(p.x)},${Y(p.y)}`).join(' ')}"/>`;
    pts.forEach((p) => { b += `<circle class="${p.opt ? 'pt-best' : 'pt-ok'}" cx="${X(p.x)}" cy="${Y(p.y)}" r="${p.opt ? 5 : 3}"><title>after schedule ${p.x}: best cost ${f(p.y, 3)}${p.opt ? ' (optimiser-proposed)' : ''}</title></circle>`; });
    b += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><text x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">schedules completed</text>`;
    return wrap(W, H, b, 'best feasible cost so far');
  }

  return { niceMax, dayProfile, runCosts, costPeak, effects, hourly, progressLine, esc, f, clock, periodAt };
})();
