"""Household baseline from historical meter readings (Task K4). All values here are labelled ``historical_measured``
except the cost, which is the user's readings priced with the selected tariff (``historical_measured_priced``).

A local day counts as complete when readings cover at least ``COMPLETE_DAY`` of it; profiles and variability use
complete days only, so partial first/last days and gaps do not distort them.
"""
from __future__ import annotations

import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from wattwise.adapters.household_model import STEPS, WINDOW_START
from wattwise.adapters.meter_import import Reading
from wattwise.adapters.tariff_model import bill

COMPLETE_DAY = 0.95
SLOT_MIN = 15  # profile resolution (the simulator's step)


def _day_minutes(d: date, tz: ZoneInfo) -> float:
    a = datetime(d.year, d.month, d.day, tzinfo=tz)
    b = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return (b.astimezone(UTC) - a.astimezone(UTC)).total_seconds() / 60


def _split_local(readings: list[Reading], tz: ZoneInfo):
    """Yield (local start, kwh, kw, minutes) pieces split at local 15-minute slot boundaries (energy shared pro rata)."""
    for r in readings:
        start = r.start_utc.astimezone(tz)
        left = r.interval_minutes
        cur = start
        while left > 1e-9:
            slot_end_min = (cur.minute // SLOT_MIN + 1) * SLOT_MIN
            boundary = cur.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=slot_end_min)
            take = min(left, (boundary - cur).total_seconds() / 60)
            if take <= 1e-9:
                take = min(left, SLOT_MIN)
            share = take / r.interval_minutes
            yield cur, r.kwh * share, r.kw, take
            cur = (cur.astimezone(UTC) + timedelta(minutes=take)).astimezone(tz)
            left -= take


