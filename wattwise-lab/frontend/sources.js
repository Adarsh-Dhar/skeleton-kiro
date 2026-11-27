/* WATTWISE LAB: source records for every input (zero fabricated input data).
   srcFields(id, src, kind) renders the form for one input's source; readSrc(id) reads it back (null when nothing was entered).
   The server enforces the rules: "verified" needs an official/manufacturer/research source, a document and a collection date;
   your own data (bill, meter export, label you read) is "user supplied". */
const Sources = (() => {
  const e = (s) => Charts.esc(s ?? '');
  const TYPES = [
    ['user_provided', 'Entered by me (from my bill, label or own reading)'],
    ['meter_export', 'Meter export (utility portal or meter)'],
    ['official', 'Official publication (regulator / utility tariff order)'],
    ['manufacturer', 'Manufacturer specification'],
    ['research_dataset', 'Documented research dataset'],
    ['unverified', 'Unverified (no traceable source)'],
  ];
  const LEVELS = [['user_supplied', 'User supplied'], ['verified', 'Verified (WATTWISE retrieves the publisher\u2019s document)'], ['unverified', 'Unverified']];
  // what to read from where, per kind of input (shown as the field hint)
  const HINTS = {
    tariff: 'Copy the rates from your latest bill or your DISCOM\u2019s current tariff order (state commission). Cite the bill month or the order number and its effective dates.',
    ev: 'Battery size (usable kWh) and charging efficiency from the vehicle\u2019s official specification or owner\u2019s manual; cite the document and the model year.',
    charger: 'Rated output in kW from the charger\u2019s nameplate or manual (for example 3.3 kW or 7.2 kW AC).',
    appliance: 'Rated power from the appliance\u2019s rating plate, BEE star label or manual; cycle length from the programme you use (or your own plug-meter reading).',
    base_load: 'Your measured always-on load: lowest steady reading on your smart meter at night, or a plug-in meter reading. Better: import your meter readings instead.',
    meter: 'The portal or meter you exported the file from and the date you downloaded it.',
  };
  const defaults = (kind) => ({ source_type: kind === 'meter' ? 'meter_export' : 'user_provided', source_name: kind === 'meter' ? '' : 'household owner', verification: 'user_supplied' });
  function srcFields(id, src, kind) {
    const example = src && src.source_type === 'example';
    const s = example ? { ...src, source_name: '', verification: 'user_supplied' } : src || defaults(kind);
    const v = (k) => e(s[k] ?? '');
    const ev = s.evidence;
    return `<details class="srcbox" id="${id}" ${src && (src.legacy || example) ? 'open' : ''} ${src && src.legacy && !example ? 'data-legacy="1"' : ''}><summary>Source ${src ? badge(src.status || null, src) : '<span class="chip prov-u">not given</span>'}</summary>
      ${example ? '<div class="errors">This value is a copy of an example earlier versions filled in. If it really is yours, choose where it comes from; otherwise replace it.</div>' : ''}
      ${src && src.legacy && !example ? `<label class="inline"><input type="checkbox" id="${id}-ok"> I have checked this value against its source (it was saved before source tracking)</label>` : ''}
      <p class="hint">${e(HINTS[kind] || '')}</p>
      <div class="grid g2">
      <div class="field"><label for="${id}-type">Source type</label><select id="${id}-type">${example ? '<option value="" selected>&mdash; choose &mdash;</option>' : ''}${TYPES.map(([k, l]) => `<option value="${k}" ${!example && s.source_type === k ? 'selected' : ''}>${e(l)}</option>`).join('')}</select></div>
      <div class="field"><label for="${id}-ver">Verification</label><select id="${id}-ver">${LEVELS.map(([k, l]) => `<option value="${k}" ${s.verification === k ? 'selected' : ''}>${e(l)}</option>`).join('')}</select></div>
      <div class="field"><label for="${id}-name">Source name</label><input id="${id}-name" maxlength="200" value="${v('source_name')}" placeholder="publisher, manufacturer or 'household owner'"></div>
      <div class="field"><label for="${id}-doc">Document or URL</label><input id="${id}-doc" maxlength="500" value="${v('document')}" placeholder="bill month, tariff order no., spec sheet URL"></div>
      <div class="field"><label for="${id}-col">Collected on</label><input id="${id}-col" type="date" value="${v('collected_on')}"></div>
      <div class="field"><label for="${id}-from">Effective from</label><input id="${id}-from" type="date" value="${v('effective_from')}"></div>
      <div class="field"><label for="${id}-to">Effective until</label><input id="${id}-to" type="date" value="${v('effective_to')}"></div>
      <div class="field"><label for="${id}-notes">Notes</label><input id="${id}-notes" maxlength="500" value="${v('notes')}"></div></div>
      <input type="hidden" id="${id}-evid" value="${e(s.evidence_id || '')}">
      <div class="row" style="margin:4px 0 6px"><button type="button" class="btn small" data-retrieve="${id}">Retrieve document from the URL above</button>
        <label class="btn small" style="cursor:pointer">Attach a file<input type="file" data-attach="${id}" hidden></label>
        <span class="hint" id="${id}-evinfo">${ev ? evidenceText(ev) : 'No document archived. "Verified" needs WATTWISE to retrieve the publisher\u2019s document itself.'}</span></div></details>`;
  }
  const evidenceText = (ev) => `${ev.kind === 'retrieved_url' ? 'Retrieved by WATTWISE' : 'Attached by you'}: ${e(ev.final_url || ev.url || ev.file_name || '')} &middot; sha256 ${e(String(ev.sha256 || '').slice(0, 12))}&hellip; &middot; ${e(String(ev.retrieved_at || '').slice(0, 10))} <a href="/api/evidence/${e(ev.evidence_id)}/content">archived copy</a>`;
  // retrieve / attach buttons inside any source box (event delegation: the boxes are re-rendered often)
  document.addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-retrieve]'); if (!b) return;
    const id = b.dataset.retrieve, url = document.querySelector(`#${id}-doc`).value.trim(), info = document.querySelector(`#${id}-evinfo`);
    if (!/^https:\/\//.test(url)) { info.textContent = 'Enter the document\u2019s https:// URL in "Document or URL" first.'; return; }
    info.textContent = 'Retrieving\u2026';
    try { const r = await api('POST', '/api/evidence/retrieve', { url }); document.querySelector(`#${id}-evid`).value = r.evidence_id; info.innerHTML = evidenceText(r); }
    catch (err) { info.textContent = err.message; }
  });
  document.addEventListener('change', async (ev) => {
    const inp = ev.target.closest('[data-attach]'); if (!inp || !inp.files[0]) return;
    const id = inp.dataset.attach, f = inp.files[0], info = document.querySelector(`#${id}-evinfo`);
    info.textContent = 'Archiving\u2026';
    try {
      const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(',')[1] || ''); r.onerror = () => rej(new Error('could not read the file')); r.readAsDataURL(f); });
      const r = await api('POST', '/api/evidence/attach', { file_name: f.name, content_b64: b64, content_type: f.type || null });
      document.querySelector(`#${id}-evid`).value = r.evidence_id; info.innerHTML = evidenceText(r);
    } catch (err) { info.textContent = err.message; }
  });
  function readSrc(id, root = document) {
    const g = (k) => { const el = root.querySelector(`#${id}-${k}`); return el ? el.value.trim() : ''; };
    if (!root.querySelector(`#${id}-type`)) return null;
    const out = { source_type: g('type'), source_name: g('name'), verification: g('ver') };
    for (const [k, f] of [['document', 'doc'], ['collected_on', 'col'], ['effective_from', 'from'], ['effective_to', 'to'], ['notes', 'notes'], ['evidence_id', 'evid']]) { const x = g(f); if (x) out[k] = x; }
    // a value saved before source tracking stays marked until the owner ticks that they checked it
    const box = root.querySelector(`#${id}`), ok = root.querySelector(`#${id}-ok`);
    if (box && box.dataset.legacy && !(ok && ok.checked)) out.legacy = true;
    return out;
  }
  // status is the server's computed source_status; without it the label is derived from the record
  function badge(status, src) {
    const derived = (s) => {
      if (s.source_type === 'example') return 'EXAMPLE · NOT REAL';
      const retrieved = s.evidence && s.evidence.kind === 'retrieved_url';
      if (s.verification === 'verified') return retrieved ? 'VERIFIED · DOCUMENT RETRIEVED' : 'CITED · NOT RETRIEVED';
      return (({ user_supplied: 'USER SUPPLIED', unverified: 'UNVERIFIED' })[s.verification] || 'UNVERIFIED') + (s.evidence ? ' · DOCUMENT ATTACHED' : '') + (s.legacy ? ' · TO CONFIRM' : '');
    };
    const st = status || (src ? { label: derived(src), warnings: [] } : null);
    if (!st) return '<span class="chip prov-u">NO SOURCE</span>';
    const css = st.label.startsWith('EXAMPLE') ? 'prov-s' : st.label.startsWith('VERIFIED') && !st.expired ? 'prov-m' : st.label.startsWith('USER') && !st.expired ? 'prov-mt' : 'prov-u';
    // a stored source carries no status of its own: derive the "cited, not retrieved" case like the server does
    return `<span class="chip ${css}" title="${e((st.warnings || []).join('; '))}">${e(st.label)}</span>`;
  }
  const summary = (src) => (src ? `${e(src.source_name)}${src.document ? ` &middot; ${e(src.document)}` : ''}${src.collected_on ? ` &middot; collected ${e(src.collected_on)}` : ''}` : 'no source recorded');
  // table of an input-source snapshot (experiment detail and report)
  function table(inputs) {
    if (!inputs || !inputs.length) return '<p class="hint">No input sources were stored with this experiment.</p>';
    return `<div class="scroll"><table><thead><tr><th>Input</th><th>Values</th><th>Source</th><th>Status</th></tr></thead><tbody>${inputs.map((i) => `<tr><td class="mono">${e(i.input)}</td>
      <td class="dim">${Object.entries(i.values || {}).filter(([k, x]) => x !== null && x !== undefined && k !== 'periods').map(([k, x]) => `${e(k)} ${e(x)}${i.units && i.units[k] ? ' ' + e(i.units[k]) : ''}`).join(', ')}</td>
      <td>${summary(i.source)}</td><td>${badge(i.status)}</td></tr>`).join('')}</tbody></table></div>`;
  }
  return { srcFields, readSrc, badge, summary, table, HINTS };
})();
