"""Turns SDK analysis output into engineer-readable findings and a justified setup recommendation."""

from __future__ import annotations

from typing import Any

from trialforge import AnalysisResult, ExperimentResult, RunStatus

from f1lab import notice_for
from f1lab.adapters.vehicle_model import FACTORS

LABEL = {k: v["label"] for k, v in FACTORS.items()}


def run_outcomes(run) -> dict[str, float]:
    """Run-level outcome per metric from the SDK's processed measurements."""
    out: dict[str, list[float]] = {}
    for p in run.processed_measurements:
        if isinstance(p.value, (int, float)) and not isinstance(p.value, bool):
            out.setdefault(p.metric, []).append(float(p.value))
    return {k: sum(v) / len(v) for k, v in out.items()}


def factor_effects(analysis: AnalysisResult, alpha: float, metric: str = "lap_time") -> dict[str, Any]:
    """Main effects, interactions and ANOVA for ``metric`` from the SDK's regression/ANOVA output.

    Regression numeric factors are coded to [-1, +1], so ``2 * coefficient`` is the change from the low to the high end
    of the tested range (seconds for lap time; negative = faster).
    """
    reg = next((r for r in analysis.regression if r.metric == metric), None)
    effects: list[dict[str, Any]] = []
    inter: list[dict[str, Any]] = []
    if reg:
        for c in reg.coefficients:
            if c.term == "Intercept":
                continue
            row = {
                "term": c.term,
                "low_to_high": 2 * c.estimate if ":" not in c.term and "^" not in c.term else None,
                "coefficient": c.estimate,
                "ci_95": [2 * c.ci_95[0], 2 * c.ci_95[1]] if c.ci_95 and ":" not in c.term and "^" not in c.term else (list(c.ci_95) if c.ci_95 else None),
                "p_value": c.p_value,
                "significant": c.p_value is not None and c.p_value < alpha,
            }  # fmt: skip
            (inter if ":" in c.term else effects).append(row)
    effects.sort(key=lambda e: abs(e["low_to_high"] or 0), reverse=True)
    anova = next((t for t in analysis.anova if t.metric == metric and t.kind == "factorial"), None)
    return {
        "effects": effects,
        "interactions": inter,
        "anova": anova.model_dump(mode="json") if anova else None,
        "r_squared": reg.r_squared if reg else None,
        "adjusted_r_squared": reg.adjusted_r_squared if reg else None,
        "residual_df": reg.residual_df if reg else None,
        "regression_warnings": reg.warnings if reg else [],
        "diagnostics": {k: v for k, v in (reg.diagnostics.items() if reg else []) if k != "residuals"},
        "significant_factors": [e["term"] for e in effects if e["significant"]],
    }


def recommendation(result: ExperimentResult, analysis: AnalysisResult, alpha: float = 0.05) -> dict[str, Any]:
    """Best feasible observed setup with the evidence behind it (never from an infeasible run)."""
    spec = result.spec
    min_stab = next((c.minimum for c in spec.constraints if c.metric == "stability"), None)
    runs = {r.run_id: r for r in result.runs}
    done = [r for r in result.runs if r.status == RunStatus.COMPLETED]
    mode = spec.metadata.get("mode", "synthetic")
    caveats = [notice_for(mode)]
    fx = factor_effects(analysis, alpha)
    if not done:
        return {"status": "no_data", "headline": "No completed runs yet.", "reasons": [], "caveats": caveats, **{"effects": fx}}
    if not analysis.best_run_id:
        return {
            "status": "no_feasible",
            "headline": f"No run satisfied the stability constraint (minimum {min_stab}).",
            "reasons": ["Every completed run violated at least one constraint.",
                        "Relax the constraint or explore setups with more downforce / better balance."],
            "caveats": caveats,
            "effects": fx,
        }  # fmt: skip
    best = runs[analysis.best_run_id]
    o = run_outcomes(best)
    first = (f"Fastest feasible observed setup: mean lap {o['lap_time']:.3f} s over its stint, stability {o.get('stability', float('nan')):.1f}"
             + (f" (minimum {min_stab:g})." if min_stab is not None else "."))
    reasons = [
        first,
        f"{len(analysis.feasible_run_ids)} of {len(analysis.feasible_run_ids) + len(analysis.infeasible_runs)} analysed runs were feasible; "
        "constraint-violating runs are never recommended, even when faster.",
    ]  # fmt: skip
    ctl = result.plan.control_configuration
    ctl_runs = [r for r in done if r.configuration == ctl]
    if ctl_runs:
        base = sum(run_outcomes(r)["lap_time"] for r in ctl_runs) / len(ctl_runs)
        reasons.append(f"Improvement over the control setup (mean of {len(ctl_runs)} run(s)): {base - o['lap_time']:+.3f} s.")
    faster_infeasible = [i for i in analysis.infeasible_runs if i["objectives"].get("lap_time", 1e9) < o["lap_time"]]
    if faster_infeasible:
        f = min(faster_infeasible, key=lambda i: i["objectives"]["lap_time"])
        reasons.append(f"Faster setups existed ({f['objectives']['lap_time']:.3f} s) but violated the constraint, "
                       f"so the stability limit costs "
                       f"{o['lap_time'] - f['objectives']['lap_time']:.3f} s.")  # fmt: skip
    sig = [e for e in fx["effects"] if e["significant"]]
    for e in sig[:4]:
        d = "faster" if e["low_to_high"] < 0 else "slower"
        reasons.append(f"{LABEL.get(e['term'], e['term'])}: moving from the low to the high end of the tested range "
                       f"makes laps {abs(e['low_to_high']):.2f} s {d} (p={e['p_value']:.3g}).")  # fmt: skip
    for e in [i for i in fx["interactions"] if i["significant"]][:2]:
        reasons.append(f"Interaction {e['term'].replace(':', ' x ')} is significant (p={e['p_value']:.3g}); "
                       "set those factors together.")  # fmt: skip
    if not sig:
        caveats.append("No factor reached significance at the chosen level: the recommendation rests on direct observation only.")
    seq = [r for r in done if r.metadata.get("sequential_iteration")]
    if best.metadata.get("sequential_iteration"):
        reasons.append("This setup was proposed by the optimiser (model-based refinement) and then measured.")
    if result.plan.design_info.get("p", 0):
        caveats.append(f"Screening used a fractional factorial (resolution {result.plan.design_info.get('resolution')}): "
                       "some two-factor interactions are aliased with each other.")  # fmt: skip
    if fx["residual_df"] is not None and fx["residual_df"] < 5:
        caveats.append(f"Only {int(fx['residual_df'])} residual degrees of freedom: inference is weak; add replicates.")
    if any(o2.get("lap_time") is None for o2 in [o]):
        caveats.append("Lap time missing for the best run.")
    caveats.extend(analysis.warnings[:3])
    caveats.append("Associations from this experiment describe the synthetic model; they say nothing about a real car.")
    _ = seq
    return {
        "status": "recommended",
        "headline": f"Recommended setup: lap {o['lap_time']:.3f} s at stability {o.get('stability', float('nan')):.1f}",
        "run_id": best.run_id,
        "setup": {k: round(v, 3) for k, v in best.configuration.items()},
        "expected": {k: round(v, 3) for k, v in o.items()},
        "reasons": reasons,
        "caveats": caveats,
        "effects": fx,
    }  # fmt: skip
