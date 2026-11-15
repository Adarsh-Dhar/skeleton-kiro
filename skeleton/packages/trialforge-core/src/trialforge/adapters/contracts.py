"""Public contracts for explicitly registered application adapters.

An adapter connects TRIALFORGE to a domain system (simulator, meter, rig). The required
protocol is five coroutines; four *optional* hooks refine behaviour:

* ``execute_run(context)``  - concurrency-safe execution keyed by run id (preferred over
  ``prepare`` + ``execute`` which share adapter state and are therefore serialised).
* ``status(handle)``       - query a previously started execution (needed for restart recovery).
* ``available()``          - cheap health check before scheduling.
* ``startup()`` / ``shutdown()`` - lifecycle management.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from trialforge.errors import (
    AdapterError,
    AdapterUnavailableError,
    CapabilityError,
)
from trialforge.models.core import ExperimentSpec, Measurement

__all__ = [
    "AdapterCapabilities",
    "AdapterError",
    "AdapterMetadata",
    "AdapterRegistry",
    "ExecutionContext",
    "ExecutionResponse",
    "ExperimentAdapter",
    "MeasurementResponse",
    "ExecutionStatusResponse",
    "check_config_against_schema",
    "schema_from_spec",
]


@dataclass(frozen=True)
class AdapterMetadata:
    """Identity and documentation of an adapter."""

    name: str
    version: str = "1.0"
    description: str = ""
    domain: str = ""
    synthetic: bool = False  # True when data are simulated rather than measured
    input_schema: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterCapabilities:
    """What the adapter can do; the engine adapts its behaviour to these."""

    immediate_execution: bool = True
    delayed_measurements: bool = False
    cancellation: bool = False
    time_series: bool = False
    concurrent: bool = False  # safe to execute several runs at once
    max_concurrency: int = 1
    retryable: bool = True  # re-executing a failed run is safe
    idempotent: bool = False  # same run id twice => same effect, no duplication
    resumable: bool = False  # ``status``/``collect_result`` work after a process restart


class ExecutionContext(BaseModel):
    """Everything an adapter receives for one run: ids, configuration, attempt number, idempotency key, seed and
    metadata (including ``requested_metrics``).
    """

    experiment_id: str
    run_id: str
    configuration: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    adapter_name: str | None = None
    attempt: int = 1
    idempotency_key: str | None = None
    random_seed: int | None = None


class ExecutionResponse(BaseModel):
    """Adapter result of starting a run: either completed with measurements, or incomplete with a handle for delayed
    collection.
    """

    completed: bool
    measurements: list[Measurement] = Field(default_factory=list)
    handle: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MeasurementResponse(BaseModel):
    """Adapter result of ``collect_result``: completed flag and any measurements received so far."""

    completed: bool
    measurements: list[Measurement] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionStatusResponse(BaseModel):
    """Result of ``adapter.status(handle)``."""

    state: str  # 'pending' | 'running' | 'completed' | 'failed' | 'unknown'
    detail: str = ""


@runtime_checkable
class ExperimentAdapter(Protocol):
    """Structural protocol every adapter implements (see the adapter development guide).

    Required attributes: ``metadata`` and ``capabilities``. Required coroutines: ``validate``, ``prepare``,
    ``execute``, ``collect_result`` and ``cancel``. Optional hooks: ``execute_run``, ``status``,
    ``available``, ``startup``, ``shutdown``.
    """

    metadata: AdapterMetadata
    capabilities: AdapterCapabilities

    async def validate(self, config: dict[str, Any]) -> None:
        """Raise (preferably AdapterError) if ``config`` cannot be executed; return None when acceptable."""
        ...

    async def prepare(self, context: ExecutionContext) -> None:
        """Receive the run context before ``execute`` (used by adapters without ``execute_run``)."""
        ...

    async def execute(self, config: dict[str, Any]) -> ExecutionResponse:
        """Run the configuration. Return ``completed=True`` with measurements, or ``completed=False`` with a handle."""
        ...

    async def collect_result(self, run_id: str) -> MeasurementResponse:
        """Return measurements of a delayed run; ``completed=False`` while they are not yet available."""
        ...

    async def cancel(self, run_id: str) -> None:
        """Best-effort cancellation of an in-flight run."""
        ...


def schema_from_spec(spec: ExperimentSpec) -> dict[str, Any]:
    """Derive a simple input schema ({factor: {type, min/max or choices, unit}})."""
    out: dict[str, Any] = {}
    for f in spec.factors:
        item: dict[str, Any] = {"type": f.factor_type, "unit": f.unit}
        if f.factor_type == "continuous":
            item.update(min=f.lower, max=f.upper)
        else:
            item["choices"] = list(f.values)
        out[f.name] = item
    return out


def check_config_against_schema(config: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Return a list of schema violations (empty = valid). Schema format as :func:`schema_from_spec`."""
    problems = []
    for name, rule in schema.items():
        if name not in config:
            if rule.get("required", True):
                problems.append(f"missing '{name}'")
            continue
        v = config[name]
        if "choices" in rule and v not in rule["choices"]:
            problems.append(f"'{name}'={v!r} not in {rule['choices']}")
        if "min" in rule and rule["min"] is not None and (not isinstance(v, (int, float)) or v < rule["min"]):
            problems.append(f"'{name}'={v!r} below minimum {rule['min']}")
        if "max" in rule and rule["max"] is not None and (not isinstance(v, (int, float)) or v > rule["max"]):
            problems.append(f"'{name}'={v!r} above maximum {rule['max']}")
    for name in config:
        if name not in schema:
            problems.append(f"unexpected '{name}'")
    return problems


