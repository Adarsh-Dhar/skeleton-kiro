"""Self-contained printable HTML report (no external assets)."""

from __future__ import annotations

from html import escape as e

from f1lab import notice_for, provenance_notice


def _table(headers: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{e(str(h))}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>"


def render(name: str, hypothesis: str, rec: dict, design: dict, runs: list[dict], fx: dict, report: dict, mode: str = "synthetic",
           provenance: str | None = None) -> str:
    second, second_head, second_fmt = "stability", "stability", ".1f"
    # one warning box per notice: a mixed experiment shows the synthetic and the imported notice
    if provenance is None:
        notices = [notice_for(mode)]
    elif provenance.startswith("MIXED"):
        notices = [provenance_notice("SYNTHETIC"), provenance_notice("IMPORTED TELEMETRY")]
    else:
        notices = [provenance_notice(provenance)]
    notice_html = "".join(f'<p class="warn"><strong>{e(n)}</strong></p>' for n in notices)
    setup = rec.get("setup") or {}
    best_rows = [[k, v] for k, v in setup.items()]
    eff_rows = [[x["term"], f"{x['low_to_high']:+.3f}" if x["low_to_high"] is not None else "", f"{x['p_value']:.3g}" if x["p_value"] is not None else "n/a",
                 "yes" if x["significant"] else "no"] for x in fx.get("effects", [])]  # fmt: skip
    anova = fx.get("anova")
    anova_rows = [[r["term"], f"{r['sum_squares']:.4f}", f"{r['df']:g}", f"{r['f_statistic']:.3f}" if r["f_statistic"] is not None else "",
                   f"{r['p_value']:.3g}" if r["p_value"] is not None else ""] for r in (anova["rows"] if anova else [])]  # fmt: skip
    run_rows = [[r["order"] + 1, r["status"], *(f"{r['configuration'].get(k, ''):.2f}" if isinstance(r["configuration"].get(k), float) else r["configuration"].get(k, "")
                                                for k in sorted(runs[0]["configuration"])),
                 f"{r['outcomes'].get('lap_time', float('nan')):.3f}" if r["outcomes"] else "", format(r['outcomes'].get(second, float('nan')), second_fmt) if r["outcomes"] else "",
                 {True: "yes", False: "NO", None: ""}[r["feasible"]]] for r in runs] if runs else []  # fmt: skip
    head = ["#", "status", *(sorted(runs[0]["configuration"]) if runs else []), "lap (s)", second_head, "feasible"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(name)} - F1 SETUP LAB report</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#111}}h1{{margin-bottom:.2rem}}
.warn{{background:#fff4d6;border:1px solid #e0b000;padding:.6rem 1rem;border-radius:6px}}table{{border-collapse:collapse;width:100%;margin:.5rem 0 1.2rem}}
th,td{{border:1px solid #ccc;padding:.25rem .5rem;text-align:left;font-variant-numeric:tabular-nums}}th{{background:#f0f0f0}}
.best{{background:#e8f6ea;border:1px solid #2e8b3d;padding:.6rem 1rem;border-radius:6px}}</style></head><body>
<h1>{e(name)}</h1><p><em>F1 SETUP LAB - experiment report (TRIALFORGE report v{e(str(report.get("report_version", "")))})</em></p>
{notice_html}
<h2>Hypothesis</h2><p>{e(hypothesis or "(none stated)")}</p>
<h2>Recommendation</h2><div class="best"><strong>{e(rec.get("headline", ""))}</strong>{_table(["parameter", "value"], best_rows) if best_rows else ""}
<ul>{"".join(f"<li>{e(x)}</li>" for x in rec.get("reasons", []))}</ul></div>
<h2>Design</h2><p>{e(str(design.get("type", "")))}; {design.get("runs_planned", "")} planned runs; seed {e(str(design.get("seed", "")))}.
{e(", ".join(design.get("defining_relation", [])))} {("Resolution " + str(design.get("resolution"))) if design.get("resolution") else ""}</p>
<h2>Factor effects on lap time (low to high of tested range)</h2>{_table(["factor", "seconds", "p", "significant"], eff_rows)}
<h2>ANOVA (Type II)</h2>{_table(["term", "SS", "df", "F", "p"], anova_rows) if anova_rows else "<p>Not available (insufficient residual degrees of freedom).</p>"}
<h2>All runs</h2>{_table(head, run_rows)}
<h2>Limitations</h2><ul>{"".join(f"<li>{e(x)}</li>" for x in rec.get("caveats", []) + report.get("limitations", []))}</ul>
<p><small>Generated {e(str(report.get("generated_at", "")))} - spec hash {e(str(report.get("reproducibility", {}).get("specification_hash", ""))[:16])}</small></p>
</body></html>"""
