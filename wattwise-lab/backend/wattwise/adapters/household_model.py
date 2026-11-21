"""Household electricity model: tariff, EV charging session, appliances, base load. Pure Python, no SDK dependency.

Time convention: *absolute clock hours* on a 24 h window that starts at 16:00. Values 16..24 are that evening; values 24..40 are the next
day (e.g. 31.5 = 07:30 tomorrow). ``clock()`` formats them. The simulation uses 15-minute steps.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from wattwise.adapters import tariff_model as TM

STEP_H = 0.25
WINDOW_START = 16.0
STEPS = 96
FACTORS: dict[str, dict[str, Any]] = {
    "charge_start": {"low": 17.0, "high": 31.0, "unit": "h", "label": "EV charging start", "default": 19.0, "clock": True},
    "charge_power_kw": {"low": 1.4, "high": 11.0, "unit": "kW", "label": "Charger power", "default": 7.4, "clock": False},
    "appliance_start": {"low": 17.0, "high": 30.0, "unit": "h", "label": "Flexible appliances start", "default": 19.0, "clock": True},
}
BASELINE = {k: v["default"] for k, v in FACTORS.items()}
METRIC_UNITS = {"energy_cost": "currency", "energy_kwh": "kWh", "peak_power_kw": "kW", "departure_charge_pct": "percent", "ev_energy_kwh": "kWh", "grid_power": "kW"}

DEFAULT_TARIFFS: list[dict[str, Any]] = [
    {"id": "time-of-use", "name": "Time-of-use", "demand_charge_per_kw": 0.0,
     "periods": [{"label": "Night", "start": 23, "end": 7, "price": 0.11}, {"label": "Day", "start": 7, "end": 16, "price": 0.22},
                 {"label": "Peak", "start": 16, "end": 21, "price": 0.36}, {"label": "Evening", "start": 21, "end": 23, "price": 0.22}]},
    {"id": "flat-rate", "name": "Flat rate", "demand_charge_per_kw": 0.0, "periods": [{"label": "All day", "start": 0, "end": 24, "price": 0.25}]},
    {"id": "night-saver-demand", "name": "Night saver + demand charge", "demand_charge_per_kw": 0.60,
     "periods": [{"label": "Night", "start": 0, "end": 6, "price": 0.09}, {"label": "Standard", "start": 6, "end": 24, "price": 0.28}]},
]  # fmt: skip
DEFAULT_EV = {"battery_kwh": 60.0, "initial_soc_pct": 30.0, "desired_charge_pct": 80.0, "departure_hour": 7.5, "efficiency": 0.92}
DEFAULT_APPLIANCES = [
    {"name": "Dishwasher", "power_kw": 1.1, "duration_h": 2.0, "flexible": True, "fixed_start": 20.0},
    {"name": "Washing machine", "power_kw": 0.5, "duration_h": 1.5, "flexible": True, "fixed_start": 19.0},
    {"name": "Tumble dryer", "power_kw": 2.0, "duration_h": 1.5, "flexible": False, "fixed_start": 18.0},
]  # fmt: skip


def clock(h: float) -> str:
    """Format an absolute hour as HH:MM (adds '+1d' after midnight)."""
    m = round(h * 60)
    return f"{(m // 60) % 24:02d}:{m % 60:02d}" + ("+1d" if m >= 24 * 60 else "")


def rng_for(*parts: Any) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def _in_period(p: dict, hod: float) -> bool:
    s, e = p["start"], p["end"]
    return (s <= hod < e) if s < e else (hod >= s or hod < e)


def _coverage_issues(periods: list[dict], prefix: str = "") -> list[str]:
    issues: list[str] = []
    gaps, overlaps = [], []
    for k in range(96):
        hod = k * STEP_H
        n = sum(_in_period(p, hod) for p in periods)
        (gaps if n == 0 else overlaps if n > 1 else []).append(clock(hod))
    if gaps:
        issues.append(f"{prefix}periods leave gaps (no price) at {', '.join(gaps[:4])}{'...' if len(gaps) > 4 else ''}")
    if overlaps:
        issues.append(f"{prefix}periods overlap at {', '.join(overlaps[:4])}{'...' if len(overlaps) > 4 else ''}")
    return issues


def validate_tariff(t: dict[str, Any]) -> list[str]:
    """Return a list of problems (empty = valid): periods must tile the 24 h day exactly (for every month/day type they
    apply to), with non-negative prices. Optional extensions are checked by ``tariff_model.validate_extensions``."""
    issues: list[str] = []
    periods = t.get("periods") or []
    if not str(t.get("name", "")).strip():
        issues.append("tariff name is required")
    if not periods:
        return issues + ["at least one price period is required"]
    if len(periods) > 12 * (4 if TM.date_dependent(t) else 1):
        issues.append("at most 12 periods are supported" if not TM.date_dependent(t) else "at most 48 periods are supported")
    for i, p in enumerate(periods, 1):
        try:
            s, e, price = float(p["start"]), float(p["end"]), float(p["price"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"period {i}: start, end and price must be numbers")
            continue
        if not (0 <= s <= 24 and 0 <= e <= 24) or s == e:
            issues.append(f"period {i}: start/end must be hours in [0, 24] and differ")
        if price < 0 or price > 10:
            issues.append(f"period {i}: price must be between 0 and 10 per kWh")
    issues += TM.validate_extensions(t)
    if issues:
        return issues
    for month, day_type in TM.contexts(t):
        applicable = [p for p in periods if TM.applies(p, month, day_type)]
        prefix = "" if month is None and day_type is None else f"{'month ' + str(month) + ' ' if month else ''}{day_type or ''}: ".replace("  ", " ")
        issues += _coverage_issues(applicable, prefix)
        if len(issues) > 6:
            break
    if not 0 <= float(t.get("demand_charge_per_kw", 0) or 0) <= 100:
        issues.append("demand charge must be between 0 and 100 per kW")
    return issues


def price_at(tariff: dict[str, Any], abs_hour: float) -> float:
    hod = abs_hour % 24
    if TM.date_dependent(tariff):
        return float(TM.period_for(tariff, hod, *TM.simulation_context(tariff))["price"])
    for p in tariff["periods"]:
        if _in_period(p, hod):
            return float(p["price"])
    raise ValueError(f"tariff has no period for hour {hod}")


def period_label(tariff: dict[str, Any], abs_hour: float) -> str:
    hod = abs_hour % 24
    if TM.date_dependent(tariff):
        p = TM.period_for(tariff, hod, *TM.simulation_context(tariff))
        return p.get("label") or f"{p['start']}-{p['end']}"
    return next((p.get("label") or f"{p['start']}-{p['end']}" for p in tariff["periods"] if _in_period(p, hod)), "?")


def departure_abs(hour_of_day: float) -> float:
    return hour_of_day if hour_of_day >= WINDOW_START else hour_of_day + 24


def simulate(setup: dict[str, float], household: dict[str, Any], rng: random.Random | None = None) -> dict[str, Any]:
    """Simulate one day for a schedule. ``setup`` may omit factors (baseline values are used). Noise only if ``rng`` is given."""
    s = {**BASELINE, **setup}
    ev, tariff, apps = household["ev"], household["tariff"], household["appliances"]
    cap_kwh, eff = ev["battery_kwh"], ev["efficiency"]
    soc = cap_kwh * ev["initial_soc_pct"] / 100
    depart = departure_abs(ev["departure_hour"])
    flex_start = s["appliance_start"]
    soc_at_departure = None
    steps: list[dict[str, Any]] = []
    for i in range(STEPS):
        t = WINDOW_START + i * STEP_H
        hod = t % 24
        profile = household.get("base_profile_kw")  # calibrated from historical meter data (96 steps from 16:00), if selected
        if profile:
            base = float(profile[i])
        else:  # synthetic profile: base load plus evening and morning peaks
            base = household.get("base_load_kw", 0.35) + (1.0 if 18 <= hod < 21 else 0.0) + (0.6 if 6.5 <= hod < 8.5 else 0.0)
        if rng:
            base += abs(rng.gauss(0, 0.03))
        app = sum(a["power_kw"] for a in apps if (flex_start if a["flexible"] else a["fixed_start"]) <= t < (flex_start if a["flexible"] else a["fixed_start"]) + a["duration_h"])
        ev_kw = 0.0
        if s["charge_start"] <= t < depart and soc < cap_kwh - 1e-9:
            frac = soc / cap_kwh
            taper = 1.0 if frac <= 0.8 else max(0.3, 1 - 0.7 * (frac - 0.8) / 0.2)  # charging slows above 80 %
            ev_kw = min(s["charge_power_kw"] * taper, (cap_kwh - soc) / (eff * STEP_H))
            soc += ev_kw * eff * STEP_H
        if soc_at_departure is None and t + STEP_H > depart:
            soc_at_departure = soc / cap_kwh * 100
        steps.append({"t": t, "base": base, "appliances": app, "ev": ev_kw, "kw": base + app + ev_kw, "price": price_at(tariff, t)})
    if soc_at_departure is None:
        soc_at_departure = soc / cap_kwh * 100
    return {"steps": steps, "metrics": summarise([(st["t"], st["kw"]) for st in steps], tariff, ev_kw=[st["ev"] for st in steps], departure_charge_pct=soc_at_departure)}


def summarise(readings: list[tuple[float, float]], tariff: dict[str, Any], *, step_h: float = STEP_H, ev_kw: list[float] | None = None,
              departure_charge_pct: float | None = None) -> dict[str, Any]:
    """Energy, cost (tariff + demand charge), peak and cost by period from (abs_hour, kW) readings.

    Tariffs that use the optional extensions (fixed charges, tiers, taxes, export credits, seasonal/day-type periods) are
    priced by ``_summarise_extended``; original tariffs keep this exact code path (identical costs).
    """
    if TM.is_extended(tariff):
        return _summarise_extended(readings, tariff, step_h=step_h, ev_kw=ev_kw, departure_charge_pct=departure_charge_pct)
    kwh = sum(kw * step_h for _, kw in readings)
    peak = max((kw for _, kw in readings), default=0.0)
    by_period: dict[str, dict[str, float]] = {}
    energy_cost = 0.0
    for t, kw in readings:
        e = kw * step_h
        c = e * price_at(tariff, t)
        energy_cost += c
        d = by_period.setdefault(period_label(tariff, t), {"kwh": 0.0, "cost": 0.0})
        d["kwh"] += e
        d["cost"] += c
    demand = float(tariff.get("demand_charge_per_kw", 0) or 0) * peak
    out = {"energy_kwh": kwh, "energy_cost": energy_cost + demand, "energy_charge": energy_cost, "demand_charge": demand, "peak_power_kw": peak,
           "cost_by_period": by_period}  # fmt: skip
    if ev_kw is not None:
        out["ev_energy_kwh"] = sum(ev_kw) * step_h
    if departure_charge_pct is not None:
        out["departure_charge_pct"] = departure_charge_pct
    return out


def _summarise_extended(readings: list[tuple[float, float]], tariff: dict[str, Any], *, step_h: float, ev_kw: list[float] | None,
                        departure_charge_pct: float | None) -> dict[str, Any]:
    """One simulated day under an extended tariff. Billing-period items (monthly charge, billing-period demand) are prorated."""
    month, day_type = TM.simulation_context(tariff)
    kwh = sum(kw * step_h for _, kw in readings)
    peak = max((kw for _, kw in readings), default=0.0)
    by_period: dict[str, dict[str, float]] = {}
    charge = credit = cum = 0.0
    for t, kw in readings:
        e = kw * step_h
        r = TM.price_interval(tariff, t % 24, e, cum, month, day_type)
        cum += r["import_kwh"]
        charge += r["charge"]
        credit += r["credit"]
        d = by_period.setdefault(r["label"], {"kwh": 0.0, "cost": 0.0})
        d["kwh"] += e
        d["cost"] += r["charge"] - r["credit"]
    parts = TM.finish(tariff, charge, credit, peak, 1.0, single_day=True)
    out = {"energy_kwh": kwh, "energy_cost": parts["total"], "energy_charge": charge, "demand_charge": parts["demand_charge"],
           "fixed_charge": parts["fixed_charge"], "tax": parts["tax"], "export_credit": credit, "peak_power_kw": peak,
           "cost_by_period": by_period}  # fmt: skip
    if ev_kw is not None:
        out["ev_energy_kwh"] = sum(ev_kw) * step_h
    if departure_charge_pct is not None:
        out["departure_charge_pct"] = departure_charge_pct
    return out