def analyse(readings: list[Reading], timezone: str, tariff: dict[str, Any] | None = None) -> dict[str, Any]:
    tz = ZoneInfo(timezone)
    if not readings:
        return {"provenance": "historical_measured", "days": [], "coverage": {"days_total": 0, "days_complete": 0}, "warnings": ["no readings"]}
    day_kwh: dict[date, float] = {}
    day_min: dict[date, float] = {}
    day_peak: dict[date, float] = {}
    slot: dict[date, dict[int, list[float]]] = {}
    for local, kwh, kw, minutes in _split_local(readings, tz):
        d = local.date()
        day_kwh[d] = day_kwh.get(d, 0.0) + kwh
        day_min[d] = day_min.get(d, 0.0) + minutes
        day_peak[d] = max(day_peak.get(d, 0.0), kw)
        k = (local.hour * 60 + local.minute) // SLOT_MIN
        e = slot.setdefault(d, {}).setdefault(k, [0.0, 0.0])
        e[0] += kwh
        e[1] += minutes
    days = sorted(day_kwh)
    cov = {d: min(1.0, day_min[d] / _day_minutes(d, tz)) for d in days}
    complete = [d for d in days if cov[d] >= COMPLETE_DAY]
    warnings: list[str] = []
    if len(complete) < 7:
        warnings.append(f"only {len(complete)} complete day(s): at least 7 (ideally 30) are needed for a reliable baseline")
    daily = [day_kwh[d] for d in complete]

    def profile(ds: list[date]) -> list[float | None]:
        out: list[float | None] = []
        for k in range(24 * 60 // SLOT_MIN):
            e = sum(slot[d].get(k, [0, 0])[0] for d in ds)
            m = sum(slot[d].get(k, [0, 0])[1] for d in ds)
            out.append(round(e / (m / 60), 4) if m > 0 else None)
        return out

    def hourly(p: list[float | None]) -> list[float | None]:
        per = 60 // SLOT_MIN
        out = []
        for h in range(24):
            vals = [v for v in p[h * per:(h + 1) * per] if v is not None]
            out.append(round(sum(vals) / len(vals), 4) if vals else None)
        return out

    weekday = [d for d in complete if d.weekday() < 5]
    weekend = [d for d in complete if d.weekday() >= 5]
    p_all, p_wd, p_we = profile(complete), profile(weekday), profile(weekend)
    peak_r = max(readings, key=lambda r: r.kw)
    interval = statistics.median([r.interval_minutes for r in readings])
    out: dict[str, Any] = {
        "provenance": "historical_measured", "timezone": timezone,
        "period": {"first_local": readings[0].start_utc.astimezone(tz).isoformat(), "last_local": readings[-1].start_utc.astimezone(tz).isoformat()},
        "coverage": {"days_total": len(days), "days_complete": len(complete), "days_partial": len(days) - len(complete),
                     "complete_day_threshold": COMPLETE_DAY, "mean_coverage": round(sum(cov.values()) / len(cov), 4), "interval_minutes": interval},
        "daily_energy_kwh": {"mean": round(statistics.mean(daily), 3) if daily else None, "median": round(statistics.median(daily), 3) if daily else None,
                             "std": round(statistics.stdev(daily), 3) if len(daily) > 1 else None, "min": round(min(daily), 3) if daily else None,
                             "max": round(max(daily), 3) if daily else None,
                             "cv": round(statistics.stdev(daily) / statistics.mean(daily), 4) if len(daily) > 1 and statistics.mean(daily) > 0 else None},
        "peak_demand": {"kw": round(peak_r.kw, 3), "at_utc": peak_r.start_utc.isoformat(), "averaged_over_minutes": peak_r.interval_minutes,
                        "note": f"peak of {peak_r.interval_minutes:g}-minute average power; instantaneous peaks are higher"},
        "hourly_profile_kw": hourly(p_all), "weekday_hourly_kw": hourly(p_wd) if weekday else None, "weekend_hourly_kw": hourly(p_we) if weekend else None,
        "weekday_days": len(weekday), "weekend_days": len(weekend),
        "days": [{"date": d.isoformat(), "kwh": round(day_kwh[d], 3), "peak_kw": round(day_peak[d], 3), "coverage": round(cov[d], 4),
                  "complete": cov[d] >= COMPLETE_DAY, "weekend": d.weekday() >= 5} for d in days],
        "warnings": warnings,
    }  # fmt: skip
    out["profile_15min_kw"] = p_all
    if tariff is not None:
        intervals = [(local, kwh, kw) for local, kwh, kw, _m in _split_local(readings, tz)]
        b = bill(tariff, intervals)
        complete_set = {d.isoformat() for d in complete}
        bc = bill(tariff, [x for x in intervals if x[0].date().isoformat() in complete_set]) if complete_set else None
        out["complete_days_cost"] = None if bc is None else {
            "provenance": "historical_measured_priced", "days": bc["days"], "total": round(bc["total"], 4),
            "mean_daily": round(bc["total"] / bc["days"], 4),
            "cost_by_period_per_day": {k: round(v["cost"] / bc["days"], 4) for k, v in bc["cost_by_period"].items()},
        }  # fmt: skip
        per_day = [v["energy_charge"] for k, v in b["by_day"].items() if k in complete_set]
        out["baseline_cost"] = {
            "provenance": "historical_measured_priced", "tariff_id": tariff.get("id"), "tariff_name": tariff.get("name"),
            "total": round(b["total"], 4), "energy_charge": round(b["energy_charge"], 4), "fixed_charge": round(b["fixed_charge"], 4),
            "demand_charge": round(b["demand_charge"], 4), "tax": round(b["tax"], 4), "export_credit": round(b["export_credit"], 4),
            "days": b["days"], "mean_daily_energy_charge_complete_days": round(statistics.mean(per_day), 4) if per_day else None,
            "cost_by_period": {k: {"kwh": round(v["kwh"], 3), "cost": round(v["cost"], 4)} for k, v in b["cost_by_period"].items()},
            "note": "your readings priced with the selected tariff; compare with your bill for the same dates before trusting it",
        }  # fmt: skip
    return out


def window_profile(profile_15min: list[float | None]) -> list[float] | None:
    """Local-day 15-minute profile (00:00 first) -> the simulator's 96-step window starting at 16:00. None if gaps."""
    if len(profile_15min) != STEPS or any(v is None for v in profile_15min):
        return None
    start = int(WINDOW_START * 60 // SLOT_MIN)
    return [float(v) for v in profile_15min[start:] + profile_15min[:start]]


def calibration(analysis: dict[str, Any], day_type: str = "all", ev_charger_kw: float | None = None) -> dict[str, Any]:
    """The 96-step background-load profile an experiment starts from, plus the checks a user must know about."""
    if analysis["coverage"]["days_complete"] == 0:
        raise ValueError("the import has no complete day; a baseline needs at least one fully covered day")
    p = analysis["profile_15min_kw"]
    prof = window_profile(p)
    if prof is None:
        raise ValueError("the measured profile has empty 15-minute slots; import more complete days")
    notes = ["the imported usage is used as the household's non-shiftable background load",
             "if the import period already contains EV charging or the listed flexible appliances, they are counted twice: "
             "remove them from the appliance list or import a period without them"]
    if ev_charger_kw and max(prof) >= 0.8 * ev_charger_kw:
        notes.append(f"the measured profile reaches {max(prof):.1f} kW, close to the charger power: it probably includes EV charging")
    return {"base_profile_kw": prof, "provenance": "historical_measured", "day_type": day_type,
            "days_used": analysis["coverage"]["days_complete"], "mean_kw": round(sum(prof) / len(prof), 4), "notes": notes}
