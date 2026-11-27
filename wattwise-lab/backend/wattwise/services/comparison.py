"""'My actual electricity usage vs optimised schedule' (Task K6).

Every number carries a provenance label:
  historical_measured          imported meter readings
  historical_measured_priced   those readings priced with the tariff (compare with a real bill before trusting)
  owner_entered_base_load      the constant always-on load the owner entered (no meter baseline)
  projected                    a model estimate for a schedule (simulator output)
  measured_schedule            readings recorded after the household actually ran that schedule (imported to a run)
Savings are 'projected' unless a measured schedule exists; an experiment without a meter baseline never produces a
savings claim against the household's real usage.
"""
from __future__ import annotations

import statistics
from typing import Any

from wattwise.adapters.household_model import FACTORS, baseline_setup, household_catalog, simulate
from wattwise.services import narrative


def _sched(cfg: dict[str, float], hh: dict[str, Any]) -> str:
    catalog = household_catalog(hh)
    has_ev = hh.get("ev") is not None
    full = {**baseline_setup(hh), **cfg}
    full = {k: v for k, v in full.items() if has_ev or k in catalog}  # no EV: EV-only values are not shown
    return ", ".join(f"{catalog.get(k, FACTORS.get(k, {})).get('label', k)} {v}" for k, v in narrative.schedule_text(full, catalog).items())

def _ev_pct(hh: dict[str, Any], value: float | None, prov: str) -> dict[str, Any]:
    """The EV row is omitted for a household without an EV."""
    return {"ev_charge_at_departure_pct": _v(value, prov, "%")} if hh.get("ev") is not None else {}


LABELS = {
    "historical_measured": "MEASURED (your imported meter readings)",
    "historical_measured_priced": "MEASURED usage x selected tariff (check against your bill)",
    "owner_entered_base_load": "OWNER-ENTERED constant always-on load (not measured)",
    "projected": "PROJECTED (model estimate)",
    "measured_schedule": "MEASURED SCHEDULE (readings after actually running it)",
}


def _v(value: Any, provenance: str, unit: str = "", note: str | None = None) -> dict[str, Any]:
    out = {"value": round(value, 4) if isinstance(value, float) else value, "provenance": provenance, "unit": unit}
    if note:
        out["note"] = note
    return out


