"""TRIALFORGE public developer SDK.

Stable public API (semantic versioning): everything in ``__all__``. Deep module paths may change.
"""

from trialforge.adapters.contracts import (
    AdapterCapabilities,
    AdapterError,
    AdapterMetadata,
    AdapterRegistry,
    ExecutionContext,
    ExecutionResponse,
    ExecutionStatusResponse,
    ExperimentAdapter,
    MeasurementResponse,
    check_config_against_schema,
    schema_from_spec,
)
from trialforge.analysis.engine import AnalysisEngine
from trialforge.definitions.registry import DEFAULT_PLUGINS, PluginRegistry
from trialforge.designer.engine import DesignError, ExperimentDesigner
from trialforge.errors import (
    AdapterUnavailableError,
    AnalysisError,
    CapabilityError,
    ExecutionError,
    InvalidTransitionError,
    OptimizationError,
    PersistenceError,
    PluginError,
    TrialforgeError,
    ValidationError,
)
from trialforge.events import Event, EventBus
from trialforge.execution.background import LocalBackgroundRunner
from trialforge.execution.service import ExperimentService
from trialforge.history import ExperimentHistory
from trialforge.measurements.engine import MeasurementEngine, StreamingWindowAggregator, make_series
from trialforge.measurements.units import DEFAULT_UNITS, UnitRegistry, convert
from trialforge.models.core import (
    AnalysisResult,
    AnovaTable,
    Constraint,
    DerivedMetric,
    DesignPlan,
    EffectEstimate,
    ExecutionPolicy,
    ExperimentReport,
    ExperimentResult,
    ExperimentSpec,
    ExperimentStatus,
    Factor,
    InteractionEffect,
    Measurement,
    MeasurementDefinition,
    MeasurementTransformation,
    Objective,
    ProcessedMeasurement,
    Recommendation,
    RegressionModel,
    Run,
    RunStatus,
    StopDecision,
)
from trialforge.optimization.engine import Optimizer, OptimizerContext, StoppingCriteria
from trialforge.persistence.repository import (
    ExperimentRepository,
    InMemoryExperimentRepository,
    SQLiteExperimentRepository,
)
from trialforge.reporting.engine import ReportEngine

# Short alias used in the developer guide.
Analyzer = AnalysisEngine

__version__ = "0.2.2"
API_VERSION = "1"

__all__ = [
    "AdapterCapabilities",
    "AdapterError",
    "AdapterMetadata",
    "AdapterRegistry",
    "AdapterUnavailableError",
    "AnalysisEngine",
    "AnalysisError",
    "AnalysisResult",
    "Analyzer",
    "AnovaTable",
    "API_VERSION",
    "CapabilityError",
    "Constraint",
    "DEFAULT_PLUGINS",
    "DEFAULT_UNITS",
    "DerivedMetric",
    "DesignError",
    "DesignPlan",
    "EffectEstimate",
    "Event",
    "EventBus",
    "ExecutionContext",
    "ExecutionError",
    "ExecutionPolicy",
    "ExecutionResponse",
    "ExecutionStatusResponse",
    "ExperimentAdapter",
    "ExperimentDesigner",
    "ExperimentHistory",
    "ExperimentReport",
    "ExperimentRepository",
    "ExperimentResult",
    "ExperimentService",
    "ExperimentSpec",
    "ExperimentStatus",
    "Factor",
    "InMemoryExperimentRepository",
    "InteractionEffect",
    "InvalidTransitionError",
    "LocalBackgroundRunner",
    "Measurement",
    "MeasurementDefinition",
    "MeasurementEngine",
    "MeasurementResponse",
    "MeasurementTransformation",
    "Objective",
    "OptimizationError",
    "Optimizer",
    "OptimizerContext",
    "PersistenceError",
    "PluginError",
    "PluginRegistry",
    "ProcessedMeasurement",
    "Recommendation",
    "RegressionModel",
    "ReportEngine",
    "Run",
    "RunStatus",
    "SQLiteExperimentRepository",
    "StopDecision",
    "StoppingCriteria",
    "StreamingWindowAggregator",
    "TrialforgeError",
    "UnitRegistry",
    "ValidationError",
    "check_config_against_schema",
    "convert",
    "make_series",
    "schema_from_spec",
]
