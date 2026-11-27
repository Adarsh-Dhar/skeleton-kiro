"""Household electricity model: tariff, EV charging session, appliances, base load. Pure Python, no SDK dependency.
Zero fabricated input data: every input (tariff, EV, appliances, base load, current schedule) comes from the household's own
configuration; nothing falls back to built-in example values. Missing inputs raise ``MissingInput`` instead of being invented.

Time convention: *absolute clock hours* on a 24 h window that starts at 16:00. Values 16..24 are that evening; values 24..40 are the next
day (e.g. 31.5 = 07:30 tomorrow). ``clock()`` formats them. The simulation uses 15-minute steps.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime
from typing import Any

from wattwise.adapters import tariff_model as TM

STEP_H = 0.25
WINDOW_START = 16.0
STEPS = 96
# Schedule variables an experiment may vary, and the widest range it may search. These are search limits, not household data:
# the household's current schedule (its baseline) comes from its own plug-in time, charger and appliance start times.
FACTORS: dict[str, dict[str, Any]] = {
    "charge_start": {"low": 17.0, "high": 31.0, "unit": "h", "label": "EV charging start", "clock": True},
    "charge_power_kw": {"low": 1.4, "high": 11.0, "unit": "kW", "label": "Charger power", "clock": False},
    "appliance_start": {"low": 17.0, "high": 30.0, "unit": "h", "label": "Flexible appliances start", "clock": True},
}
METRIC_UNITS = {"energy_cost": "currency", "energy_kwh": "kWh", "peak_power_kw": "kW", "departure_charge_pct": "percent", "ev_energy_kwh": "kWh", "grid_power": "kW"}

CHARGER_MIN_KW = 1.4
CHARGER_MAX_KW = 350.0
DEFAULT_CHARGER_MAX_KW = 11.0  # upper search limit for charger power when no charger capacity is known (not a household value)
MAX_FLEXIBLE_APPLIANCES = 20
APPLIANCE_KEY_PREFIX = "appliance_start__"


class MissingInput(ValueError):
    """A required household input is not configured. It is reported, never replaced by an invented value."""
class ChargerCapacityError(ValueError):
    """The charging power of a setup exceeds the household's charger capacity."""


class FactorConflict(ValueError):
    """A setup mixes the legacy shared ``appliance_start`` with per-appliance start factors."""


def _slug(name: str) -> str:
    out = "".join(c if c.isalnum() and c.isascii() else "-" for c in str(name).lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:39] or "appliance"


def appliance_id(name: str, explicit: str | None = None) -> str:
    """Explicit id when given (non-blank), otherwise a slug of the appliance name."""
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    return _slug(name)


def appliance_key(app: dict[str, Any]) -> str:
    """Factor name of one appliance's start time: ``appliance_start__<token>`` (token = id with '-' replaced by '_')."""
    aid = appliance_id(app.get("name", ""), app.get("id"))
    return APPLIANCE_KEY_PREFIX + aid.replace("-", "_")


def factor_limits(charger_capacity_kw: float | None = None, appliances: list[dict[str, Any]] | None = None,
                  has_ev: bool = True) -> dict[str, dict[str, Any]]:
    """Factor catalog valid for one household. With default arguments this is a copy of ``FACTORS``."""
    out: dict[str, dict[str, Any]] = {}
    for name, f in FACTORS.items():
        if not has_ev and name in ("charge_start", "charge_power_kw"):
            continue
        f = dict(f)
        if name == "charge_power_kw":
            f["high"] = float(charger_capacity_kw) if charger_capacity_kw is not None else DEFAULT_CHARGER_MAX_KW
        out[name] = f
    template = FACTORS["appliance_start"]
    for app in appliances or []:
        if app.get("flexible"):
            out[appliance_key(app)] = {**template, "label": f"Start: {app.get('name', '')}"}
    return out


