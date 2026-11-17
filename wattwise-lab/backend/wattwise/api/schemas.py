"""Request schemas (field validation). Experiment-level validation is done by the SDK."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from wattwise.adapters.household_model import DEFAULT_APPLIANCES, DEFAULT_EV, FACTORS

METHODS = ("full_factorial", "fractional_factorial", "ofat", "central_composite", "box_behnken", "random")


class Period(BaseModel):
    label: str = Field("", max_length=30)
    start: float = Field(ge=0, le=24)
    end: float = Field(ge=0, le=24)
    price: float = Field(ge=0, le=10)


class TariffIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    demand_charge_per_kw: float = Field(0.0, ge=0, le=100)
    periods: list[Period] = Field(min_length=1, max_length=12)


class EVSettings(BaseModel):
    battery_kwh: float = Field(DEFAULT_EV["battery_kwh"], ge=10, le=200)
    initial_soc_pct: float = Field(DEFAULT_EV["initial_soc_pct"], ge=0, le=100)
    desired_charge_pct: float = Field(DEFAULT_EV["desired_charge_pct"], ge=1, le=100, description="Minimum charge required at departure (constraint)")
    departure_hour: float = Field(DEFAULT_EV["departure_hour"], ge=0, lt=24, description="Hour of day the car leaves, e.g. 7.5 = 07:30")
    efficiency: float = Field(DEFAULT_EV["efficiency"], gt=0.5, le=1.0)

    @model_validator(mode="after")
    def _ok(self):
        if self.initial_soc_pct >= self.desired_charge_pct:
            raise ValueError("initial charge must be below the desired departure charge, otherwise there is nothing to optimise")
        return self


class ApplianceIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    power_kw: float = Field(gt=0, le=10)
    duration_h: float = Field(gt=0, le=8)
    flexible: bool = True
    fixed_start: float = Field(19.0, ge=16, lt=40, description="Absolute hour (16-40) used when the appliance is not flexible")


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
    ev: EVSettings = Field(default_factory=EVSettings)
    appliances: list[ApplianceIn] = Field(default_factory=lambda: [ApplianceIn(**a) for a in DEFAULT_APPLIANCES], max_length=6)
    base_load_kw: float = Field(0.35, ge=0, le=5)
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

    @model_validator(mode="after")
    def _factors_ok(self):
        unknown = set(self.factors) - set(FACTORS)
        if unknown:
            raise ValueError(f"unknown factor(s): {sorted(unknown)}; allowed: {sorted(FACTORS)}")
        for name, s in self.factors.items():
            lim = FACTORS[name]
            if s.enabled and s.low >= s.high:
                raise ValueError(f"{name}: low must be smaller than high")
            if s.enabled and (s.low < lim["low"] or s.high > lim["high"]):
                raise ValueError(f"{name}: range must lie within [{lim['low']}, {lim['high']}]")
        if not any(s.enabled for s in self.factors.values()):
            raise ValueError("enable at least one factor")
        if sum(s.enabled for s in self.factors.values()) < 2 and self.design.method != "ofat":
            raise ValueError("enable at least two factors (or use the one-factor-at-a-time design)")
        return self


class DayRequest(BaseModel):
    tariff_id: str | None = "time-of-use"
    tariff: TariffIn | None = None
    ev: EVSettings = Field(default_factory=EVSettings)
    appliances: list[ApplianceIn] = Field(default_factory=lambda: [ApplianceIn(**a) for a in DEFAULT_APPLIANCES], max_length=6)
    base_load_kw: float = Field(0.35, ge=0, le=5)
    setup: dict[str, float] = Field(default_factory=dict)


class MeterRequest(BaseModel):
    csv: str = Field(min_length=1)
    unit: Literal["kW", "W"] = "kW"
    tariff_id: str | None = "time-of-use"
    tariff: TariffIn | None = None
    filename: str = "meter.csv"


class MeterAttachRequest(MeterRequest):
    departure_charge_pct: float = Field(ge=0, le=100, description="State of charge when the car left (from the vehicle)")


class AnalysisRequest(BaseModel):
    alpha: float = Field(0.05, gt=0, lt=0.5)
    bootstrap: bool = False


class OptimiseRequest(BaseModel):
    count: int = Field(2, ge=1, le=10)
    method: Literal["bayesian", "optuna", "random", "grid"] = "bayesian"
