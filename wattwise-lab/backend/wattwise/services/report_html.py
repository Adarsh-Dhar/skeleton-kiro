"""Self-contained printable HTML report (no external assets)."""

from __future__ import annotations

from html import escape as e

from wattwise import SIMULATED_NOTICE


def _table(headers: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{e(str(h))}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>"


def _sources(inputs: list[dict] | None) -> str:
    """Measured/entered inputs with their sources (the report keeps them apart from the projected results below)."""
    if not inputs:
        return "<p>No input-source snapshot was stored with this experiment (created before source tracking).</p>"
    rows = []
    for i in inputs:
        s = i.get("source") or {}
        vals = ", ".join(f"{k}={v}" + (f" {i['units'][k]}" if i.get("units", {}).get(k) else "") for k, v in (i.get("values") or {}).items()
                         if v is not None and k != "periods")
        rows.append([i["input"], vals, s.get("source_name", ""), s.get("document") or "", s.get("collected_on") or "",
                     " / ".join(x for x in (s.get("effective_from"), s.get("effective_to")) if x), i.get("status", {}).get("label", "")])
    return _table(["input", "values", "source", "document", "collected", "effective", "status"], rows)
def render(name: str, hypothesis: str, tariff: dict, rec: dict, design: dict, runs: list[dict], fx: dict, report: dict,
           inputs: list[dict] | None = None) -> str:
    best = [[k, v] for k, v in (rec.get("setup_text") or {}).items()]
    eff = [[x["term"], f"{x['low_to_high']:+.3f}" if x["low_to_high"] is not None else "", f"{x['p_value']:.3g}" if x["p_value"] is not None else "n/a",
            "yes" if x["significant"] else "no"] for x in fx.get("effects", [])]  # fmt: skip
    an = fx.get("anova")
    anova = [[r["term"], f"{r['sum_squares']:.4f}", f"{r['df']:g}", f"{r['f_statistic']:.3f}" if r["f_statistic"] is not None else "",
              f"{r['p_value']:.3g}" if r["p_value"] is not None else ""] for r in (an["rows"] if an else [])]  # fmt: skip
    rows = [[r["order"] + 1, r["status"], *(r["schedule"].get(k, "") for k in ("charge_start", "charge_power_kw", "appliance_start") if r["schedule"]),
             f"{r['outcomes'].get('energy_cost', float('nan')):.2f}" if r["outcomes"] else "", f"{r['outcomes'].get('energy_kwh', float('nan')):.1f}" if r["outcomes"] else "",
             f"{r['outcomes'].get('peak_power_kw', float('nan')):.1f}" if r["outcomes"] else "", f"{r['outcomes'].get('departure_charge_pct', float('nan')):.0f}" if r["outcomes"] else "",
             {True: "yes", False: "NO", None: ""}[r["feasible"]]] for r in runs]  # fmt: skip
    periods = [[p.get("label") or "", f"{p['start']:g}-{p['end']:g} h", f"{p['price']:.3f}"] for p in tariff["periods"]]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(name)} - WATTWISE LAB report</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#14231c}}h1{{margin-bottom:.2rem}}
.warn{{background:#fff7e0;border:1px solid #e0b000;padding:.6rem 1rem;border-radius:8px}}table{{border-collapse:collapse;width:100%;margin:.5rem 0 1.2rem}}
th,td{{border:1px solid #cfdad3;padding:.25rem .5rem;text-align:left;font-variant-numeric:tabular-nums}}th{{background:#eef5f0}}
.best{{background:#e6f6ee;border:1px solid #1f9d66;padding:.6rem 1rem;border-radius:8px}}</style></head><body>
<h1>{e(name)}</h1><p><em>WATTWISE LAB experiment report (TRIALFORGE report v{e(str(report.get("report_version", "")))})</em></p>
<p class="warn"><strong>{e(SIMULATED_NOTICE)}</strong></p>
<h2>Inputs (your data and cited sources)</h2>{_sources(inputs)}
<h2>Hypothesis</h2><p>{e(hypothesis or "(none stated)")}</p>
<h2>Recommendation (projected, not measured)</h2><div class="best"><strong>{e(rec.get("headline", ""))}</strong>{_table(["parameter", "value"], best) if best else ""}
<ul>{"".join(f"<li>{e(x)}</li>" for x in rec.get("reasons", []))}</ul></div>
<h2>Tariff: {e(tariff["name"])}</h2>{_table(["period", "hours", "price per kWh"], periods)}<p>Demand charge per kW of daily peak: {e(str(tariff.get("demand_charge_per_kw", 0)))}</p>
<h2>Design</h2><p>{e(str(design.get("method", "")))}; {design.get("runs_planned", "")} planned schedules; seed {e(str(design.get("seed", "")))}.</p>
<h2>Factor effects on daily cost (low to high of tested range)</h2>{_table(["factor", "cost change", "p", "significant"], eff)}
<h2>ANOVA (Type II)</h2>{_table(["term", "SS", "df", "F", "p"], anova) if anova else "<p>Not available (insufficient residual degrees of freedom).</p>"}
<h2>All schedules (projected outcomes unless imported from a meter)</h2>{_table(["#", "status", "EV start", "charger kW", "appliances", "cost", "kWh", "peak kW", "charge %", "feasible"], rows)}
<h2>Limitations</h2><ul>{"".join(f"<li>{e(x)}</li>" for x in rec.get("caveats", []) + report.get("limitations", []))}</ul>
<p><small>Generated {e(str(report.get("generated_at", "")))} - spec hash {e(str(report.get("reproducibility", {}).get("specification_hash", ""))[:16])}</small></p></body></html>"""
