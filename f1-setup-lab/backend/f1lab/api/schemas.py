"""Request schemas. Field-level validation happens here; experiment-level validation is done by the SDK."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from f1lab.adapters.vehicle_model import FACTORS
from f1lab.catalogs.ea_f1_25 import GAME_FACTORS, SANITY_RANGE

METHODS = ("fractional_factorial", "full_factorial", "ofat", "central_composite", "box_behnken", "random")


class FactorSetting(BaseModel):
    enabled: bool = True
    low: float
    high: float


def default_factors() -> dict[str, FactorSetting]:
    spans = {"front_wing": (10, 40), "rear_wing": (10, 40), "suspension": (3, 9), "tyre_pressure_psi": (20, 24)}
    return {k: FactorSetting(low=spans[k][0], high=spans[k][1]) for k in FACTORS}


class DesignSettings(BaseModel):
    method: Literal["fractional_factorial", "full_factorial", "ofat", "central_composite", "box_behnken", "random"] = "fractional_factorial"
    replicates: int = Field(2, ge=1, le=10)
    center_points: int = Field(2, ge=0, le=10)
    n_runs: int = Field(16, ge=3, le=200, description="Only used by the random design")
    run_budget: int = Field(40, ge=4, le=200)
    seed: int = Field(1, ge=0, le=2**31 - 1)


class SimulatorSettings(BaseModel):
    latency_seconds: float = Field(0.4, ge=0, le=5, description="Simulated time per run so progress is visible")
    fault_rate: float = Field(0.0, ge=0, le=1, description="Demo: fraction of runs whose first attempt fails")
    max_retries: int = Field(1, ge=0, le=5)
    stint_laps: int = Field(5, ge=3, le=20)


class LiveFactor(BaseModel):
    values: list[float] = Field(min_length=2, max_length=7, description="Setup values you will actually set in the game (2 to 7)")
    baseline: float


class LiveSettings(BaseModel):
    factors: dict[str, LiveFactor]
    min_valid_laps: int = Field(3, ge=2, le=20, description="Valid timed laps needed for a stint to count")
    max_lap_std: float = Field(1.5, gt=0, le=30, description="Consistency limit: standard deviation of the stint's lap times (seconds)")


class LiveConnectRequest(BaseModel):
    host: str = Field("127.0.0.1", min_length=1, max_length=64)
    port: int = Field(20777, ge=1024, le=65535)


class ExperimentRequest(BaseModel):
    mode: Literal["synthetic", "live_simulator"] = "synthetic"
    live: LiveSettings | None = None
    experiment_id: str | None = Field(
        None,
        pattern=r"^[a-z0-9][a-z0-9-]{2,40}$",
        description="Optional id. Simulated data are seeded from the run ids, so a fixed id reproduces results exactly.",
    )
    name: str = Field(min_length=1, max_length=80)
    hypothesis: str = Field("", max_length=1000)
    factors: dict[str, FactorSetting] = Field(default_factory=default_factors)
    min_stability: float = Field(85.0, ge=0, le=100)
    also_minimise_tyre_wear: bool = False
    design: DesignSettings = Field(default_factory=DesignSettings)
    simulator: SimulatorSettings = Field(default_factory=SimulatorSettings)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name cannot be blank")
        return v.strip()

    @model_validator(mode="after")
    def _live_ok(self):
        if self.mode != "live_simulator":
            if self.live is not None:
                raise ValueError("'live' settings are only valid when mode is live_simulator")
            return self
        if self.live is None:
            raise ValueError("live_simulator experiments need 'live' settings (factors, values, baselines)")
        if self.design.method in ("central_composite", "box_behnken"):
            raise ValueError(f"{self.design.method} needs continuous factors; game setups are discrete steps. Use fractional_factorial, full_factorial, ofat or random")
        if self.design.method == "fractional_factorial" and len(self.live.factors) < 3:
            raise ValueError("fractional_factorial needs at least three factors")
        if len(self.live.factors) < 1:
            raise ValueError("choose at least one factor")
        lo, hi = SANITY_RANGE
        for name, f in self.live.factors.items():
            if name not in GAME_FACTORS:
                raise ValueError(f"unknown game factor '{name}'; allowed: {sorted(GAME_FACTORS)}")
            if len(set(f.values)) != len(f.values):
                raise ValueError(f"{name}: values must be distinct")
            if any(v < lo or v > hi for v in f.values) or not lo <= f.baseline <= hi:
                raise ValueError(f"{name}: values must lie within [{lo:g}, {hi:g}]")
            if f.baseline not in f.values:
                raise ValueError(f"{name}: the baseline must be one of the chosen values")
        return self

    @model_validator(mode="after")
    def _factors_ok(self):
        if self.mode == "live_simulator":
            return self  # synthetic factor limits do not apply
        unknown = set(self.factors) - set(FACTORS)
        if unknown:
            raise ValueError(f"unknown factor(s): {sorted(unknown)}; allowed: {sorted(FACTORS)}")
        for name, s in self.factors.items():
            lim = FACTORS[name]
            if not s.enabled:
                continue
            if s.low >= s.high:
                raise ValueError(f"{name}: low must be smaller than high")
            if s.low < lim["low"] or s.high > lim["high"]:
                raise ValueError(f"{name}: range must lie within the simulator limits [{lim['low']}, {lim['high']}]")
        if sum(1 for s in self.factors.values() if s.enabled) < 2:
            raise ValueError("enable at least two factors")
        return self


class TelemetryRequest(BaseModel):
    csv: str = Field(min_length=1)
    time_unit: Literal["s", "ms"] = "s"
    filename: str = "telemetry.csv"


class AnalysisRequest(BaseModel):
    alpha: float = Field(0.05, gt=0, lt=0.5)
    bootstrap: bool = False


class OptimiseRequest(BaseModel):
    count: int = Field(2, ge=1, le=10)
    method: Literal["bayesian", "optuna", "random", "grid"] = "bayesian"
