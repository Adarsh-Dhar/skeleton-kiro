"""Turns SDK analysis output into plain-language findings and a justified schedule recommendation."""

from __future__ import annotations

from typing import Any

from trialforge import AnalysisResult, ExperimentResult, RunStatus

from wattwise import SIMULATED_NOTICE
from wattwise.adapters.household_model import APPLIANCE_KEY_PREFIX, FACTORS, baseline_setup, clock, household_catalog, simulate

LABEL = {k: v["label"] for k, v in FACTORS.items()}


def _meta(name: str, catalog: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    if catalog is not None and name in catalog:
        return catalog[name]
    if name in FACTORS:
        return FACTORS[name]
    return FACTORS["appliance_start"] if name.startswith(APPLIANCE_KEY_PREFIX) else None  # per-appliance start: same unit and clock format


def fmt_value(name: str, v: float, catalog: dict[str, dict[str, Any]] | None = None) -> str:
    f = _meta(name, catalog)
    if f is None:
        return f"{v:g}"
    return clock(v) if f.get("clock") else f"{v:.1f} {f['unit']}"


def schedule_text(cfg: dict[str, float], catalog: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """Readable values for a setup. ``catalog`` is the household's factor catalog (per-appliance start factors are formatted like ``appliance_start``)."""
    return {k: fmt_value(k, v, catalog) for k, v in cfg.items()}


def run_outcomes(run) -> dict[str, float]:
    out: dict[str, list[float]] = {}
    for p in run.processed_measurements:
        if p.metric != "grid_power" and isinstance(p.value, (int, float)) and not isinstance(p.value, bool):
            out.setdefault(p.metric, []).append(float(p.value))
    return {k: sum(v) / len(v) for k, v in out.items()}


def factor_effects(analysis: AnalysisResult, alpha: float, metric: str = "energy_cost") -> dict[str, Any]:
    """Regression effects on ``metric``: numeric factors are coded to [-1,+1], so ``2 * coefficient`` is the change from the low to the high
    end of the tested range (daily cost units; negative = cheaper)."""
    reg = next((r for r in analysis.regression if r.metric == metric), None)
    effects: list[dict[str, Any]] = []
    inter: list[dict[str, Any]] = []
    if reg:
        for c in reg.coefficients:
            if c.term == "Intercept":
                continue
            linear = ":" not in c.term and "^" not in c.term
            row = {
                "term": c.term, "low_to_high": 2 * c.estimate if linear else None, "coefficient": c.estimate,
                "ci_95": [2 * c.ci_95[0], 2 * c.ci_95[1]] if c.ci_95 and linear else (list(c.ci_95) if c.ci_95 else None),
                "p_value": c.p_value, "significant": c.p_value is not None and c.p_value < alpha,
            }  # fmt: skip
            (inter if ":" in c.term else effects).append(row)
    effects.sort(key=lambda e: abs(e["low_to_high"] or 0), reverse=True)
    anova = next((t for t in analysis.anova if t.metric == metric and t.kind == "factorial"), None)
    return {
        "effects": effects, "interactions": inter, "anova": anova.model_dump(mode="json") if anova else None,
        "r_squared": reg.r_squared if reg else None, "adjusted_r_squared": reg.adjusted_r_squared if reg else None,
        "residual_df": reg.residual_df if reg else None, "regression_warnings": reg.warnings if reg else [],
        "significant_factors": [e["term"] for e in effects if e["significant"]],
    }  # fmt: skip


def recommendation(result: ExperimentResult, analysis: AnalysisResult, household: dict[str, Any], alpha: float = 0.05) -> dict[str, Any]:
    """Cheapest *feasible* measured schedule with the evidence behind it."""
    spec = result.spec
    has_ev = household.get("ev") is not None
    catalog = household_catalog(household)
    label = {**LABEL, **{k: v["label"] for k, v in catalog.items()}}
    desired = next((c.minimum for c in spec.constraints if c.metric == "departure_charge_pct"), None)
    done = [r for r in result.runs if r.status == RunStatus.COMPLETED]
    caveats = [SIMULATED_NOTICE]
    fx = factor_effects(analysis, alpha)
    if not done:
        return {"status": "no_data", "headline": "No completed runs yet.", "reasons": [], "caveats": caveats, "effects": fx}
    if not analysis.best_run_id:
        need = f" the required departure charge of {desired:g}%" if desired is not None else " the constraints"
        return {"status": "no_feasible", "headline": f"No schedule reached{need}.",
                "reasons": ["Every completed schedule left the car below the required charge.", "Try earlier charging, higher charger power, or a lower requirement."],
                "caveats": caveats, "effects": fx}  # fmt: skip
    best = next(r for r in result.runs if r.run_id == analysis.best_run_id)
    o = run_outcomes(best)
    bs = baseline_setup(household)
    base = simulate(bs, household)["metrics"]
    saving = base["energy_cost"] - o["energy_cost"]
    plug_in = f"plug in at {clock(bs['charge_start'])}, {bs['charge_power_kw']:.1f} kW, " if has_ev else ""
    reasons = [
        f"Cheapest feasible schedule: {o['energy_cost']:.2f} per day, {o['energy_kwh']:.1f} kWh, peak {o['peak_power_kw']:.1f} kW, "
        f"{o['departure_charge_pct']:.0f}% charge at departure" + (f" (required {desired:g}%)." if desired is not None else ".")
        if has_ev and "departure_charge_pct" in o else ".",
        f"Baseline schedule ({plug_in}appliances at {clock(bs['appliance_start'])}) costs "
        f"{base['energy_cost']:.2f}: saving {saving:.2f} per day ({100 * saving / base['energy_cost']:.0f}%) at "
        f"{base['peak_power_kw']:.1f} kW baseline peak vs {o['peak_power_kw']:.1f} kW.",
    ]  # fmt: skip
    if has_ev:
        reasons.append(f"{len(analysis.feasible_run_ids)} of {len(analysis.feasible_run_ids) + len(analysis.infeasible_runs)} analysed schedules met the departure-charge requirement; "
                       "schedules that violate it are never recommended, even when cheaper.")
    cheaper_bad = [i for i in analysis.infeasible_runs if i["objectives"].get("energy_cost", 1e9) < o["energy_cost"]]
    if cheaper_bad:
        c = min(cheaper_bad, key=lambda i: i["objectives"]["energy_cost"])
        v = c["violations"][0].get("value")
        reasons.append(f"Cheaper schedules existed (down to {c['objectives']['energy_cost']:.2f}) but left the car at only "
                       f"{v:.0f}% at departure, so the requirement costs {o['energy_cost'] - c['objectives']['energy_cost']:.2f} per day."
                       if v is not None else f"Cheaper schedules existed (down to {c['objectives']['energy_cost']:.2f}) but violated the requirement.")  # fmt: skip
    for e in [x for x in fx["effects"] if x["significant"]][:3]:
        reasons.append(f"{label.get(e['term'], e['term'])}: moving from the low to the high end of the tested range changes daily cost by "
                       f"{e['low_to_high']:+.2f} (p={e['p_value']:.3g}).")  # fmt: skip
    for e in [x for x in fx["interactions"] if x["significant"]][:2]:
        reasons.append(f"Interaction {e['term'].replace(':', ' x ')} is significant (p={e['p_value']:.3g}): set those together.")
    if best.metadata.get("sequential_iteration"):
        reasons.append("This schedule was proposed by the optimiser and then measured.")
    if not [e for e in fx["effects"] if e["significant"]]:
        caveats.append("No factor reached significance at the chosen level: the recommendation rests on direct observation only.")
    if fx["residual_df"] is not None and fx["residual_df"] < 5:
        caveats.append(f"Only {int(fx['residual_df'])} residual degrees of freedom: inference is weak; add runs.")
    caveats.extend(analysis.warnings[:3])
    caveats.append("Costs come from a simulated household and the tariff you entered; real savings depend on real usage and billing rules.")
    return {
        "status": "recommended",
        "headline": f"Cheapest feasible schedule: {o['energy_cost']:.2f} per day ({100 * saving / base['energy_cost']:.0f}% below baseline)",
        "run_id": best.run_id, "setup": {k: round(v, 3) for k, v in best.configuration.items()}, "setup_text": schedule_text(best.configuration, catalog),
        "expected": {k: round(v, 3) for k, v in o.items()},
        "baseline": {k: round(float(base[k]), 3) for k in ("energy_cost", "energy_kwh", "peak_power_kw", "departure_charge_pct") if k in base},
        "reasons": reasons, "caveats": caveats, "effects": fx,
    }  # fmt: skip
