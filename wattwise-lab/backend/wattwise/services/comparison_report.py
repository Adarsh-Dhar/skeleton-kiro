"""Printable HTML for the actual-vs-optimised comparison. Every value shows its provenance badge."""
from __future__ import annotations

from html import escape
from typing import Any

BADGE = {"historical_measured": ("MEASURED", "#1b7f3b"), "historical_measured_priced": ("MEASURED x TARIFF", "#2d6a4f"),
         "owner_entered_base_load": ("OWNER-ENTERED", "#8a5a00"), "projected": ("PROJECTED", "#7a3db8"), "measured_schedule": ("MEASURED SCHEDULE", "#0b5394")}  # fmt: skip


def _cell(v: dict[str, Any] | None) -> str:
    if not v:
        return "<td>&mdash;</td>"
    text, color = BADGE.get(v.get("provenance"), (v.get("provenance", "?"), "#555"))
    val = v.get("value")
    shown = "not estimated" if val is None else (f"{val:,.3f}" if isinstance(val, float) else escape(str(val)))
    note = f'<div class="note">{escape(v["note"])}</div>' if v.get("note") else ""
    return f'<td>{shown} {escape(v.get("unit", ""))} <span class="b" style="background:{color}">{text}</span>{note}</td>'


def render(c: dict[str, Any]) -> str:
    h, s = c["historical"], c["schedules"]
    rows_sched = []
    for name, d in (("Current schedule", s["current"]), ("Best projected schedule", s["best_projected"])):
        if d:
            rows_sched.append(f"<tr><th>{name}<div class='note'>{escape(d['schedule'])}</div></th>{_cell(d['cost'])}{_cell(d['kwh'])}{_cell(d['peak_kw'])}"
                              f"{_cell(d['ev_charge_at_departure_pct'])}</tr>")
    for d in s["measured"]:
        rows_sched.append(f"<tr><th>Measured run {escape(d['run_id'])}<div class='note'>{escape(d['schedule'])}</div></th>{_cell(d['cost'])}{_cell(d['kwh'])}"
                          f"{_cell(d['peak_kw'])}{_cell(d['ev_charge_at_departure_pct'])}</tr>")
    periods = "".join(f"<tr><th>{escape(k)}</th>{_cell(v['historical_per_day'])}{_cell(v['projected_best'])}</tr>" for k, v in c["cost_by_period"].items())
    savings = "".join(f"<tr><th>{escape(k.replace('_', ' '))}</th>{_cell(v)}</tr>" for k, v in c["savings"].items())
    days = "".join(f"<tr><td>{escape(d['date'])}{'' if d['complete'] else ' (partial)'}</td>{_cell(d['kwh'])}{_cell(d['peak_kw'])}</tr>" for d in h["days"][:62])
    legend = "".join(f'<li><span class="b" style="background:{col}">{t}</span> {escape(c["labels"][k])}</li>' for k, (t, col) in BADGE.items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Actual usage vs optimised schedule</title><style>
body{{font:14px system-ui,sans-serif;max-width:1000px;margin:24px auto;color:#222}}table{{border-collapse:collapse;width:100%;margin:8px 0 20px}}
td,th{{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}.b{{color:#fff;border-radius:3px;padding:1px 5px;font-size:11px}}
.note{{color:#666;font-size:12px}}</style></head><body>
<h1>My actual electricity usage vs optimised schedule</h1>
<p>Experiment <b>{escape(c['experiment']['name'])}</b> ({escape(c['experiment']['id'])}), tariff <b>{escape(str(c['experiment']['tariff']))}</b>,
background load: <b>{escape(c['experiment']['baseline'])}</b>. Meter import <b>{escape(c['import']['file_name'])}</b> ({escape(c['import']['import_id'])}).</p>
<ul>{legend}</ul>
<h2>Historical (measured)</h2><table><tr><th>Mean daily energy</th>{_cell(h['mean_daily_kwh'])}</tr><tr><th>Mean daily cost</th>{_cell(h['mean_daily_cost'])}</tr>
<tr><th>Total cost of the import period</th>{_cell(h['total_cost'])}</tr><tr><th>Peak demand</th>{_cell(h['peak_kw'])}</tr>
<tr><th>Mean daily peak</th>{_cell(h['mean_daily_peak_kw'])}</tr></table>
<h2>Schedules</h2><table><tr><th></th><th>Cost per day</th><th>Energy</th><th>Peak</th><th>EV charge at departure</th></tr>{''.join(rows_sched)}</table>
<h2>Cost by tariff period</h2><table><tr><th>Period</th><th>Historical, per day</th><th>Best projected</th></tr>{periods}</table>
<h2>Estimated savings</h2><table>{savings}</table>
<h2>Assumptions</h2><ul>{''.join(f'<li>{escape(a)}</li>' for a in c['assumptions'])}</ul>
<h2>Daily history</h2><table><tr><th>Date</th><th>Energy</th><th>Peak</th></tr>{days}</table>
</body></html>"""