def build(lab, import_id: str, exp_id: str) -> dict[str, Any]:
    res = lab._result(exp_id)
    hh = lab._hh(res)
    if hh.get("legacy_inputs"):
        from wattwise.services.lab import Conflict
        raise Conflict(f"experiment {exp_id} was {lab.LEGACY_NOTICE}")
    tariff = hh["tariff"]
    calibrated_from = (hh.get("baseline_source") or {}).get("import_id")
    calibrated_here = calibrated_from == import_id
    base_prov = "projected"
    hist = lab.meter_baseline(import_id, tariff.get("id") if tariff.get("id") not in (None, "inline") else None)
    if "baseline_cost" not in hist:  # inline tariff on the experiment: price the history with it directly
        from wattwise.services import baseline as B

        meta = lab.meters.get(import_id)
        hist = {**B.analyse(lab.meters.readings(import_id), meta["timezone"], tariff), "import": hist["import"]}
    complete_days = [d for d in hist["days"] if d["complete"]]
    cost = hist["baseline_cost"]
    cd = hist.get("complete_days_cost")  # averages use complete days only (partial days would understate a day)
    hist_daily_cost = cd["mean_daily"] if cd else None
    hist_daily_kwh = hist["daily_energy_kwh"]["mean"]
    per_day_by_period = cd["cost_by_period_per_day"] if cd else {}

    # projected: today's schedule and the best completed run, both on this experiment's household (no noise)
    base_sim = simulate(baseline_setup(hh), hh)["metrics"]
    rows = [lab._run_row(r) for r in res.runs]
    done = [r for r in rows if r["status"] == "completed" and r["feasible"] and "energy_cost" in r["outcomes"]]
    projected = [r for r in done if not r["imported"]]
    measured = [r for r in done if r["imported"]]
    best = min(projected, key=lambda r: r["outcomes"]["energy_cost"]) if projected else None
    best_sim = simulate(best["configuration"], hh)["metrics"] if best else None
    profile_prov = "historical_measured" if hh.get("base_profile_kw") else "owner_entered_base_load"

    savings: dict[str, Any] = {}
    if best_sim:
        savings["projected_vs_current_schedule"] = _v(base_sim["energy_cost"] - best_sim["energy_cost"], "projected", "currency/day",
                                                      "model vs model on the same household; not a measured saving")
        if calibrated_here:
            savings["projected_vs_measured_history"] = _v(None if hist_daily_cost is None else hist_daily_cost - best_sim["energy_cost"], "projected",
                                                          "currency/day", "your measured average daily cost (complete days) minus the model's estimate for "
                                                          "the best schedule" if hist_daily_cost is not None else "not estimated: the import has no complete day")
        else:
            savings["projected_vs_measured_history"] = _v(None, "projected", "currency/day",
                                                          "not estimated: this experiment did not start from this import's measured baseline "
                                                          f"({'owner-entered constant base load' if not calibrated_from else 'calibrated from ' + calibrated_from})")
    if measured:
        mbest = min(measured, key=lambda r: r["outcomes"]["energy_cost"])
        savings["measured_vs_measured_history"] = _v(None if hist_daily_cost is None else hist_daily_cost - mbest["outcomes"]["energy_cost"],
                                                     "measured_schedule", "currency/day",
                                                     f"run {mbest['run_id']}: one measured day vs your historical average; repeat over several days")
    else:
        savings["measured_vs_measured_history"] = _v(None, "measured_schedule", "currency/day",
                                                     "no measured schedule yet: run the recommended schedule at home and import that day's readings to the run")
    by_period = {k: {"historical_per_day": _v(per_day_by_period.get(k), "historical_measured_priced", "currency",
                                              None if k in per_day_by_period else "no complete day in this period"),
                     "projected_best": _v((best_sim or {}).get("cost_by_period", {}).get(k, {}).get("cost", 0.0), "projected", "currency") if best_sim else None}
                 for k, v in cost["cost_by_period"].items()}  # fmt: skip
    peaks = [d["peak_kw"] for d in complete_days]
    out = {
        "experiment": {"id": exp_id, "name": res.spec.name, "baseline": "calibrated" if hh.get("base_profile_kw") else "owner_entered_base_load",
                       "calibrated_from_import": calibrated_from, "tariff": tariff.get("name")},
        "import": hist["import"], "labels": LABELS,
        "historical": {
            "days": [{"date": d["date"], "kwh": _v(d["kwh"], "historical_measured", "kWh"), "peak_kw": _v(d["peak_kw"], "historical_measured", "kW"),
                      "complete": d["complete"]} for d in hist["days"]],
            "mean_daily_kwh": _v(hist_daily_kwh, "historical_measured", "kWh"),
            "mean_daily_cost": _v(hist_daily_cost, "historical_measured_priced", "currency",
                                  None if hist_daily_cost is not None else "not estimated: the import has no complete day"),
            "total_cost": _v(cost["total"], "historical_measured_priced", "currency", cost["note"]),
            "peak_kw": _v(hist["peak_demand"]["kw"], "historical_measured", "kW", hist["peak_demand"]["note"]),
            "mean_daily_peak_kw": _v(statistics.mean(peaks) if peaks else None, "historical_measured", "kW"),
            "coverage": hist["coverage"], "warnings": hist["warnings"],
        },
        "schedules": {
            "current": {"schedule": _sched({}, hh), "cost": _v(base_sim["energy_cost"], base_prov, "currency/day"),
                        "kwh": _v(base_sim["energy_kwh"], base_prov, "kWh"), "peak_kw": _v(base_sim["peak_power_kw"], base_prov, "kW"),
                        **_ev_pct(hh, base_sim.get("departure_charge_pct"), base_prov)},
            "best_projected": None if not best_sim else {
                "run_id": best["run_id"], "schedule": _sched(best["configuration"], hh), "cost": _v(best_sim["energy_cost"], "projected", "currency/day"),
                "kwh": _v(best_sim["energy_kwh"], "projected", "kWh"), "peak_kw": _v(best_sim["peak_power_kw"], "projected", "kW"),
                **_ev_pct(hh, best_sim.get("departure_charge_pct"), "projected")},
            "measured": [{"run_id": r["run_id"], "schedule": _sched(r["configuration"], hh), "cost": _v(r["outcomes"]["energy_cost"], "measured_schedule", "currency/day"),
                          "kwh": _v(r["outcomes"].get("energy_kwh"), "measured_schedule", "kWh"), "peak_kw": _v(r["outcomes"].get("peak_power_kw"), "measured_schedule", "kW"),
                          **_ev_pct(hh, r["outcomes"].get("departure_charge_pct"), "measured_schedule")} for r in measured],
        },
        "cost_by_period": by_period,
        "savings": savings,
        "input_sources": res.spec.metadata["lab"].get("input_sources"),
        "assumptions": [
            f"background load in the projections: {LABELS[profile_prov]}",
            *((hh.get("baseline_source") or {}).get("notes") or []),
            "projections are one simulated day; historical values are averages over the complete days of the import",
            "historical cost = your readings x the experiment's tariff; it is only as accurate as the tariff you entered",
            "EV and appliance parameters are the values you entered with their sources (see input_sources), not measured",
            "a projected saving is not evidence of a real saving: only a measured schedule is",
        ],
    }  # fmt: skip
    return out
