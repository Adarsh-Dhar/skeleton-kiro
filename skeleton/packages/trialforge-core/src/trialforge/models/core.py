"""Domain-independent validated experiment and result models.

Compatibility policy: every name and field exported by 0.1.0 is retained. New
fields are optional with defaults. ``design_method`` values and statuses were
only extended, never renamed.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trialforge.errors import ValidationError  # noqa: F401  (re-exported for convenience)
from trialforge.utilities.safe_expr import expression_names, parse_expression

MODEL_SCHEMA_VERSION = "1.0"

DESIGN_METHODS = frozenset(
    {
        "factorial",
        "ofat",
        "random",
        "fractional_factorial",
        "blocked_factorial",
        "central_composite",
        "box_behnken",
    }
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class TFModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ----------------------------------------------------------------- factors
class Factor(TFModel):
    """An experimental variable.

    ``discrete`` and ``categorical`` factors list explicit ``values``; ``continuous``
    factors have inclusive ``lower``/``upper`` bounds. ``role='block'`` marks a
    nuisance/blocking variable that is distinct from treatment factors.
    """

    name: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    factor_type: Literal["discrete", "continuous", "categorical"]
    values: list[Any] | None = None
    lower: float | None = None
    upper: float | None = None
    unit: str | None = None
    default: Any | None = None
    role: Literal["treatment", "block"] = "treatment"
    validation_rule: Callable[[Any], bool] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_domain(self):
        if self.validation_rule is not None and not callable(self.validation_rule):
            raise ValueError("validation_rule must be callable")
        if self.factor_type == "continuous" and self.values is not None:
            raise ValueError("continuous factors use bounds, not a values list")
        if self.factor_type in ("discrete", "categorical"):
            if self.lower is not None or self.upper is not None:
                raise ValueError(f"{self.factor_type} factors use values, not numerical bounds")
            if not self.values:
                raise ValueError(f"{self.factor_type} factor '{self.name}' requires values")
            if len(set(map(repr, self.values))) != len(self.values):
                raise ValueError(f"factor '{self.name}' has duplicate values")
        if self.factor_type == "discrete":
            for v in self.values:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise ValueError(f"discrete factor '{self.name}' values must be finite numbers")
        if self.factor_type == "continuous":
            if self.lower is None or self.upper is None or self.lower >= self.upper:
                raise ValueError(f"continuous factor '{self.name}' requires lower < upper")
            if not (math.isfinite(self.lower) and math.isfinite(self.upper)):
                raise ValueError(f"continuous factor '{self.name}' bounds must be finite")
        if self.factor_type == "categorical":
            if len({type(value) for value in self.values}) > 1:
                raise ValueError(f"categorical factor '{self.name}' values must have a consistent type")
        if self.default is not None and not self.accepts(self.default):
            raise ValueError(f"default for factor '{self.name}' is outside its domain")
        return self

    @property
    def is_numeric(self) -> bool:
        return self.factor_type in ("continuous", "discrete")

    def accepts(self, value: Any) -> bool:
        if self.factor_type in ("discrete", "categorical"):
            valid = value in (self.values or [])
        else:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and self.lower <= value <= self.upper
        return bool(valid and (self.validation_rule is None or self.validation_rule(value)))

    def candidates(self, levels: int = 3) -> list[Any]:
        if self.factor_type in ("discrete", "categorical"):
            return list(self.values or [])
        if levels < 2:
            raise ValueError("levels must be >= 2")
        return [self.lower + (self.upper - self.lower) * i / (levels - 1) for i in range(levels)]

    def low_high(self) -> tuple[Any, Any]:
        """Return the (low, high) level used by two-level designs."""
        if self.factor_type == "continuous":
            return self.lower, self.upper
        if self.is_numeric:
            ordered = sorted(self.values)
            return ordered[0], ordered[-1]
        return self.values[0], self.values[-1]

    def decode(self, coded: float) -> Any:
        """Map a coded value in [-1, 1] back to the real continuous range."""
        if self.factor_type != "continuous":
            raise ValueError("only continuous factors have a coded scale")
        return self.lower + (coded + 1.0) / 2.0 * (self.upper - self.lower)

    def encode(self, value: float) -> float:
        """Map a real numeric value to the coded [-1, 1] scale."""
        low, high = self.low_high()
        return 2.0 * (float(value) - low) / (high - low) - 1.0


# -------------------------------------------------------------- objectives
class Objective(TFModel):
    """Optimisation goal for one metric. ``function`` optionally names a registered
    objective plugin computing the value from run outcomes."""

    metric: str = Field(min_length=1)
    direction: Literal["minimize", "maximize", "target"]
    target: float | None = None
    weight: float = Field(default=1.0, gt=0)
    priority: int | None = Field(default=None, ge=1)
    function: str | None = None

    @model_validator(mode="after")
    def target_required(self):
        if self.direction == "target" and self.target is None:
            raise ValueError("target objective requires a target value")
        if self.direction != "target" and self.target is not None:
            raise ValueError("target is only valid for target-direction objectives")
        if self.target is not None and not math.isfinite(self.target):
            raise ValueError("objective target must be finite")
        return self


class Constraint(TFModel):
    """Restriction on a factor (``input``) or on a measured outcome (``output``).

    ``kind='auto'`` resolves to input when ``metric`` names a factor and output when it
    names a metric. ``function`` names a registered constraint plugin
    ``fn(configuration, outcomes) -> bool`` for cross-variable rules.
    """

    metric: str = Field(default="", description="factor or metric name")
    kind: Literal["auto", "input", "output"] = "auto"
    minimum: float | None = None
    maximum: float | None = None
    equals: Any | None = None
    allowed_values: list[Any] | None = None
    function: str | None = None
    description: str = ""
    validator: Callable[[Any], bool] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def valid_bounds(self):
        if not self.metric and not self.function:
            raise ValueError("constraint requires a metric/factor name or a function")
        for bound in (self.minimum, self.maximum):
            if bound is not None and not math.isfinite(bound):
                raise ValueError("constraint bounds must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("constraint minimum cannot exceed maximum")
        if self.equals is not None and (self.minimum is not None or self.maximum is not None or self.allowed_values):
            raise ValueError("equality cannot be combined with bounds or allowed_values")
        if self.minimum is not None and self.maximum is not None and self.minimum == self.maximum:
            raise ValueError("equal bounds should be represented as equals")
        if self.allowed_values is not None and not self.allowed_values:
            raise ValueError("allowed_values cannot be empty")
        if self.validator is not None and not callable(self.validator):
            raise ValueError("validator must be callable")
        if (
            self.minimum is None
            and self.maximum is None
            and self.equals is None
            and not self.allowed_values
            and self.validator is None
            and self.function is None
        ):
            raise ValueError("constraint must define a condition")
        if self.function is not None and self.kind == "auto":
            raise ValueError("function constraints must set kind to 'input' or 'output'")
        return self

    def accepts(self, value: Any) -> bool:
        """Check ``value`` against the declarative condition (not ``function``)."""
        if self.equals is not None and value != self.equals:
            return False
        if self.allowed_values is not None and value not in self.allowed_values:
            return False
        if self.minimum is not None and (not isinstance(value, (int, float)) or value < self.minimum):
            return False
        if self.maximum is not None and (not isinstance(value, (int, float)) or value > self.maximum):
            return False
        return self.validator(value) if self.validator else True


class MeasurementDefinition(TFModel):
    """Declares a metric: unit, type, expected range, aggregation, time-series settings, outlier method and
    registered transformations.
    """

    metric: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    data_type: Literal["number", "integer", "string", "boolean"] = "number"
    expected_min: float | None = None
    expected_max: float | None = None
    aggregation: Literal["mean", "median", "sum", "min", "max", "last", "none"] = "mean"
    required: bool = True
    time_series: bool = False
    sampling_interval_seconds: float | None = Field(default=None, gt=0)
    outlier_method: Literal["mad", "iqr", "none"] = "mad"
    transformations: list[dict[str, Any]] = Field(default_factory=list)
    """Ordered sample transformations, each ``{"name": <registered>, "params": {...}}``."""
    description: str = ""

    @model_validator(mode="after")
    def ranges_valid(self):
        if self.expected_min is not None and self.expected_max is not None and self.expected_min > self.expected_max:
            raise ValueError("measurement expected_min cannot exceed expected_max")
        if self.data_type in ("string", "boolean") and (self.expected_min is not None or self.expected_max is not None):
            raise ValueError("numeric expected bounds require a number or integer measurement")
        return self


class DerivedMetric(TFModel):
    """A per-run metric computed from other metrics' processed values.

    Exactly one of ``expression`` (restricted arithmetic over metric names) or
    ``function`` (a registered metric plugin) must be given.
    """

    name: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    unit: str = Field(min_length=1)
    expression: str | None = None
    function: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def one_source(self):
        if (self.expression is None) == (self.function is None):
            raise ValueError("derived metric needs exactly one of expression or function")
        if self.expression is not None:
            parse_expression(self.expression)
        return self


class ExecutionPolicy(TFModel):
    """Per-experiment execution behaviour."""

    max_retries: int = Field(default=0, ge=0, le=20)
    retry_delay_seconds: float = Field(default=0.0, ge=0)
    max_concurrency: int = Field(default=1, ge=1, le=256)
    run_timeout_seconds: float | None = Field(default=None, gt=0)
    recovery: Literal["auto", "fail", "retry", "resume"] = "auto"
    """auto: choose from adapter capabilities; fail: mark interrupted runs failed;
    retry: re-queue; resume: query the adapter handle."""


class ExperimentSpec(TFModel):
    """Complete, validated definition of an experiment: factors, objectives, input/output constraints, measurement
    definitions, derived metrics, design method/options, adapter name, execution policy, budget and seed.
    """

    schema_version: str = MODEL_SCHEMA_VERSION
    experiment_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    description: str = ""
    hypothesis: str = ""
    factors: list[Factor] = Field(min_length=1)
    objectives: list[Objective] = Field(min_length=1)
    constraints: list[Constraint] = Field(default_factory=list)
    measurement_definitions: list[MeasurementDefinition] = Field(default_factory=list)
    derived_metrics: list[DerivedMetric] = Field(default_factory=list)
    design_method: str = "factorial"
    design_options: dict[str, Any] = Field(default_factory=dict)
    adapter_name: str | None = None
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    run_budget: int = Field(gt=0)
    random_seed: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    # -- helpers -------------------------------------------------------------
    @property
    def treatment_factors(self) -> list[Factor]:
        return [f for f in self.factors if f.role == "treatment"]

    @property
    def block_factors(self) -> list[Factor]:
        return [f for f in self.factors if f.role == "block"]

    @property
    def metric_names(self) -> set[str]:
        return {m.metric for m in self.measurement_definitions} | {d.name for d in self.derived_metrics}

    def constraint_kind(self, constraint: Constraint) -> Literal["input", "output"]:
        """Resolve a constraint to input or output scope."""
        if constraint.kind != "auto":
            return constraint.kind
        return "input" if constraint.metric in {f.name for f in self.factors} else "output"

    def input_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if self.constraint_kind(c) == "input"]

    def output_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if self.constraint_kind(c) == "output"]

    @model_validator(mode="after")
    def validate_references(self):
        factor_names = [f.name for f in self.factors]
        if self.design_method not in DESIGN_METHODS:
            raise ValueError(f"unsupported design method '{self.design_method}'; supported: {sorted(DESIGN_METHODS)}")
        if len(set(factor_names)) != len(factor_names):
            raise ValueError("factor names must be unique")
        if not self.treatment_factors:
            raise ValueError("at least one treatment factor is required")
        metric_names = [m.metric for m in self.measurement_definitions]
        if len(set(metric_names)) != len(metric_names):
            raise ValueError("measurement metric names must be unique")
        derived_names = [d.name for d in self.derived_metrics]
        if len(set(derived_names)) != len(derived_names) or set(derived_names) & set(metric_names):
            raise ValueError("derived metric names must be unique and distinct from measured metrics")
        if set(factor_names) & (set(metric_names) | set(derived_names)):
            raise ValueError("a name cannot be both a factor and a metric")
        for derived in self.derived_metrics:
            if derived.expression is not None:
                missing = expression_names(derived.expression) - set(metric_names) - set(derived_names)
                if missing:
                    raise ValueError(f"derived metric '{derived.name}' references undefined metrics: {sorted(missing)}")
        declared = self.metric_names
        for objective in self.objectives:
            if objective.function is None and objective.metric not in declared:
                raise ValueError(f"objective metric '{objective.metric}' is not defined")
        objective_keys = [(o.metric, o.direction) for o in self.objectives]
        if len(set(objective_keys)) != len(objective_keys):
            raise ValueError("duplicate objective definitions")
        names = set(factor_names)
        for constraint in self.constraints:
            kind = self.constraint_kind(constraint)
            name = constraint.metric
            if constraint.function is not None:
                continue
            if kind == "input" and name not in names:
                raise ValueError(f"input constraint references unknown factor '{name}'")
            if kind == "output":
                if name in names:
                    raise ValueError(f"output constraint '{name}' names a factor; use kind='input'")
                if declared and name not in declared:
                    raise ValueError(f"constraint metric or factor '{name}' is not defined")
        self._check_conflicting_constraints()
        for factor in self.factors:
            if not any(self._factor_value_allowed(factor, v) for v in factor.candidates(5)):
                raise ValueError(f"no value of factor '{factor.name}' satisfies its constraints")
        return self

    def _factor_value_allowed(self, factor: Factor, value: Any) -> bool:
        return factor.accepts(value) and all(
            c.accepts(value) for c in self.input_constraints() if c.metric == factor.name and c.function is None
        )

    def _check_conflicting_constraints(self) -> None:
        by_name: dict[tuple[str, str], list[Constraint]] = {}
        for c in self.constraints:
            if c.function is None:
                by_name.setdefault((self.constraint_kind(c), c.metric), []).append(c)
        for (_, name), group in by_name.items():
            lo = max((c.minimum for c in group if c.minimum is not None), default=None)
            hi = min((c.maximum for c in group if c.maximum is not None), default=None)
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"conflicting constraints on '{name}': minimum {lo} exceeds maximum {hi}")
            equals = {repr(c.equals) for c in group if c.equals is not None}
            if len(equals) > 1:
                raise ValueError(f"conflicting equality constraints on '{name}'")


# ------------------------------------------------------------- lifecycle
class RunStatus(StrEnum):
    """Run lifecycle states; see ``RUN_TRANSITIONS`` for legal moves."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_MEASUREMENTS = "waiting_for_measurements"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentStatus(StrEnum):
    """Experiment lifecycle states; see ``EXPERIMENT_TRANSITIONS`` for legal moves."""

    CREATED = "created"
    VALIDATED = "validated"
    DESIGNED = "designed"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_MEASUREMENTS = "waiting_for_measurements"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_R = RunStatus
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    _R.CREATED: frozenset({_R.QUEUED, _R.CANCELLED}),
    _R.QUEUED: frozenset({_R.RUNNING, _R.CANCELLED, _R.FAILED}),
    _R.RUNNING: frozenset({_R.WAITING_FOR_MEASUREMENTS, _R.COMPLETED, _R.FAILED, _R.CANCELLED, _R.QUEUED}),
    _R.WAITING_FOR_MEASUREMENTS: frozenset({_R.COMPLETED, _R.FAILED, _R.CANCELLED, _R.QUEUED}),
    _R.COMPLETED: frozenset(),
    _R.FAILED: frozenset({_R.QUEUED}),  # only via explicit retry
    _R.CANCELLED: frozenset(),
}
_E = ExperimentStatus
EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    _E.CREATED: frozenset({_E.VALIDATED, _E.FAILED, _E.CANCELLED}),
    _E.VALIDATED: frozenset({_E.DESIGNED, _E.FAILED, _E.CANCELLED}),
    _E.DESIGNED: frozenset({_E.QUEUED, _E.RUNNING, _E.CANCELLED, _E.FAILED}),
    _E.QUEUED: frozenset({_E.RUNNING, _E.CANCELLED, _E.FAILED}),
    _E.RUNNING: frozenset({_E.WAITING_FOR_MEASUREMENTS, _E.COMPLETED, _E.FAILED, _E.CANCELLED, _E.QUEUED}),
    _E.WAITING_FOR_MEASUREMENTS: frozenset({_E.RUNNING, _E.COMPLETED, _E.FAILED, _E.CANCELLED, _E.QUEUED}),
    _E.COMPLETED: frozenset({_E.QUEUED, _E.RUNNING}),  # sequential extension
    _E.FAILED: frozenset({_E.QUEUED, _E.RUNNING}),  # explicit retry
    _E.CANCELLED: frozenset(),
}


