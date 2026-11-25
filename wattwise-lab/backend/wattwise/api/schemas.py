"""Request schemas (field validation). Experiment-level validation is done by the SDK."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from wattwise.adapters.household_model import (
    CHARGER_MAX_KW,
    CHARGER_MIN_KW,
    DEFAULT_APPLIANCES,
    DEFAULT_EV,
    FACTORS,
    MAX_FLEXIBLE_APPLIANCES,
    appliance_id,
    factor_limits,
)

METHODS = ("full_factorial", "fractional_factorial", "ofat", "central_composite", "box_behnken", "random")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _decimals_ok(v: float, places: int) -> bool:
    exp = Decimal(repr(v)).as_tuple().exponent
    return isinstance(exp, int) and exp >= -places


def validate_charger_capacity(value: Any) -> float | None:
    """Charger capacity in kW: a number from 1.4 to 350 with at most 2 decimal places (None means not set)."""
    if value is None:
        return None
    invalid = f"charger capacity {value} is invalid; accepted range is {CHARGER_MIN_KW} kW to {CHARGER_MAX_KW:g} kW with at most 2 decimal places"
    if not _is_number(value) or not math.isfinite(value):
        raise ValueError(invalid)
    v = float(value)
    if v < CHARGER_MIN_KW:
        raise ValueError(f"charger capacity {v} kW is below the minimum supported value of {CHARGER_MIN_KW} kW")
    if v > CHARGER_MAX_KW or not _decimals_ok(v, 2):
        raise ValueError(invalid)
    return v


def validate_billing_consumption(value: Any) -> float | None:
    """Consumption already used in the billing cycle (kWh): finite, 0 to 1,000,000, at most 3 decimal places."""
    if value is None:
        return None
    if not _is_number(value) or not math.isfinite(value) or value < 0 or value > 1_000_000 or not _decimals_ok(float(value), 3):
        raise ValueError(f"billing cycle consumption must be a number from 0 to 1,000,000 kWh (got {value})")
    return float(value)


def validate_appliances(apps: list[Any]) -> list[Any]:
    """At most 20 flexible appliances and unique ids (uniqueness is checked on the factor token, '-' read as '_')."""
    flexible = sum(1 for a in apps if a.flexible)
    if flexible > MAX_FLEXIBLE_APPLIANCES:
        raise ValueError(f"at most {MAX_FLEXIBLE_APPLIANCES} flexible appliances are supported (got {flexible})")
    seen: set[str] = set()
    for a in apps:
        aid = appliance_id(a.name, a.id)
        token = aid.replace("-", "_")
        if token in seen:
            raise ValueError(f"duplicate appliance id '{aid}'")
        seen.add(token)
    return apps


def _require_load(ev: Any, apps: list[Any]) -> None:
    if ev is None and not any(a.flexible for a in apps):
        raise ValueError("at least one optimisable load is required: add a flexible appliance or an EV")


class Period(BaseModel):
    label: str = Field("", max_length=30)
    start: float = Field(ge=0, le=24)
    end: float = Field(ge=0, le=24)
    price: float = Field(ge=0, le=10)
    days: Literal["all", "weekday", "weekend"] = "all"
    months: list[int] | None = Field(None, description="Months (1-12) this price applies to; default all (seasonal rates)")


class Tier(BaseModel):
    up_to_kwh: float | None = Field(None, gt=0, description="Upper bound of this tier; null for the last tier")
    adder_per_kwh: float = Field(0.0, ge=-10, le=10, description="Added to the time-of-use price inside this tier")


class SimulationDay(BaseModel):
    month: int = Field(1, ge=1, le=12)
    day_type: Literal["weekday", "weekend"] = "weekday"


class TariffIn(BaseModel):
    """Original fields plus optional generic extensions (see wattwise.adapters.tariff_model). Leave them empty unless
    your own tariff/bill has them; nothing here is a real utility's tariff."""

    name: str = Field(min_length=1, max_length=60)
    demand_charge_per_kw: float = Field(0.0, ge=0, le=100)
    periods: list[Period] = Field(min_length=1, max_length=48)
    fixed_daily_charge: float | None = Field(None, ge=0, le=1000)
    fixed_monthly_charge: float | None = Field(None, ge=0, le=10000)
    tiers: list[Tier] | None = Field(None, max_length=10)
    tier_basis: Literal["daily", "billing_period"] | None = None
    surcharge_per_kwh: float | None = Field(None, ge=0, le=10)
    tax_pct: float | None = Field(None, ge=0, le=100)
    export_credit_per_kwh: float | None = Field(None, ge=0, le=10)
    demand_basis: Literal["daily", "billing_period"] | None = None
    billing_days: int | None = Field(None, ge=1, le=366)
    simulation_day: SimulationDay | None = None


