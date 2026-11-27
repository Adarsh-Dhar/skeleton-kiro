"""Data-source records for every WATTWISE input (zero fabricated input data).

Every input value comes from one of two places: a verified real-world source (tariff order, manufacturer specification, research
dataset) or the household owner (their bill, meter export, appliance labels, vehicle documents). Each input record carries a
``source``; this module holds the policy that decides what a source may claim. Pure Python, no I/O.

Rules
  * ``verified`` needs an official/manufacturer/research source, a document (URL or reference) and the date it was collected.
  * the owner's own data (``user_provided``, ``meter_export``) is never ``verified``: it is ``user_supplied`` (or ``unverified``).
  * ``unverified`` sources stay usable but are labelled as such everywhere they appear.
  * ``example`` marks the values the app used to ship (not a real tariff or household). They are never used for a calculation.
  * a source whose ``effective_to`` date has passed is reported as expired.
"""
from __future__ import annotations

from datetime import date
from typing import Any

SOURCE_TYPES = ("official", "manufacturer", "research_dataset", "meter_export", "user_provided", "unverified", "example")
VERIFICATION_LEVELS = ("verified", "user_supplied", "unverified")
VERIFIABLE_TYPES = ("official", "manufacturer", "research_dataset")
OWNER_TYPES = ("user_provided", "meter_export")
SOURCE_TYPE_LABELS = {
    "official": "Official publication (regulator, utility tariff order)",
    "manufacturer": "Manufacturer specification",
    "research_dataset": "Documented research dataset",
    "meter_export": "Meter export (from your utility portal or meter)",
    "user_provided": "Entered by the household owner",
    "unverified": "Unverified source",
    "example": "Built-in example (not real data)",
}
VERIFICATION_LABELS = {"verified": "VERIFIED", "user_supplied": "USER SUPPLIED", "unverified": "UNVERIFIED"}
# Unit of every configurable input (the stored value is always in this unit).
INPUT_UNITS = {
    "tariff.periods.price": "currency/kWh", "tariff.demand_charge_per_kw": "currency/kW", "tariff.fixed_daily_charge": "currency/day",
    "tariff.fixed_monthly_charge": "currency/month", "tariff.surcharge_per_kwh": "currency/kWh", "tariff.tax_pct": "%",
    "tariff.export_credit_per_kwh": "currency/kWh", "tariff.tiers.up_to_kwh": "kWh", "tariff.tiers.adder_per_kwh": "currency/kWh",
    "ev.battery_kwh": "kWh", "ev.efficiency": "fraction (0.5-1)", "ev.initial_soc_pct": "%", "ev.desired_charge_pct": "%",
    "ev.departure_hour": "hour of day", "ev.plug_in_hour": "hour of day", "charger_power_kw": "kW",
    "appliances.power_kw": "kW", "appliances.duration_h": "h", "appliances.fixed_start": "hour (16-40 window)",
    "base_load_kw": "kW", "meter.readings": "kW / kWh as in the file",
}  # fmt: skip
class ExampleDataError(ValueError):
    """An input is a built-in example, not real data, so it cannot be used for a calculation."""
def source_issues(src: dict[str, Any]) -> list[str]:
    """Problems with a source record (empty list = acceptable). Used by the request schemas."""
    issues: list[str] = []
    st, ver = src.get("source_type"), src.get("verification")
    if st not in SOURCE_TYPES:
        issues.append(f"source_type must be one of {', '.join(SOURCE_TYPES)}")
    if ver not in VERIFICATION_LEVELS:
        issues.append(f"verification must be one of {', '.join(VERIFICATION_LEVELS)}")
    if not str(src.get("source_name") or "").strip():
        issues.append("source_name is required (who published the value, or 'household owner')")
    if ver == "verified":
        if st not in VERIFIABLE_TYPES:
            issues.append(f"only {', '.join(VERIFIABLE_TYPES)} sources can be 'verified'; your own data is 'user_supplied'")
        if not str(src.get("document") or "").strip():
            issues.append("a verified source needs the document or URL it came from")
        if not src.get("collected_on"):
            issues.append("a verified source needs the date it was collected (collected_on)")
    if st in ("unverified", "example") and ver != "unverified":
        issues.append(f"a source of type '{st}' can only have verification 'unverified'")
    if st == "example":
        issues.append("built-in example values are not accepted as input data")
    a, b = _d(src.get("effective_from")), _d(src.get("effective_to"))
    if a and b and b < a:
        issues.append("effective_to is before effective_from")
    c = _d(src.get("collected_on"))
    if c and c > date.today():
        issues.append("collected_on cannot be in the future")
    return issues
def _d(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None
def status(src: dict[str, Any] | None, today: date | None = None) -> dict[str, Any]:
    """How a source is shown: verification label, expiry and the reasons a value is not independently verified."""
    today = today or date.today()
    if not src:
        return {"verification": "unverified", "label": "NO SOURCE", "expired": False, "usable": False,
                "warnings": ["no source recorded for this input"]}
    ver = src.get("verification") if src.get("verification") in VERIFICATION_LEVELS else "unverified"
    warnings: list[str] = []
    end = _d(src.get("effective_to"))
    expired = bool(end and end < today)
    if expired:
        warnings.append(f"expired: effective until {end.isoformat()}; check for a newer tariff order or document")
    start = _d(src.get("effective_from"))
    if start and start > today:
        warnings.append(f"not yet in effect: applies from {start.isoformat()}")
    if src.get("source_type") == "example":
        warnings.append("built-in example, not real data: replace it with your own values")
    elif ver == "unverified":
        warnings.append("unverified: no traceable source, date or document")
    elif ver == "user_supplied":
        warnings.append("supplied by the household owner; not independently verified")
    if src.get("legacy") and src.get("source_type") != "example":
        warnings.append("recorded before source tracking: confirm the value and add its source")
    label = VERIFICATION_LABELS[ver] + (" · EXPIRED" if expired else "")
    if src.get("source_type") == "example":
        label = "EXAMPLE · NOT REAL"
    return {"verification": ver, "label": label, "expired": expired, "usable": src.get("source_type") != "example", "warnings": warnings}
def legacy_source(what: str) -> dict[str, Any]:
    """Source for a value the owner saved before source tracking existed: theirs, but to be confirmed."""
    return {"source_type": "user_provided", "source_name": "household owner", "document": None, "collected_on": None,
            "effective_from": None, "effective_to": None, "verification": "user_supplied", "legacy": True,
            "notes": f"{what} saved before source tracking; confirm it against your bill, appliance label or vehicle documents"}
def example_source(what: str) -> dict[str, Any]:
    return {"source_type": "example", "source_name": "WATTWISE built-in example", "document": None, "collected_on": None,
            "effective_from": None, "effective_to": None, "verification": "unverified", "legacy": True,
            "notes": f"{what} shipped with earlier versions as an illustration; it is not any utility's or household's real data"}
def require_usable(src: dict[str, Any] | None, what: str) -> None:
    if src and src.get("source_type") == "example":
        raise ExampleDataError(f"{what} is a built-in example, not real data; enter the values from your bill or documents instead")
def entry(path: str, src: dict[str, Any] | None, values: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """One row of an input-source snapshot: the values, their units, the source and its status."""
    return {"input": path, "values": values, "units": {k: INPUT_UNITS.get(f"{path.split('[')[0]}.{k}", INPUT_UNITS.get(k, "")) for k in values},
            "source": src, "status": status(src, today)}