# ----------------------------------------------------------- measurements
class Measurement(TFModel):
    """A single raw or adapter-provided observation.

    ``raw_value``/``raw_unit`` are populated when the engine converts units, so the
    original reading is always recoverable. ``source`` identifies provenance
    (instrument, file, simulator); ``metadata['synthetic']`` should be True for
    simulated data.
    """

    measurement_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    metric: str = Field(min_length=1)
    value: Any
    unit: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=utcnow)
    quality_flags: list[str] = Field(default_factory=list)
    raw_value: Any | None = None
    raw_unit: str | None = None
    source: str | None = None
    channel: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MeasurementTransformation(TFModel):
    """Audit record of one operation (unit conversion, transformation, aggregation, derivation) applied to measurements."""

    transformation_id: str = Field(default_factory=lambda: str(uuid4()))
    operation: str
    input_measurement_ids: list[str]
    output_measurement_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)


class ProcessedMeasurement(TFModel):
    """Aggregated/derived value for one run and metric with source measurement ids, quality flags and transformation history."""

    processed_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    metric: str
    value: Any
    unit: str
    timestamp: datetime = Field(default_factory=utcnow)
    source_measurement_ids: list[str]
    quality_flags: list[str] = Field(default_factory=list)
    transformations: list[MeasurementTransformation] = Field(default_factory=list)
    window_start: datetime | None = None
    window_end: datetime | None = None
    derived: bool = False