class EVSettings(BaseModel):
    battery_kwh: float = Field(DEFAULT_EV["battery_kwh"], ge=10, le=200)
    initial_soc_pct: float = Field(DEFAULT_EV["initial_soc_pct"], ge=0, le=100)
    desired_charge_pct: float = Field(DEFAULT_EV["desired_charge_pct"], ge=1, le=100, description="Minimum charge required at departure (constraint)")
    departure_hour: float = Field(DEFAULT_EV["departure_hour"], ge=0, lt=24, description="Hour of day the car leaves, e.g. 7.5 = 07:30")
    efficiency: float = Field(DEFAULT_EV["efficiency"], gt=0.5, le=1.0)
    in_baseline: bool = Field(False, description="The baseline meter data already contains this EV's charging")

    @model_validator(mode="after")
    def _ok(self):
        if self.initial_soc_pct >= self.desired_charge_pct:
            raise ValueError("initial charge must be below the desired departure charge, otherwise there is nothing to optimise")
        return self


class ApplianceIn(BaseModel):
    id: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9_-]{0,38}$", description="Stable id; defaults to a slug of the name")
    name: str = Field(min_length=1, max_length=40)
    power_kw: float = Field(gt=0, le=10)
    duration_h: float = Field(gt=0, le=8)
    flexible: bool = True
    fixed_start: float = Field(19.0, ge=16, lt=40, description="Absolute hour (16-40) used when the appliance is not flexible")
    in_baseline: bool = Field(False, description="The baseline meter data already contains this appliance's use")


class HouseholdSettings(BaseModel):
    """The household configuration persisted on the server (``/api/household``). Values are the user's, not measured."""

    tariff_id: str = Field("time-of-use", min_length=1, max_length=60)
    base_load_kw: float = Field(0.35, ge=0, le=5)
    appliances: list[ApplianceIn] = Field(default_factory=lambda: [ApplianceIn(**a) for a in DEFAULT_APPLIANCES], max_length=20)
    ev: EVSettings | None = Field(default_factory=EVSettings, description="null means the household has no EV")
    charger_power_kw: float | None = Field(None, description="Charger capacity (kW), 1.4 to 350 with at most 2 decimals")
    baseline_import_id: str | None = Field(None, max_length=40, description="Historical meter import used as the calibrated baseline")

    @field_validator("charger_power_kw", mode="before")
    @classmethod
    def _charger(cls, v):
        return validate_charger_capacity(v)

    @model_validator(mode="after")
    def _loads(self):
        validate_appliances(self.appliances)
        _require_load(self.ev, self.appliances)
        return self


class HouseholdPut(HouseholdSettings):
    migrated_from: Literal["localStorage"] | None = Field(None, description="Set once by the browser when it uploads legacy settings")


class FactorSetting(BaseModel):
    enabled: bool = True
    low: float
    high: float


def default_factors() -> dict[str, FactorSetting]:
    return {
        "charge_start": FactorSetting(low=19.0, high=29.0),
        "charge_power_kw": FactorSetting(low=3.7, high=11.0),
        "appliance_start": FactorSetting(low=18.0, high=26.0),
    }