class AdapterRegistry:
    """Explicit registry: no implicit imports or dynamic code execution."""

    def __init__(self) -> None:
        self._adapters: dict[str, ExperimentAdapter] = {}
        self._started: set[str] = set()

    # -- registration -----------------------------------------------------
    def register(self, adapter: ExperimentAdapter, *, name: str | None = None, replace: bool = False) -> None:
        """Register ``adapter`` under ``name`` (default ``adapter.metadata.name``)."""
        if not isinstance(adapter, ExperimentAdapter):
            raise AdapterError(
                "object does not implement the ExperimentAdapter protocol "
                "(needs metadata, capabilities, validate, prepare, execute, collect_result, cancel)"
            )
        adapter_name = name or adapter.metadata.name
        if not adapter_name:
            raise ValueError("adapter name cannot be empty")
        if adapter_name in self._adapters and not replace:
            raise ValueError(f"adapter '{adapter_name}' is already registered")
        caps = adapter.capabilities
        if not (caps.immediate_execution or caps.delayed_measurements):
            raise AdapterError(f"adapter '{adapter_name}' declares neither immediate nor delayed execution")
        if caps.max_concurrency < 1:
            raise AdapterError("max_concurrency must be >= 1")
        self._adapters[adapter_name] = adapter

    def unregister(self, name: str) -> None:
        """Remove an adapter by name (no error if absent)."""
        self._adapters.pop(name, None)
        self._started.discard(name)

    def get(self, name: str) -> ExperimentAdapter:
        """Return the adapter registered as ``name``; raises KeyError if unknown."""
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"no adapter registered as '{name}'") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._adapters

    def names(self) -> list[str]:
        """Registered adapter names in registration order."""
        return list(self._adapters)

    def list(self) -> list[AdapterMetadata]:
        """Metadata of every registered adapter."""
        return [a.metadata for a in self._adapters.values()]

    # -- selection ----------------------------------------------------------
    def select(self, spec: ExperimentSpec | None = None, *, name: str | None = None) -> tuple[str, ExperimentAdapter]:
        """Pick the adapter: explicit ``name`` > ``spec.adapter_name`` > sole registered adapter."""
        chosen = name or (spec.adapter_name if spec else None)
        if chosen is None:
            if len(self._adapters) == 1:
                chosen = next(iter(self._adapters))
            elif not self._adapters:
                raise AdapterUnavailableError("no adapters are registered")
            else:
                raise AdapterUnavailableError(f"experiment does not name an adapter and several are registered: {self.names()}")
        if chosen not in self._adapters:
            raise AdapterUnavailableError(f"adapter '{chosen}' is not registered; available: {self.names()}")
        return chosen, self._adapters[chosen]

    @staticmethod
    def required_capabilities(spec: ExperimentSpec) -> list[str]:
        """Capabilities the experiment needs from its adapter."""
        need = []
        if any(d.time_series for d in spec.measurement_definitions):
            need.append("time_series")
        return need

    def check_capabilities(self, adapter: ExperimentAdapter, spec: ExperimentSpec) -> list[str]:
        """Raise CapabilityError for hard requirements; return non-fatal notes."""
        notes = []
        for cap in self.required_capabilities(spec):
            if not getattr(adapter.capabilities, cap, False):
                raise CapabilityError(f"adapter '{adapter.metadata.name}' lacks required capability '{cap}'")
        policy = spec.execution
        if policy.max_concurrency > 1 and not adapter.capabilities.concurrent:
            notes.append("adapter is not concurrent-safe; runs execute sequentially")
        if policy.max_retries and not adapter.capabilities.retryable:
            notes.append("adapter is not retryable; retries will not be attempted")
        return notes

    def input_schema(self, name: str) -> dict[str, Any] | None:
        """The declared input schema of adapter ``name`` (None if it declares none)."""
        return self.get(name).metadata.input_schema

    # -- lifecycle ------------------------------------------------------
    async def ensure_available(self, name: str) -> None:
        """Await the adapter's optional ``available()`` check; raises AdapterUnavailableError if it reports False."""
        adapter = self.get(name)
        hook = getattr(adapter, "available", None)
        if hook is not None:
            ok = await hook()
            if not ok:
                raise AdapterUnavailableError(f"adapter '{name}' reports itself unavailable")

    async def start(self, name: str) -> None:
        """Run the adapter's optional ``startup()`` hook once."""
        if name in self._started:
            return
        hook = getattr(self.get(name), "startup", None)
        if hook is not None:
            await hook()
        self._started.add(name)

    async def shutdown_all(self) -> None:
        """Run the optional ``shutdown()`` hook of every started adapter."""
        for name in list(self._started):
            hook = getattr(self._adapters.get(name), "shutdown", None)
            if hook is not None:
                await hook()
            self._started.discard(name)