class Run(TFModel):
    """One experimental run: configuration, lifecycle status and history, raw and processed measurements, attempts
    and block assignment.
    """

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    experiment_id: str
    configuration: dict[str, Any]
    order: int = Field(ge=0)
    status: RunStatus = RunStatus.QUEUED
    adapter_name: str | None = None
    adapter_handle: str | None = None
    measurements: list[Measurement] = Field(default_factory=list)
    processed_measurements: list[ProcessedMeasurement] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    block: dict[str, Any] = Field(default_factory=dict)
    coded: dict[str, float] = Field(default_factory=dict)
    replicate: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)

    def transition(self, new: RunStatus, *, reason: str = "") -> None:
        """Move to ``new`` enforcing the lifecycle table; records history."""
        from trialforge.errors import InvalidTransitionError

        if new == self.status:
            return
        if new not in RUN_TRANSITIONS[self.status]:
            raise InvalidTransitionError(f"run {self.run_id}: {self.status.value} -> {new.value} is not allowed")
        self.history.append({"from": self.status.value, "to": new.value, "at": utcnow().isoformat(), "reason": reason})
        self.status = new


class DesignPlan(TFModel):
    """A generated design: runs with configurations, randomisation, block variables, seed and design metadata
    (defining relations, alias structure, resolution where applicable).
    """

    experiment_id: str
    method: str
    runs: list[Run]
    control_configuration: dict[str, Any]
    random_seed: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    block_variables: list[str] = Field(default_factory=list)
    randomization: dict[str, Any] = Field(default_factory=dict)
    design_info: dict[str, Any] = Field(default_factory=dict)
    treatment_factors: list[str] = Field(default_factory=list)


