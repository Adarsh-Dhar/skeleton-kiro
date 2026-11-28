"""Request schemas. Field-level validation happens here; experiment-level validation is done by the SDK."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

from f1lab.adapters.vehicle_model import FACTORS

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


class ExperimentRequest(BaseModel):
    mode: Literal["synthetic"] = "synthetic"  # the only experiment mode; any other value is rejected
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
    def _factors_ok(self):
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


class SetupLinkRequest(BaseModel):
    """Selection and Setup_Configuration of a Setup_Link submission (validated by LabService.setup_link, which names every problem)."""
    driver: str | None = None
    laps: list[int] | None = None
    metrics: list[str] | None = None
    experiment_id: str | None = None
    new_experiment: dict | None = None
    factors: dict | None = None


class AnalysisRequest(BaseModel):
    alpha: float = Field(0.05, gt=0, lt=0.5)
    bootstrap: bool = False


class OptimiseRequest(BaseModel):
    count: int = Field(2, ge=1, le=10)
    method: Literal["bayesian", "optuna", "random", "grid"] = "bayesian"


# ---------------------------------------------------------------------------------------------------------------------
# CSV Upload (Historical provider "csv_upload"): metadata, Column_Mapping, preview, validate and session-exists schemas.
# Shape checks only; the rules that need the file (1:1 mapping, columns present, the 11 metadata items) are enforced by
# integrations/historical/csv_importer.py and reported by the validate endpoint.
# ---------------------------------------------------------------------------------------------------------------------
CsvDataType = Literal["lap_summary", "sampled_telemetry"]
DuplicatePolicy = Literal["drop_exact_duplicates", "reject"]
OrderPolicy = Literal["sort", "reject"]
FILE_HASH_PATTERN = r"^[0-9a-fA-F]{64}$"


class ColumnMapping(RootModel[dict[str, str]]):
    """Column_Mapping: ``{standard_field: csv_column}``. Blank names are rejected; 1:1 and existence are checked against the file."""

    @model_validator(mode="after")
    def _names_not_blank(self):
        blank = [k for k, v in self.root.items() if not k.strip() or not v.strip()]
        if blank:
            raise ValueError("mapping must not contain blank field or column names")
        return self


class CsvUnits(BaseModel):
    """Units of the file's time, speed and distance columns; omitted means seconds, kph and metres."""

    time: str | None = Field(None, description="s | ms (or seconds | milliseconds)")
    speed: str | None = Field(None, description="kph | mph | m/s")
    distance: str | None = Field(None, description="m | km | ft")

    @model_validator(mode="after")
    def _supported(self):
        from f1lab.integrations.historical import csv_normalizer as norm

        try:
            norm.normalize_units({k: v for k, v in self.model_dump().items() if v is not None})
        except norm.UnsupportedUnitError as exc:
            raise ValueError(str(exc)) from exc
        return self


class CsvSessionMetadata(BaseModel):
    """Session_Metadata supplied by the user. An item that is not supplied must be named in ``unavailable``."""

    year: int | None = None
    event: str | None = Field(None, max_length=400)
    circuit: str | None = Field(None, max_length=400)
    session_type: str | None = Field(None, max_length=400)
    driver: str | None = Field(None, max_length=400)
    session_date: str | None = Field(None, max_length=40, description="ISO 8601 YYYY-MM-DD")
    timezone: str | None = Field(None, max_length=80, description="IANA timezone name")
    original_filename: str | None = Field(None, max_length=1000)
    time_unit: str | None = None
    distance_unit: str | None = None
    unavailable: list[str] = Field(default_factory=list, max_length=20, description="items explicitly marked unavailable")


class _CsvFileRef(BaseModel):
    file_hash: str = Field(pattern=FILE_HASH_PATTERN, description="SHA-256 returned by the preview endpoint")

    @field_validator("file_hash")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class CsvImportOptions(_CsvFileRef):
    """The ``csv`` sub-object of a ``provider: "csv_upload"`` import request."""

    mapping: ColumnMapping | None = Field(None, description="omitted: the suggested mapping is used")
    units: CsvUnits = Field(default_factory=CsvUnits)
    metadata: CsvSessionMetadata = Field(default_factory=CsvSessionMetadata)
    duplicate_policy: DuplicatePolicy = "reject"
    order_policy: OrderPolicy = "reject"
    data_type: CsvDataType | None = Field(None, description="required when the file could be either type")
    replace_existing: bool = Field(False, description="replace a stored session with the same provider, event, session type, driver and year")
    tz_offset: str | None = Field(None, max_length=10, description="UTC offset for timestamps that carry none, e.g. +02:00")


class CsvValidateRequest(_CsvFileRef):
    """Validate an uploaded file against a mapping, units, policies and metadata without importing it."""

    mapping: ColumnMapping | None = None
    units: CsvUnits = Field(default_factory=CsvUnits)
    metadata: CsvSessionMetadata = Field(default_factory=CsvSessionMetadata)
    duplicate_policy: DuplicatePolicy = "reject"
    order_policy: OrderPolicy = "reject"
    data_type: CsvDataType | None = None
    tz_offset: str | None = Field(None, max_length=10)


class CsvPreviewResponse(BaseModel):
    file_hash: str
    original_filename: str
    size_bytes: int
    columns: list[str]
    column_count: int
    row_count: int
    sample_rows: list[list[str]]
    suggested_mapping: dict[str, str]
    detected_type: Literal["lap_summary", "sampled_telemetry", "ambiguous", "unrecognized"]
    data_type_choices: list[CsvDataType]
    validation_report: dict


class CsvSessionExistsQuery(BaseModel):
    """Query of ``GET /api/historical/csv/session-exists``: a file hash, a Session_Metadata key, or both."""

    file_hash: str | None = Field(None, pattern=FILE_HASH_PATTERN)
    provider: Literal["csv_upload"] = "csv_upload"
    event: str | None = Field(None, min_length=1, max_length=400)
    session_type: str | None = Field(None, min_length=1, max_length=400)
    driver: str | None = Field(None, min_length=1, max_length=400)
    year: int | None = Field(None, ge=1950, le=2100)

    @model_validator(mode="after")
    def _key_complete(self):
        key = {"event": self.event, "session_type": self.session_type, "driver": self.driver, "year": self.year}
        given = [k for k, v in key.items() if v is not None]
        if given and len(given) < len(key):
            raise ValueError("a session key needs event, session_type, driver and year; missing: " + ", ".join(k for k in key if k not in given))
        if self.file_hash is None and not given:
            raise ValueError("give a file_hash, a session key (event, session_type, driver, year), or both")
        return self


class CsvSessionExistsResponse(BaseModel):
    exists: bool
    session_id: str | None = None
    matched_by: Literal["file_hash", "session_key"] | None = None