class DesignSettings(BaseModel):
    method: Literal["full_factorial", "fractional_factorial", "ofat", "central_composite", "box_behnken", "random"] = "full_factorial"
    levels: int = Field(3, ge=2, le=5, description="Levels per factor (full factorial / OFAT)")
    replicates: int = Field(1, ge=1, le=10)
    center_points: int = Field(2, ge=0, le=10)
    n_runs: int = Field(16, ge=3, le=200, description="Only used by the random design")
    run_budget: int = Field(60, ge=4, le=300)
    seed: int = Field(1, ge=0, le=2**31 - 1)


class SimulatorSettings(BaseModel):
    latency_seconds: float = Field(0.3, ge=0, le=5)
    fault_rate: float = Field(0.0, ge=0, le=1)
    max_retries: int = Field(1, ge=0, le=5)


class ExperimentRequest(BaseModel):
    experiment_id: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9-]{2,40}$", description="Optional; a fixed id reproduces simulated data exactly")
    name: str = Field(min_length=1, max_length=80)
    hypothesis: str = Field("", max_length=1000)
    tariff_id: str | None = "time-of-use"
    tariff: TariffIn | None = Field(None, description="Inline tariff (overrides tariff_id)")
    ev: EVSettings | None = Field(default_factory=EVSettings, description="null means the household has no EV")
    appliances: list[ApplianceIn] = Field(default_factory=lambda: [ApplianceIn(**a) for a in DEFAULT_APPLIANCES], max_length=20)
    appliance_start_mode: Literal["shared", "per_appliance"] = "shared"
    base_load_kw: float = Field(0.35, ge=0, le=5)
    charger_capacity_kw: float | None = Field(None, description="Charger capacity (kW), 1.4 to 350 with at most 2 decimals")
    baseline_import_id: str | None = Field(None, max_length=40, description="Start from the background load measured in this historical meter import "
                                                                          "(otherwise the synthetic profile is used)")
    baseline_excludes_modelled_appliances: bool = Field(False, description="Confirms the baseline does not contain the modelled loads (skips the overlap check)")
    day_type: Literal["weekday", "weekend"] | None = Field(None, description="Baseline profile day type; null uses the all-days profile")
    window_start_date: date | None = Field(None, description="Local date on which the simulated window starts (ISO)")
    billing_cycle_consumption_kwh: float | None = Field(None, description="kWh already used in the billing cycle (slab tariffs)")
    factors: dict[str, FactorSetting] = Field(default_factory=default_factors)
    also_minimise_peak: bool = False
    design: DesignSettings = Field(default_factory=DesignSettings)
    simulator: SimulatorSettings = Field(default_factory=SimulatorSettings)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name cannot be blank")
        return v.strip()

    @field_validator("charger_capacity_kw", mode="before")
    @classmethod
    def _charger(cls, v):
        return validate_charger_capacity(v)

    @field_validator("billing_cycle_consumption_kwh", mode="before")
    @classmethod
    def _billing(cls, v):
        return validate_billing_consumption(v)

    @model_validator(mode="after")
    def _factors_ok(self):
        validate_appliances(self.appliances)
        _require_load(self.ev, self.appliances)
        has_ev = self.ev is not None
        catalog = factor_limits(self.charger_capacity_kw, [a.model_dump() for a in self.appliances], has_ev)
        ev_only = set(FACTORS) - set(factor_limits(has_ev=False))
        if self.charger_capacity_kw is not None and "factors" not in self.model_fields_set and "charge_power_kw" in self.factors:
            self.factors["charge_power_kw"] = FactorSetting(low=CHARGER_MIN_KW, high=self.charger_capacity_kw)
        # EV factors in a no-EV request are skipped (the default factor dict contains them), not rejected
        active = {n: s for n, s in self.factors.items() if has_ev or n not in ev_only}
        unknown = set(active) - set(catalog)
        if unknown:
            raise ValueError(f"unknown factor(s): {sorted(unknown)}; allowed: {sorted(catalog)}")
        for name, s in active.items():
            lim = catalog[name]
            if s.enabled and s.low >= s.high:
                raise ValueError(f"{name}: low must be smaller than high")
            if s.enabled and (s.low < lim["low"] or s.high > lim["high"]):
                raise ValueError(f"{name}: range must lie within [{lim['low']}, {lim['high']}]")
        enabled = sum(s.enabled for s in active.values())
        if not enabled:
            raise ValueError("enable at least one factor")
        if has_ev and enabled < 2 and self.design.method != "ofat":
            raise ValueError("enable at least two factors (or use the one-factor-at-a-time design)")
        return self