class ExperimentResult(TFModel):
    """A specification, its design plan and the current state of all runs."""

    spec: ExperimentSpec
    plan: DesignPlan
    runs: list[Run]
    status: ExperimentStatus
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------- analysis
class EffectEstimate(TFModel):
    """A treatment/control or factor-level comparison for one metric.

    ``evidence='insufficient'`` means the design/sample does not justify inference;
    in that case interval and p-value fields are None and ``warnings`` says why.
    """

    metric: str
    control_mean: float | None = None
    treatment_mean: float | None = None
    mean_difference: float | None = None
    relative_change_percent: float | None = None
    sample_size_control: int = 0
    sample_size_treatment: int = 0
    standard_error: float | None = None
    confidence_interval_95: tuple[float, float] | None = None
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    comparison_type: Literal["treatment_vs_control", "factor_level", "paired", "configuration"] = "treatment_vs_control"
    factor: str | None = None
    level: Any | None = None
    control_level: Any | None = None
    differing_factors: list[str] = Field(default_factory=list)
    treatment_configuration: dict[str, Any] = Field(default_factory=dict)
    control_configuration: dict[str, Any] = Field(default_factory=dict)
    control_run_ids: list[str] = Field(default_factory=list)
    treatment_run_ids: list[str] = Field(default_factory=list)
    degrees_of_freedom: float | None = None
    t_statistic: float | None = None
    p_value: float | None = None
    effect_size: float | None = None
    effect_size_type: str | None = None
    ci_method: str | None = None
    bootstrap_ci_95: tuple[float, float] | None = None
    evidence: Literal["sufficient", "insufficient"] = "insufficient"
    p_value_holm: float | None = None
    n_comparisons_in_family: int | None = None
    standardized_note: str | None = None
    warnings: list[str] = Field(default_factory=list)