def household_catalog(household: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``factor_limits`` for a household dict (capacity, appliances and EV presence read from it)."""
    return factor_limits(household.get("charger_capacity_kw"), household.get("appliances") or [], household.get("ev") is not None)


def baseline_setup(household: dict[str, Any]) -> dict[str, float]:
    """The household's current schedule, from its own inputs: the EV plugs in at its usual plug-in time and charges at the
    charger's capacity; each movable appliance starts at its usual start time (``fixed_start``). Missing inputs are omitted."""
    setup: dict[str, float] = {}
    ev = household.get("ev")
    if ev is not None:
        if ev.get("plug_in_hour") is not None:
            setup["charge_start"] = departure_abs(float(ev["plug_in_hour"]))
        if household.get("charger_capacity_kw") is not None:
            setup["charge_power_kw"] = float(household["charger_capacity_kw"])
    for a in household.get("appliances") or []:
        if a.get("flexible") and a.get("fixed_start") is not None:
            setup[appliance_key(a)] = float(a["fixed_start"])
    return setup
def with_baseline(household: dict[str, Any], setup: dict[str, float]) -> dict[str, float]:
    """``setup`` completed with the current schedule. A shared ``appliance_start`` in ``setup`` replaces the per-appliance usual starts."""
    base = baseline_setup(household)
    if "appliance_start" in setup:
        base = {k: v for k, v in base.items() if not k.startswith(APPLIANCE_KEY_PREFIX) or k in setup}
    return {**base, **setup}


def check_setup_keys(setup: dict[str, Any]) -> None:
    """Raise FactorConflict when a setup has both ``appliance_start`` and per-appliance start keys."""
    per = sorted(k for k in setup if k.startswith(APPLIANCE_KEY_PREFIX))
    if "appliance_start" in setup and per:
        raise FactorConflict(f"setup contains both 'appliance_start' and per-appliance start factors: {', '.join(per)}")


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


def _month_day(tariff: dict[str, Any], abs_hour: float, ctx: TM.WindowContext | None) -> tuple[int | None, str | None]:
    return ctx.for_hour(abs_hour) if ctx is not None else TM.simulation_context(tariff)


def price_at(tariff: dict[str, Any], abs_hour: float, ctx: TM.WindowContext | None = None) -> float:
    hod = abs_hour % 24
    if TM.date_dependent(tariff):
        return float(TM.period_for(tariff, hod, *_month_day(tariff, abs_hour, ctx))["price"])
    for p in tariff["periods"]:
        if _in_period(p, hod):
            return float(p["price"])
    raise ValueError(f"tariff has no period for hour {hod}")


def period_label(tariff: dict[str, Any], abs_hour: float, ctx: TM.WindowContext | None = None) -> str:
    hod = abs_hour % 24
    if TM.date_dependent(tariff):
        p = TM.period_for(tariff, hod, *_month_day(tariff, abs_hour, ctx))
        return p.get("label") or f"{p['start']}-{p['end']}"
    return next((p.get("label") or f"{p['start']}-{p['end']}" for p in tariff["periods"] if _in_period(p, hod)), "?")


def departure_abs(hour_of_day: float) -> float:
    return hour_of_day if hour_of_day >= WINDOW_START else hour_of_day + 24


def _window_ctx(tariff: dict[str, Any], household: dict[str, Any]) -> TM.WindowContext | None:
    """Window context for one simulated day: the household's ``window_start_date`` or a reference date for the tariff's simulation day."""
    if not TM.date_dependent(tariff):
        return None
    start = household.get("window_start_date")
    if isinstance(start, str):
        start = date.fromisoformat(start)
    elif isinstance(start, datetime):
        start = start.date()
    if start is None:
        month, day_type = TM.simulation_context(tariff)
        start = TM.reference_date(int(month or 1), str(day_type or "weekday"))
    return TM.window_context(tariff, start)


def simulate(setup: dict[str, float], household: dict[str, Any]) -> dict[str, Any]:
    """Project one day for a schedule (a model result, never a measurement). ``setup`` may omit factors: the household's current
    schedule is used (see ``baseline_setup``). ``household["ev"]`` may be ``None`` (no EV: no charging, no ``departure_charge_pct``).
    Flexible appliances start at ``appliance_start__<id>`` when given, else at ``appliance_start``, else at their usual start.
    Background load is the measured profile (``base_profile_kw``) or the owner's ``base_load_kw``; without either, ``MissingInput``.
    The result is deterministic: no noise is added.
    """
    check_setup_keys(setup)
    ev, tariff, apps = household.get("ev"), household.get("tariff"), household.get("appliances") or []
    if not tariff:
        raise MissingInput("no tariff is configured: enter the rates from your electricity bill or tariff order")
    profile = household.get("base_profile_kw")  # measured: calibrated from imported meter readings (96 steps from 16:00)
    base_kw = household.get("base_load_kw")
    if not profile and base_kw is None:
        raise MissingInput("background load is missing: import your meter readings or enter your always-on load (base_load_kw)")
    s = with_baseline(household, setup)
    if ev is not None and ("charge_start" not in s or "charge_power_kw" not in s):
        raise MissingInput("the EV needs its usual plug-in time and the charger capacity (charger_power_kw) before charging can be projected")
    cap_limit = household.get("charger_capacity_kw")
    if ev is not None and cap_limit is not None and s["charge_power_kw"] > float(cap_limit):
        raise ChargerCapacityError(f"charging power {s['charge_power_kw']:g} kW exceeds the charger capacity {float(cap_limit):g} kW")
    ctx = _window_ctx(tariff, household)
    if ev is not None:
        cap_kwh, eff = ev["battery_kwh"], ev["efficiency"]
        soc = cap_kwh * ev["initial_soc_pct"] / 100
        depart = departure_abs(ev["departure_hour"])
    else:
        cap_kwh = eff = soc = depart = 0.0
    starts = [(appliance_id(a.get("name", ""), a.get("id")), a,
               s.get(appliance_key(a), s.get("appliance_start", a["fixed_start"])) if a["flexible"] else a["fixed_start"]) for a in apps]
    soc_at_departure = None
    steps: list[dict[str, Any]] = []
    for i in range(STEPS):
        t = WINDOW_START + i * STEP_H
        base = float(profile[i]) if profile else float(base_kw)  # measured profile, else the owner's constant always-on load
        by_app: dict[str, float] = {}
        kws = []
        for aid, a, st in starts:
            kw = a["power_kw"] if st <= t < st + a["duration_h"] else 0.0
            kws.append(kw)
            by_app[aid] = by_app.get(aid, 0.0) + kw
        app = sum(kws)
        ev_kw = 0.0
        if ev is not None:
            if s["charge_start"] <= t < depart and soc < cap_kwh - 1e-9:
                frac = soc / cap_kwh
                taper = 1.0 if frac <= 0.8 else max(0.3, 1 - 0.7 * (frac - 0.8) / 0.2)  # charging slows above 80 %
                ev_kw = min(s["charge_power_kw"] * taper, (cap_kwh - soc) / (eff * STEP_H))
                soc += ev_kw * eff * STEP_H
            if soc_at_departure is None and t + STEP_H > depart:
                soc_at_departure = soc / cap_kwh * 100
        steps.append({"t": t, "base": base, "appliances": app, "ev": ev_kw, "kw": base + app + ev_kw, "price": price_at(tariff, t, ctx),
                      "by_appliance": by_app})
    if ev is not None and soc_at_departure is None:
        soc_at_departure = soc / cap_kwh * 100
    billing = household.get("billing_cycle_kwh")
    return {"steps": steps, "metrics": summarise([(st["t"], st["kw"]) for st in steps], tariff, ev_kw=[st["ev"] for st in steps],
                                                 departure_charge_pct=soc_at_departure, ctx=ctx,
                                                 billing_cycle_kwh=float(billing) if billing is not None else None)}


def summarise(readings: list[tuple[float, float]], tariff: dict[str, Any], *, step_h: float = STEP_H, ev_kw: list[float] | None = None,
              departure_charge_pct: float | None = None, ctx: TM.WindowContext | None = None,
              billing_cycle_kwh: float | None = None) -> dict[str, Any]:
    """Energy, cost (tariff + demand charge), peak and cost by period from (abs_hour, kW) readings.

    Tariffs that use the optional extensions (fixed charges, tiers, taxes, export credits, seasonal/day-type periods) are
    priced by ``_summarise_extended``; original tariffs keep this exact code path (identical costs).
    """
    if TM.is_extended(tariff):
        return _summarise_extended(readings, tariff, step_h=step_h, ev_kw=ev_kw, departure_charge_pct=departure_charge_pct, ctx=ctx,
                                   billing_cycle_kwh=billing_cycle_kwh)
    kwh = sum(kw * step_h for _, kw in readings)
    peak = max((kw for _, kw in readings), default=0.0)
    by_period: dict[str, dict[str, float]] = {}
    energy_cost = 0.0
    for t, kw in readings:
        e = kw * step_h
        c = e * price_at(tariff, t, ctx)
        energy_cost += c
        d = by_period.setdefault(period_label(tariff, t, ctx), {"kwh": 0.0, "cost": 0.0})
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
                        departure_charge_pct: float | None, ctx: TM.WindowContext | None = None,
                        billing_cycle_kwh: float | None = None) -> dict[str, Any]:
    """One simulated day under an extended tariff. Billing-period items (monthly charge, billing-period demand) are prorated.

    ``ctx`` gives each reading its own month and day type (window crossing midnight); ``billing_cycle_kwh`` seeds the tier
    accumulator only for tiered tariffs with ``tier_basis == "billing_period"``.
    """
    kwh = sum(kw * step_h for _, kw in readings)
    peak = max((kw for _, kw in readings), default=0.0)
    by_period: dict[str, dict[str, float]] = {}
    charge = credit = 0.0
    cum = float(billing_cycle_kwh or 0.0) if tariff.get("tiers") and tariff.get("tier_basis") == "billing_period" else 0.0
    for t, kw in readings:
        e = kw * step_h
        month, day_type = _month_day(tariff, t, ctx)
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
