"""Generic tariff model (backwards compatible with the original time-of-use tariffs).

A tariff is a dict. The original fields are unchanged: ``name``, ``periods`` [{label, start, end, price}] tiling the day,
``demand_charge_per_kw`` (charged on the day's peak). Every extension below is OPTIONAL; a tariff without any of them is
priced by exactly the original code path, so existing tariffs and experiments produce identical costs.

Extensions (generic structure only; real values must come from the user's own tariff/bill):
  periods[i].days                "all" | "weekday" | "weekend"   (default "all")
  periods[i].months              [1..12]                          (default all months; seasonal rates)
  fixed_daily_charge             per day
  fixed_monthly_charge           per billing month (prorated per day as monthly / billing_days)
  tiers                          [{"up_to_kwh": float|None, "adder_per_kwh": float}]  ascending, last up_to_kwh None
  tier_basis                     "daily" | "billing_period"       (cumulative import energy the tier thresholds apply to)
  surcharge_per_kwh              added to every imported kWh
  tax_pct                        applied to the sum of all charges (not to export credits)
  export_credit_per_kwh          credit for exported energy (negative grid power)
  demand_basis                   "daily" (default, original) | "billing_period" (peak over the whole period)
  billing_days                   days per billing period for prorating in single-day estimates (default 30)
  simulation_day                 {"month": 1..12, "day_type": "weekday"|"weekend"} used by the single-day simulator
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

EXTENSION_KEYS = ("fixed_daily_charge", "fixed_monthly_charge", "tiers", "tier_basis", "surcharge_per_kwh", "tax_pct",
                  "export_credit_per_kwh", "demand_basis", "billing_days", "simulation_day")  # fmt: skip
DAY_TYPES = ("weekday", "weekend")


def is_extended(t: dict[str, Any]) -> bool:
    if any(t.get(k) not in (None, 0, 0.0, [], {}, "", "daily") for k in EXTENSION_KEYS):
        return True
    return any(p.get("days", "all") != "all" or p.get("months") for p in t.get("periods") or [])


def date_dependent(t: dict[str, Any]) -> bool:
    return any(p.get("days", "all") != "all" or p.get("months") for p in t.get("periods") or [])


def day_type_of(d: datetime) -> str:
    return "weekend" if d.weekday() >= 5 else "weekday"


def _in_period(p: dict, hod: float) -> bool:
    s, e = float(p["start"]), float(p["end"])
    return (s <= hod < e) if s < e else (hod >= s or hod < e)


def applies(p: dict, month: int | None, day_type: str | None) -> bool:
    days = p.get("days", "all") or "all"
    if days != "all" and day_type is not None and days != day_type:
        return False
    months = p.get("months")
    return not (months and month is not None and month not in months)


def contexts(t: dict[str, Any]) -> list[tuple[int | None, str | None]]:
    """(month, day_type) combinations whose period coverage must be checked."""
    periods = t.get("periods") or []
    months = sorted({m for p in periods for m in (p.get("months") or [])})
    use_days = any((p.get("days", "all") or "all") != "all" for p in periods)
    ms: list[int | None] = list(range(1, 13)) if months else [None]
    ds: list[str | None] = list(DAY_TYPES) if use_days else [None]
    return [(m, d) for m in ms for d in ds]


def period_for(t: dict[str, Any], hod: float, month: int | None = None, day_type: str | None = None) -> dict:
    for p in t["periods"]:
        if _in_period(p, hod) and applies(p, month, day_type):
            return p
    ctx = "" if month is None and day_type is None else f" (month {month}, {day_type})"
    raise ValueError(f"tariff has no period for hour {hod}{ctx}")


def simulation_context(t: dict[str, Any]) -> tuple[int | None, str | None]:
    if not date_dependent(t):
        return None, None
    sd = t.get("simulation_day") or {}
    return int(sd.get("month", 1)), str(sd.get("day_type", "weekday"))


def validate_extensions(t: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for i, p in enumerate(t.get("periods") or [], 1):
        if (p.get("days", "all") or "all") not in ("all", *DAY_TYPES):
            issues.append(f"period {i}: days must be 'all', 'weekday' or 'weekend'")
        ms = p.get("months")
        if ms is not None and (not isinstance(ms, list) or not all(isinstance(m, int) and 1 <= m <= 12 for m in ms)):
            issues.append(f"period {i}: months must be a list of month numbers 1-12")
    for k, hi in (("fixed_daily_charge", 1000), ("fixed_monthly_charge", 10000), ("surcharge_per_kwh", 10), ("export_credit_per_kwh", 10)):
        v = t.get(k)
        if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= hi):
            issues.append(f"{k} must be a number between 0 and {hi}")
    tax = t.get("tax_pct")
    if tax is not None and not (isinstance(tax, (int, float)) and 0 <= tax <= 100):
        issues.append("tax_pct must be between 0 and 100")
    tiers = t.get("tiers")
    if tiers:
        last = 0.0
        for j, tier in enumerate(tiers, 1):
            up = tier.get("up_to_kwh")
            if not isinstance(tier.get("adder_per_kwh", 0), (int, float)) or abs(tier.get("adder_per_kwh", 0)) > 10:
                issues.append(f"tier {j}: adder_per_kwh must be a number between -10 and 10")
            if up is None:
                if j != len(tiers):
                    issues.append(f"tier {j}: only the last tier may have no upper limit")
            elif not isinstance(up, (int, float)) or up <= last:
                issues.append(f"tier {j}: up_to_kwh must increase")
            else:
                last = float(up)
        if tiers[-1].get("up_to_kwh") is not None:
            issues.append("the last tier must have up_to_kwh = null (no upper limit)")
    for k, allowed in (("tier_basis", ("daily", "billing_period")), ("demand_basis", ("daily", "billing_period"))):
        if t.get(k) is not None and t[k] not in allowed:
            issues.append(f"{k} must be one of {list(allowed)}")
    bd = t.get("billing_days")
    if bd is not None and not (isinstance(bd, int) and 1 <= bd <= 366):
        issues.append("billing_days must be an integer between 1 and 366")
    sd = t.get("simulation_day")
    if sd is not None:
        if not isinstance(sd, dict) or not (1 <= int(sd.get("month", 1)) <= 12) or sd.get("day_type", "weekday") not in DAY_TYPES:
            issues.append("simulation_day must be {month: 1-12, day_type: weekday|weekend}")
    return issues


def tier_adder(t: dict[str, Any], cumulative_kwh_before: float) -> float:
    for tier in t.get("tiers") or []:
        up = tier.get("up_to_kwh")
        if up is None or cumulative_kwh_before < float(up):
            return float(tier.get("adder_per_kwh", 0) or 0)
    return 0.0


def _split_by_tiers(t: dict[str, Any], before: float, e: float) -> list[tuple[float, float]]:
    """Split ``e`` kWh starting at cumulative ``before`` into (kwh, adder) chunks along tier boundaries."""
    if not t.get("tiers"):
        return [(e, 0.0)]
    out, pos, left = [], before, e
    for tier in t["tiers"]:
        up = tier.get("up_to_kwh")
        cap = float("inf") if up is None else float(up)
        if pos >= cap:
            continue
        take = min(left, cap - pos)
        if take > 0:
            out.append((take, float(tier.get("adder_per_kwh", 0) or 0)))
            pos += take
            left -= take
        if left <= 1e-12:
            break
    if left > 1e-12:
        out.append((left, 0.0))
    return out


def price_interval(t: dict[str, Any], hod: float, e_kwh: float, cum_import_kwh: float, month: int | None, day_type: str | None) -> dict[str, Any]:
    """Charges for one interval of ``e_kwh`` (negative = export). Returns label, energy charge, export credit, import kWh."""
    p = period_for(t, hod, month, day_type)
    label = p.get("label") or f"{p['start']}-{p['end']}"
    price = float(p["price"])
    if e_kwh >= 0:
        surcharge = float(t.get("surcharge_per_kwh", 0) or 0)
        charge = sum(k * (price + adder + surcharge) for k, adder in _split_by_tiers(t, cum_import_kwh, e_kwh))
        return {"label": label, "charge": charge, "credit": 0.0, "import_kwh": e_kwh, "export_kwh": 0.0}
    if t.get("export_credit_per_kwh") is not None:
        return {"label": label, "charge": 0.0, "credit": -e_kwh * float(t["export_credit_per_kwh"]), "import_kwh": 0.0, "export_kwh": -e_kwh}
    return {"label": label, "charge": e_kwh * price, "credit": 0.0, "import_kwh": 0.0, "export_kwh": -e_kwh}  # original behaviour


def finish(t: dict[str, Any], energy_charge: float, export_credit: float, peak_kw: float, days: float, single_day: bool) -> dict[str, float]:
    """Fixed, demand and tax components for ``days`` days. Single-day estimates prorate billing-period items."""
    billing_days = int(t.get("billing_days") or 30)
    fixed = float(t.get("fixed_daily_charge", 0) or 0) * days + float(t.get("fixed_monthly_charge", 0) or 0) * days / billing_days
    rate = float(t.get("demand_charge_per_kw", 0) or 0)
    # daily basis: rate x the day's peak (multi-day bills pass the sum of daily peaks); billing-period basis: rate x the
    # period peak, prorated to one day for single-day estimates
    prorate = single_day and (t.get("demand_basis") or "daily") == "billing_period"
    demand = rate * peak_kw / billing_days if prorate else rate * peak_kw
    subtotal = energy_charge + fixed + demand
    tax = subtotal * float(t.get("tax_pct", 0) or 0) / 100
    return {"fixed_charge": fixed, "demand_charge": demand, "tax": tax, "export_credit": export_credit,
            "total": subtotal + tax - export_credit}


def bill(t: dict[str, Any], intervals: list[tuple[datetime, float, float]]) -> dict[str, Any]:
    """Cost of a multi-day series of (local start time, kWh in interval, average kW in interval).

    Daily-basis tiers/demand restart each local day; billing-period basis runs over the whole series.
    """
    if not intervals:
        return {"total": 0.0, "energy_charge": 0.0, "fixed_charge": 0.0, "demand_charge": 0.0, "tax": 0.0, "export_credit": 0.0,
                "import_kwh": 0.0, "export_kwh": 0.0, "days": 0, "cost_by_period": {}, "by_day": {}}
    by_period: dict[str, dict[str, float]] = {}
    by_day: dict[str, dict[str, float]] = {}
    energy_charge = credit = imp = exp = 0.0
    cum_day: dict[str, float] = {}
    cum_all = 0.0
    tier_daily = (t.get("tier_basis") or "daily") == "daily"
    daily_demand = (t.get("demand_basis") or "daily") == "daily"
    peak_day: dict[str, float] = {}
    peak_all = 0.0
    for start, kwh, kw in sorted(intervals, key=lambda x: x[0]):
        day = start.date().isoformat()
        hod = start.hour + start.minute / 60 + start.second / 3600
        month, dtype = start.month, day_type_of(start)
        cum = cum_day.get(day, 0.0) if tier_daily else cum_all
        r = price_interval(t, hod, kwh, cum, month, dtype)
        energy_charge += r["charge"]
        credit += r["credit"]
        imp += r["import_kwh"]
        exp += r["export_kwh"]
        cum_day[day] = cum_day.get(day, 0.0) + r["import_kwh"]
        cum_all += r["import_kwh"]
        peak_day[day] = max(peak_day.get(day, 0.0), kw)
        peak_all = max(peak_all, kw)
        bp = by_period.setdefault(r["label"], {"kwh": 0.0, "cost": 0.0})
        bp["kwh"] += kwh
        bp["cost"] += r["charge"] - r["credit"]
        bd = by_day.setdefault(day, {"kwh": 0.0, "energy_charge": 0.0, "peak_kw": 0.0})
        bd["kwh"] += kwh
        bd["energy_charge"] += r["charge"] - r["credit"]
        bd["peak_kw"] = peak_day[day]
    days = len(by_day)
    rate = float(t.get("demand_charge_per_kw", 0) or 0)
    demand_peak_total = sum(peak_day.values()) if daily_demand else peak_all
    parts = finish({**t, "demand_charge_per_kw": rate, "demand_basis": "billing_period"}, energy_charge, credit, demand_peak_total, days, single_day=False)
    return {"total": parts["total"], "energy_charge": energy_charge, "fixed_charge": parts["fixed_charge"], "demand_charge": parts["demand_charge"],
            "tax": parts["tax"], "export_credit": credit, "import_kwh": imp, "export_kwh": exp, "days": days, "peak_kw": peak_all,
            "demand_basis": "daily" if daily_demand else "billing_period", "cost_by_period": by_period, "by_day": by_day}  # fmt: skip


class MissingStartDate(ValueError):
    pass


@dataclass(frozen=True)
class WindowContext:
    """Maps hours since the start of a simulated window (hour 0 = local midnight of ``start_date``) to a month and day type."""

    start_date: date

    def for_hour(self, abs_hour: float) -> tuple[int, str]:
        d = self.start_date + timedelta(days=int(abs_hour // 24))
        return d.month, day_type_of(d)  # type: ignore[arg-type]  # date has weekday() like datetime


def window_context(tariff: dict[str, Any], start_date: date | None) -> WindowContext | None:
    """None for tariffs that are not date dependent; MissingStartDate for date-dependent tariffs without a start date."""
    if not date_dependent(tariff):
        return None
    if start_date is None:
        raise MissingStartDate(f"tariff '{tariff.get('name', '')}' is date dependent and needs the simulated window's start date")
    return WindowContext(start_date)


def reference_date(month: int, day_type: str) -> date:
    """A Tuesday (weekday) or Saturday (weekend) in ``month``; the first one is within days 1-7, so never the month's last day.

    The next calendar day (Wednesday or Sunday) is then in the same month with the same day type.
    """
    target = 5 if day_type == "weekend" else 1
    d = date(2025, int(month), 1)
    return d + timedelta(days=(target - d.weekday()) % 7)


@dataclass(frozen=True)
class Slice:
    start: datetime
    seconds: float
    kwh: float
    label: str
    charge: float
    credit: float


def price_reading(t: dict[str, Any], start_local: datetime, duration_s: float, kwh: float, cum_import_kwh: float = 0.0, *,
                  use_real_dates: bool = False) -> list[Slice]:
    """Split one reading at every tariff period boundary and local midnight inside [start, start+duration) and price each slice.

    Slice energy is ``kwh * slice_seconds / duration_s``. Only the reading's own interval is priced (no gap filling).
    By default the tariff's ``simulation_day`` supplies month and day type (as meter pricing always did); with
    ``use_real_dates`` each slice uses its own local date.
    """
    if not duration_s > 0:
        return []
    tz = start_local.tzinfo
    utc = UTC
    cur = start_local.astimezone(utc) if tz else start_local
    end = cur + timedelta(seconds=duration_s)
    bounds = sorted({0.0, *(float(p[k]) for p in t["periods"] for k in ("start", "end"))})
    sim_ctx = simulation_context(t)
    eps = timedelta(microseconds=1)

    def local(x: datetime) -> datetime:
        return x.astimezone(tz) if tz else x

    def next_boundary(x: datetime) -> datetime:
        mid = local(x).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        best = end
        for off in (0, 1):
            for h in bounds:
                naive = mid + timedelta(days=off, hours=h)
                cand = naive.replace(tzinfo=tz).astimezone(utc) if tz else naive
                if x + eps < cand < best:
                    best = cand
        return best

    out: list[Slice] = []
    cum = cum_import_kwh
    while cur < end:
        nxt = next_boundary(cur)
        secs = (nxt - cur).total_seconds()
        loc = local(cur)
        hod = round(loc.hour + loc.minute / 60 + loc.second / 3600 + loc.microsecond / 3.6e9, 9)
        month, dtype = (loc.month, day_type_of(loc)) if use_real_dates else sim_ctx
        e = kwh * secs / duration_s
        r = price_interval(t, hod, e, cum, month, dtype)
        cum += r["import_kwh"]
        out.append(Slice(loc, secs, e, r["label"], r["charge"], r["credit"]))
        cur = nxt
    return out