class InteractionEffect(TFModel):
    """Two-factor interaction for one metric: difference-of-differences estimate plus ANOVA F test where the design
    supports it.
    """

    metric: str
    factors: tuple[str, str]
    estimate: float | None = None
    description: str = ""
    f_statistic: float | None = None
    p_value: float | None = None
    degrees_of_freedom: tuple[float, float] | None = None
    run_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AnovaRow(TFModel):
    term: str
    sum_squares: float
    df: float
    mean_square: float | None = None
    f_statistic: float | None = None
    p_value: float | None = None


class AnovaTable(TFModel):
    """ANOVA table (one-way per factor, or Type II factorial) for one metric, with residual degrees of freedom and warnings."""

    metric: str
    kind: Literal["one_way", "factorial"]
    factor: str | None = None
    rows: list[AnovaRow]
    residual_df: float
    n_observations: int
    run_ids: list[str] = Field(default_factory=list)
    sum_squares_type: str = "II"
    warnings: list[str] = Field(default_factory=list)
    model_info: dict[str, Any] = Field(default_factory=dict)  # parameters, rank, replication and what the model can support


class Coefficient(TFModel):
    term: str
    estimate: float
    std_error: float | None = None
    t_statistic: float | None = None
    p_value: float | None = None
    ci_95: tuple[float, float] | None = None