class DayRequest(BaseModel):
    tariff_id: str | None = "time-of-use"
    tariff: TariffIn | None = None
    ev: EVSettings | None = Field(default_factory=EVSettings, description="null means the household has no EV")
    appliances: list[ApplianceIn] = Field(default_factory=lambda: [ApplianceIn(**a) for a in DEFAULT_APPLIANCES], max_length=20)
    base_load_kw: float = Field(0.35, ge=0, le=5)
    charger_capacity_kw: float | None = Field(None, description="Charger capacity (kW), 1.4 to 350 with at most 2 decimals")
    baseline_import_id: str | None = Field(None, max_length=40, description="Use the profile measured in this meter import as background load")
    day_type: Literal["weekday", "weekend"] | None = Field(None, description="Baseline profile day type; null uses the all-days profile")
    window_start_date: date | None = Field(None, description="Local date on which the simulated window starts (ISO)")
    billing_cycle_consumption_kwh: float | None = Field(None, description="kWh already used in the billing cycle (slab tariffs)")
    setup: dict[str, float] = Field(default_factory=dict)

    @field_validator("charger_capacity_kw", mode="before")
    @classmethod
    def _charger(cls, v):
        return validate_charger_capacity(v)

    @field_validator("billing_cycle_consumption_kwh", mode="before")
    @classmethod
    def _billing(cls, v):
        return validate_billing_consumption(v)

    @model_validator(mode="after")
    def _loads(self):
        validate_appliances(self.appliances)
        _require_load(self.ev, self.appliances)
        return self


class MeterRequest(BaseModel):
    csv: str = Field(min_length=1)
    unit: Literal["kW", "W"] = "kW"
    tariff_id: str | None = "time-of-use"
    tariff: TariffIn | None = None
    filename: str = "meter.csv"
    options: dict | None = Field(None, description="Optional: read the file with the historical-library interpreter (same keys as "
                                                   "/api/meter/import options: columns, timezone, reading_kind, unit, ...). "
                                                   "Omit for the simple timestamp,power_kw|power_w format.")


class MeterImportRequest(BaseModel):
    """A historical meter file for the library. ``options`` maps to wattwise.adapters.meter_import.ImportOptions."""

    csv: str = Field(min_length=1)
    file_name: str = Field("meter.csv", min_length=1, max_length=300)
    source: str = Field("csv upload", max_length=120, description="Where the file came from, e.g. the supplier's portal")
    provenance: str | None = Field(None, description="real_meter, synthetic_demo or unknown; checked by the meter library (blank means unknown)")
    options: dict = Field(default_factory=dict)
    force: bool = Field(False, description="import again even if the same file was imported with the same options")


class MeterAttachRequest(MeterRequest):
    departure_charge_pct: float = Field(ge=0, le=100, description="State of charge when the car left (from the vehicle)")


class AnalysisRequest(BaseModel):
    alpha: float = Field(0.05, gt=0, lt=0.5)
    bootstrap: bool = False


class OptimiseRequest(BaseModel):
    count: int = Field(2, ge=1, le=10)
    method: Literal["bayesian", "optuna", "random", "grid"] = "bayesian"