class RegressionModel(TFModel):
    """OLS model for one metric with coefficients, fit statistics, coding description, diagnostics and warnings."""

    metric: str
    terms: list[str]
    coefficients: list[Coefficient]
    n_observations: int
    n_parameters: int
    residual_df: float
    r_squared: float | None = None
    adjusted_r_squared: float | None = None
    residual_std_error: float | None = None
    f_statistic: float | None = None
    f_p_value: float | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    coding: dict[str, str] = Field(default_factory=dict)
    run_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AnalysisResult(TFModel):
    """Output of :meth:`AnalysisEngine.analyse`. Every estimate lists the runs it used; excluded runs carry reasons;
    warnings state statistical limitations.
    """

    experiment_id: str
    descriptive: dict[str, dict[str, float | int | None]]
    effects: list[EffectEstimate]
    factor_effects: dict[str, dict[str, float]] = Field(default_factory=dict)
    pareto_run_ids: list[str] = Field(default_factory=list)
    method: str
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    # --- 0.2 additions
    algorithm: str = "trialforge.analysis"
    algorithm_version: str = "2.0"
    created_at: datetime = Field(default_factory=utcnow)
    input_run_ids: list[str] = Field(default_factory=list)
    excluded_runs: list[dict[str, Any]] = Field(default_factory=list)
    sample_sizes: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    interactions: list[InteractionEffect] = Field(default_factory=list)
    anova: list[AnovaTable] = Field(default_factory=list)
    regression: list[RegressionModel] = Field(default_factory=list)
    feasible_run_ids: list[str] = Field(default_factory=list)
    infeasible_runs: list[dict[str, Any]] = Field(default_factory=list)
    tradeoffs: list[dict[str, Any]] = Field(default_factory=list)
    best_run_id: str | None = None
    outlier_flags: list[dict[str, Any]] = Field(default_factory=list)
    factor_effects_by_metric: dict[str, dict[str, dict[str, float]]] = Field(default_factory=dict)
    main_effects: list[EffectEstimate] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------- optimisation
class Recommendation(TFModel):
    """A proposed next configuration.

    ``prediction_kind`` states exactly what the numbers mean:
    ``model_prediction`` (probabilistic surrogate), ``heuristic_score`` (uncalibrated),
    or ``search`` (no prediction, e.g. grid/random). ``expected_improvement`` is set
    only by strategies that compute a model-based expected improvement.
    """

    configuration: dict[str, Any]
    rationale: str
    expected_improvement: float | None = None
    uncertainty: str = "Not estimable from the available data."
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    # --- 0.2 additions
    strategy: str | None = None
    prediction_kind: Literal["model_prediction", "heuristic_score", "search"] = "search"
    objective: str | None = None
    predicted_value: float | None = None
    prediction_std: float | None = None
    heuristic_score: float | None = None
    feasible: bool | None = None
    predicted_feasibility_probability: float | None = None
    constraint_evaluation: list[dict[str, Any]] = Field(default_factory=list)
    remaining_budget: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    experiment_id: str | None = None


class StopDecision(TFModel):
    """Result of evaluating stopping rules: whether to stop, the reason and detail."""

    stop: bool
    reason: Literal[
        "continue",
        "budget_exhausted",
        "target_achieved",
        "improvement_threshold_met",
        "max_iterations",
        "insufficient_improvement",
        "no_feasible_candidate",
        "user_cancelled",
    ] = "continue"
    detail: str = ""


class ExperimentReport(TFModel):
    """Versioned experiment report (see ``report_version``): definition, design, runs, quality, statistics,
    optimisation history, limitations and reproducibility metadata.
    """

    experiment_id: str
    name: str
    objective: list[Objective]
    hypothesis: str
    variables: list[Factor]
    design_method: str
    planned_runs: int
    completed_runs: int
    failed_runs: int
    invalid_runs: int
    measurement_summaries: dict[str, dict[str, float | int | None]]
    treatment_effects: list[EffectEstimate]
    best_configuration: dict[str, Any] | None
    constraint_violations: list[dict[str, Any]]
    optimisation_history: list[Recommendation]
    suggested_next_experiment: Recommendation | None
    limitations: list[str]
    # --- 0.2 additions
    report_version: str = "2.0"
    generated_at: datetime = Field(default_factory=utcnow)
    description: str = ""
    constraints: list[Constraint] = Field(default_factory=list)
    design_info: dict[str, Any] = Field(default_factory=dict)
    planned_run_details: list[dict[str, Any]] = Field(default_factory=list)
    failed_run_details: list[dict[str, Any]] = Field(default_factory=list)
    invalid_measurements: list[dict[str, Any]] = Field(default_factory=list)
    interactions: list[InteractionEffect] = Field(default_factory=list)
    anova: list[AnovaTable] = Field(default_factory=list)
    regression: list[RegressionModel] = Field(default_factory=list)
    model_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    best_observed_run_id: str | None = None
    pareto_run_ids: list[str] = Field(default_factory=list)
    infeasible_runs: list[dict[str, Any]] = Field(default_factory=list)
    data_quality_issues: list[dict[str, Any]] = Field(default_factory=list)
    analysis_warnings: list[str] = Field(default_factory=list)
    reproducibility: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)
