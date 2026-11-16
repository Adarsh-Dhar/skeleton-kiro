"""Experiment execution, lifecycle management, persistence and recovery.

Source of truth is the repository: every state transition is persisted *before* the next step, so a
crash leaves an honest record (a run that was RUNNING stays RUNNING until recovery decides, according to
the adapter's declared capabilities and the experiment's ``ExecutionPolicy``). Runs are never silently
completed or duplicated.

Run-budget semantics: the budget bounds the number of *planned runs* (including sequential extensions).
Retries re-execute the same run and do not consume additional budget.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from trialforge.adapters.contracts import (
    AdapterError,
    AdapterRegistry,
    ExecutionContext,
    ExperimentAdapter,
    check_config_against_schema,
)
from trialforge.analysis.engine import AnalysisEngine
from trialforge.definitions.registry import DEFAULT_PLUGINS, PluginRegistry
from trialforge.designer.engine import DesignError, ExperimentDesigner
from trialforge.errors import (
    ExecutionError,
    InvalidTransitionError,
    ValidationError,
)
from trialforge.evaluation import evaluate_constraints, is_feasible, run_outcomes
from trialforge.events import Event, EventBus
from trialforge.measurements.engine import MeasurementEngine
from trialforge.models.core import (
    EXPERIMENT_TRANSITIONS,
    AnalysisResult,
    DesignPlan,
    ExperimentReport,
    ExperimentResult,
    ExperimentSpec,
    ExperimentStatus,
    Measurement,
    Recommendation,
    Run,
    RunStatus,
    StopDecision,
)
from trialforge.optimization.engine import Optimizer, StoppingCriteria
from trialforge.persistence.repository import SQLiteExperimentRepository
from trialforge.reporting.engine import ReportEngine

_TERMINAL_RUN = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class _NoRetry(Exception):
    """Internal: failure that must not be retried (validation errors)."""


class ExperimentService:
    """Facade for the experiment lifecycle.

    ``ExperimentService(adapter)`` (0.1 style) registers the adapter; for several adapters pass a
    pre-populated ``registry`` and set ``ExperimentSpec.adapter_name``. ``repository`` defaults to an
    ephemeral in-memory SQLite database; pass ``SQLiteExperimentRepository('sqlite:///file.db')`` for
    durability across restarts.
    """

    def __init__(
        self,
        adapter: ExperimentAdapter | None = None,
        *,
        registry: AdapterRegistry | None = None,
        repository: SQLiteExperimentRepository | None = None,
        designer: ExperimentDesigner | None = None,
        analysis_engine: AnalysisEngine | None = None,
        optimizer: Optimizer | None = None,
        report_engine: ReportEngine | None = None,
        plugins: PluginRegistry | None = None,
        events: EventBus | None = None,
        strict_required_metrics: bool = True,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.registry = registry or AdapterRegistry()
        if adapter is not None:
            self.registry.register(adapter)
        self.repository = repository or SQLiteExperimentRepository.in_memory()
        self.plugins = plugins or DEFAULT_PLUGINS
        self.designer = designer or ExperimentDesigner()
        self.analysis_engine = analysis_engine or AnalysisEngine(plugins=self.plugins)
        self.optimizer = optimizer or Optimizer(plugins=self.plugins)
        self.report_engine = report_engine or ReportEngine(plugins=self.plugins)
        self.events = events or EventBus()
        self.strict_required_metrics = strict_required_metrics
        self._sleep = sleep
        self._spec_overrides: dict[str, ExperimentSpec] = {}
        self._adapter_locks: dict[str, asyncio.Lock] = {}
        self._cancel_runs: set[str] = set()
        self._cancel_experiments: set[str] = set()
        self.last_stop: dict[str, StopDecision] = {}

    # ===================================================== specs & validation
    def attach_spec(self, spec: ExperimentSpec) -> None:
        """Provide the original spec object (with callables) for an experiment; restores validators lost on reload."""
        self._spec_overrides[spec.experiment_id] = spec

    def get_spec(self, experiment_id: str) -> ExperimentSpec:
        """Retrieve the specification: attached object first, then the repository. Never a hidden cache."""
        stored = None
        try:
            stored = self.repository.get_spec(experiment_id)
        except KeyError:
            pass
        if experiment_id in self._spec_overrides:
            return self._spec_overrides[experiment_id]
        if stored is None:
            raise ExecutionError(
                f"no specification available for experiment {experiment_id}; pass spec= explicitly or design the experiment first"
            )
        return stored

    def validate(self, spec: ExperimentSpec) -> ExperimentSpec:
        """Validate design compatibility, plugin references and (if resolvable) the adapter. Raises ValidationError."""
        problems = self.designer.check(spec)
        for c in spec.constraints:
            if c.function and not self.plugins.has("constraint", c.function):
                problems.append(f"constraint function '{c.function}' is not registered")
        for o in spec.objectives:
            if o.function and not self.plugins.has("objective", o.function):
                problems.append(f"objective function '{o.function}' is not registered")
        try:
            MeasurementEngine(spec.measurement_definitions, derived_metrics=spec.derived_metrics, plugins=self.plugins)
        except ValidationError as exc:
            problems.append(str(exc))
        if spec.adapter_name or len(self.registry.names()) == 1:
            try:
                _, adapter = self.registry.select(spec)
                self.registry.check_capabilities(adapter, spec)
            except AdapterError as exc:
                problems.append(str(exc))
        if problems:
            raise ValidationError("; ".join(problems))
        return spec

    def _validate_configuration(self, spec: ExperimentSpec, configuration: dict[str, Any]) -> None:
        factors = {f.name: f for f in spec.factors}
        if set(configuration) != set(factors):
            raise ValidationError(f"configuration must define exactly: {sorted(factors)}")
        for name, value in configuration.items():
            if not factors[name].accepts(value):
                raise ValidationError(f"invalid value for factor '{name}': {value!r}")
        for c in spec.input_constraints():
            if c.function is None and not c.accepts(configuration[c.metric]):
                raise ValidationError(f"configuration violates constraint on '{c.metric}'")
        if not is_feasible(evaluate_constraints(spec, configuration, None, kinds=("input",), plugins=self.plugins)):
            raise ValidationError("configuration violates an input constraint")

    # ================================================================ design
    def design(self, spec: ExperimentSpec, method: str | None = None) -> DesignPlan:
        """Validate, design and persist an experiment. Re-designing an existing experiment returns its stored plan."""
        self.validate(spec)
        method = method or spec.design_method
        if self.repository.has_experiment(spec.experiment_id):
            try:
                stored = self.repository.get_plan(spec.experiment_id)
            except KeyError:
                stored = None
            if stored is not None:
                if stored.method == method or (stored.method == "sequential"):
                    return self.repository.get(spec.experiment_id).plan
                raise DesignError(
                    f"experiment {spec.experiment_id} is already designed with '{stored.method}'; "
                    "create a new experiment to use a different method"
                )
        plan = self.designer.design(spec, method)  # may raise DesignError: nothing is persisted until the design exists
        self.repository.save_experiment(spec, ExperimentStatus.CREATED)
        self._spec_overrides[spec.experiment_id] = spec
        self._advance_experiment(spec.experiment_id, ExperimentStatus.VALIDATED, "specification validated")
        for run in plan.runs:
            run.idempotency_key = f"{spec.experiment_id}:{run.run_id}"
        self.repository.save_plan(plan)
        self.repository.save_runs(plan.runs)
        self._advance_experiment(spec.experiment_id, ExperimentStatus.DESIGNED, f"{method} design with {len(plan.runs)} runs")
        self._emit("experiment.designed", spec.experiment_id, data={"runs": len(plan.runs), "method": method})
        return plan

    # ============================================================== execution
    async def run(self, spec: ExperimentSpec, method: str | None = None) -> ExperimentResult:
        """Design (or reuse the stored design) and execute all pending runs."""
        plan = self.design(spec, method)
        return await self.execute(plan, spec)

    async def execute(self, plan: DesignPlan, spec: ExperimentSpec | None = None) -> ExperimentResult:
        """Execute QUEUED runs of ``plan``. Safe to call repeatedly: finished runs are never re-executed."""
        spec = spec or self.get_spec(plan.experiment_id)
        exp = spec.experiment_id
        if not self.repository.has_experiment(exp):
            self.repository.save_experiment(spec, ExperimentStatus.CREATED)
            self._advance_experiment(exp, ExperimentStatus.VALIDATED, "implicit validation")
        try:
            self.repository.get_plan(exp)
        except KeyError:
            self.repository.save_plan(plan)
            self.repository.save_runs(plan.runs)
            self._advance_experiment(exp, ExperimentStatus.DESIGNED, "plan registered at execution")
        self._spec_overrides.setdefault(exp, spec)
        return await self._execute_pending(spec)

    async def run_pending(self, experiment_id: str) -> ExperimentResult:
        """Execute whatever is still queued for a stored experiment (e.g. after restart or retry)."""
        return await self._execute_pending(self.get_spec(experiment_id))

    async def _execute_pending(self, spec: ExperimentSpec) -> ExperimentResult:
        exp = spec.experiment_id
        runs = self.repository.list_runs(exp)
        if len(runs) > spec.run_budget:
            raise ExecutionError(f"experiment has {len(runs)} runs, exceeding its budget of {spec.run_budget}")
        self._cancel_experiments.discard(exp)
        todo = [r for r in runs if r.status in (RunStatus.QUEUED, RunStatus.CREATED)]
        for r in todo:  # pre-flight: an invalid plan raises before any adapter call or state change
            self._validate_configuration(spec, r.configuration)
        if todo:
            name, adapter = self.registry.select(spec)
            notes = self.registry.check_capabilities(adapter, spec)
            await self.registry.ensure_available(name)
            await self.registry.start(name)
            self.repository.record_adapter(adapter.metadata, adapter.capabilities)
            caps = adapter.capabilities
            limit = min(spec.execution.max_concurrency, caps.max_concurrency) if caps.concurrent else 1
            if notes:
                for n in notes:
                    self._emit("experiment.note", exp, data={"note": n})
            for r in todo:
                if r.status == RunStatus.CREATED:
                    r.transition(RunStatus.QUEUED, reason="scheduled")
                    self.repository.save_run(r)
                self.repository.enqueue(exp, r.run_id)
            self._advance_experiment(exp, ExperimentStatus.QUEUED, f"{len(todo)} run(s) queued")
            sem = asyncio.Semaphore(max(1, limit))

            async def worker(run: Run) -> None:
                async with sem:
                    await self._execute_run(spec, name, adapter, run)

            await asyncio.gather(*(worker(r) for r in todo))
        return self._refresh_status(exp)

    async def _execute_run(self, spec: ExperimentSpec, name: str, adapter: ExperimentAdapter, run: Run) -> None:
        exp = spec.experiment_id
        policy = spec.execution
        if run.run_id in self._cancel_runs or exp in self._cancel_experiments:
            self._finish_cancelled(run, "cancelled before start")
            return
        # persist RUNNING *before* touching the adapter so a crash is visible
        run.adapter_name = name
        run.started_at = run.started_at or datetime.now(UTC)
        run.transition(RunStatus.RUNNING, reason="started")
        run.attempts += 1
        self.repository.save_run(run)
        self._advance_experiment(exp, ExperimentStatus.RUNNING, "run started")
        self._emit("run.started", exp, run.run_id, {"attempt": run.attempts})
        max_attempts = 1 + (policy.max_retries if adapter.capabilities.retryable else 0)
        last_error = ""
        while True:
            try:
                self._validate_configuration(spec, run.configuration)
                try:
                    await adapter.validate(run.configuration)
                    schema = adapter.metadata.input_schema
                    if schema:
                        bad = check_config_against_schema(run.configuration, schema)
                        if bad:
                            raise ValidationError("adapter schema: " + "; ".join(bad))
                except (ValidationError, AdapterError, ValueError) as exc:
                    raise _NoRetry(f"configuration rejected by adapter: {exc}") from exc
                ctx = ExecutionContext(
                    experiment_id=exp,
                    run_id=run.run_id,
                    configuration=dict(run.configuration),
                    metadata={
                        **run.metadata,
                        "requested_metrics": sorted(m.metric for m in spec.measurement_definitions),
                        "block": dict(run.block),
                    },
                    adapter_name=name,
                    attempt=run.attempts,
                    idempotency_key=run.idempotency_key,
                    random_seed=spec.random_seed,
                )
                coro = self._call_adapter(name, adapter, ctx)
                response = await (asyncio.wait_for(coro, policy.run_timeout_seconds) if policy.run_timeout_seconds else coro)
                if run.run_id in self._cancel_runs or exp in self._cancel_experiments:
                    self._finish_cancelled(run, "cancelled during execution")
                    return
                if response.completed:
                    self._finalize(spec, run, response.measurements, response.metadata)
                else:
                    run.adapter_handle = response.handle or run.run_id
                    run.metadata = {**run.metadata, **response.metadata}
                    run.transition(RunStatus.WAITING_FOR_MEASUREMENTS, reason="awaiting delayed measurements")
                    self.repository.save_run(run)
                    self._emit("run.waiting", exp, run.run_id)
                return
            except _NoRetry as exc:
                self._fail(run, str(exc), retried=False)
                return
            except TimeoutError:
                last_error = f"timed out after {policy.run_timeout_seconds}s"
                try:
                    await adapter.cancel(run.run_id)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001 - adapter failures must not crash the engine
                last_error = f"{type(exc).__name__}: {exc}"
            if run.attempts >= max_attempts:
                self._fail(run, last_error, retried=run.attempts > 1)
                return
            run.history.append(
                {"event": "retry", "attempt": run.attempts, "error": last_error, "at": datetime.now(UTC).isoformat()}
            )
            run.attempts += 1
            self.repository.save_run(run)
            self._emit("run.retry", exp, run.run_id, {"attempt": run.attempts, "error": last_error})
            if policy.retry_delay_seconds:
                await self._sleep(policy.retry_delay_seconds)

    async def _call_adapter(self, name: str, adapter: ExperimentAdapter, ctx: ExecutionContext):
        hook = getattr(adapter, "execute_run", None)
        if hook is not None:
            return await hook(ctx)
        lock = self._adapter_locks.setdefault(name, asyncio.Lock())
        async with lock:  # prepare()+execute() share adapter state: serialise them
            await adapter.prepare(ctx)
            return await adapter.execute(dict(ctx.configuration))

    # --------------------------------------------------------- finalisation
    def _finalize(
        self, spec: ExperimentSpec, run: Run, measurements: list[Measurement], meta: dict[str, Any] | None = None
    ) -> None:
        """Assess, process and persist measurements, then complete (or fail) the run in ONE transaction."""
        eng = MeasurementEngine(spec.measurement_definitions, derived_metrics=spec.derived_metrics, plugins=self.plugins)
        existing = {m.measurement_id for m in run.measurements}
        incoming = []
        for m in measurements:  # re-delivery of the same measurement (same id) is idempotent
            if m.measurement_id not in existing:
                existing.add(m.measurement_id)
                incoming.append(m)
        valid, assessed, warnings = eng.assess(run.measurements + incoming, run.run_id)
        # Idempotent re-assessment: raw `value` is kept (unit conversion keeps raw_value) -> start from assessed copies
        run.measurements = assessed
        run.processed_measurements = eng.process(assessed, run.run_id)
        report = eng.quality_report(assessed)
        outcomes = run_outcomes(run)
        cons = evaluate_constraints(spec, run.configuration, outcomes, kinds=("output",), plugins=self.plugins)
        run.metadata = {
            **run.metadata,
            **(meta or {}),
            "quality": report,
            "quality_warnings": warnings,
            "output_constraints": cons,
            "feasible": is_feasible(cons),
        }
        if not valid:
            self._fail(run, "no valid measurements were received", measurements_already_set=True)
            return
        if self.strict_required_metrics and report["missing_metrics"]:
            self._fail(run, f"missing required metrics: {report['missing_metrics']}", measurements_already_set=True)
            return
        prev_status, prev_completed = run.status, run.completed_at
        run.error = None
        run.completed_at = datetime.now(UTC)
        run.transition(RunStatus.COMPLETED, reason="measurements committed")
        try:
            self.repository.save_run(run)  # run row + raw + processed measurements: one transaction
        except Exception:
            run.history.pop()  # commit failed: the run must NOT look completed
            run.status, run.completed_at = prev_status, prev_completed
            raise
        self.repository.finish_jobs_for_run(run.run_id, "done")
        self._emit("run.completed", spec.experiment_id, run.run_id, {"feasible": run.metadata["feasible"]})

    def _fail(self, run: Run, error: str, *, retried: bool = False, measurements_already_set: bool = False) -> None:
        run.error = error
        run.completed_at = datetime.now(UTC)
        if run.status in (RunStatus.RUNNING, RunStatus.WAITING_FOR_MEASUREMENTS, RunStatus.QUEUED):
            run.transition(RunStatus.FAILED, reason=error)
        self.repository.save_run(run)
        self.repository.finish_jobs_for_run(run.run_id, "failed")
        self._emit("run.failed", run.experiment_id, run.run_id, {"error": error})

    def _finish_cancelled(self, run: Run, reason: str) -> None:
        if run.status not in _TERMINAL_RUN:
            run.transition(RunStatus.CANCELLED, reason=reason)
            run.completed_at = datetime.now(UTC)
            self.repository.save_run(run)
            self.repository.finish_jobs_for_run(run.run_id, "failed")
            self._emit("run.cancelled", run.experiment_id, run.run_id, {"reason": reason})

    # ================================================ delayed measurements
    async def collect_pending(self, experiment_id: str) -> ExperimentResult:
        """Ask the adapter for measurements of runs in WAITING_FOR_MEASUREMENTS."""
        spec = self.get_spec(experiment_id)
        name, adapter = self.registry.select(spec)
        await self.registry.start(name)
        for run in self.repository.list_runs(experiment_id, RunStatus.WAITING_FOR_MEASUREMENTS):
            try:
                response = await adapter.collect_result(run.run_id)
            except Exception as exc:  # noqa: BLE001
                self._fail(run, f"collection failed: {type(exc).__name__}: {exc}")
                continue
            if response.completed:
                self._finalize(spec, run, response.measurements, response.metadata)
        return self._refresh_status(experiment_id)

    async def ingest_measurements(self, experiment_id: str, run_id: str, measurements: list[Measurement]) -> Run:
        """Attach externally obtained measurements (CSV import, manual entry) and complete the run.

        Allowed for runs that are not yet terminal. Completed/failed/cancelled runs are immutable so that
        analyses stay reproducible; duplicate measurement ids are ignored (idempotent).
        """
        spec = self.get_spec(experiment_id)
        run = self.repository.get_run(run_id)
        if run.experiment_id != experiment_id:
            raise ExecutionError("run does not belong to this experiment")
        if run.status in _TERMINAL_RUN:
            raise ExecutionError(f"run {run_id} is {run.status.value}; its data can no longer change")
        if run.status in (RunStatus.CREATED, RunStatus.QUEUED):
            if run.status == RunStatus.CREATED:
                run.transition(RunStatus.QUEUED, reason="external data")
            run.transition(RunStatus.RUNNING, reason="external measurements ingested")
            run.attempts += 1
        self._finalize(spec, run, measurements, {"ingested": True})
        self._refresh_status(experiment_id)
        return self.repository.get_run(run_id)

    # ================================================== cancel / retry / recover
    async def cancel(self, experiment_id: str, run_id: str | None = None) -> ExperimentResult:
        """Cancel one run or all non-terminal runs. Idempotent."""
        spec = self.get_spec(experiment_id)
        if run_id is None:
            self._cancel_experiments.add(experiment_id)
        else:
            self._cancel_runs.add(run_id)
        adapter = None
        try:
            _, adapter = self.registry.select(spec)
        except AdapterError:
            pass
        runs = [self.repository.get_run(run_id)] if run_id else self.repository.list_runs(experiment_id)
        for run in runs:
            if run.status in _TERMINAL_RUN:
                continue
            if run.status in (RunStatus.RUNNING, RunStatus.WAITING_FOR_MEASUREMENTS) and adapter is not None:
                if adapter.capabilities.cancellation:
                    try:
                        await adapter.cancel(run.run_id)
                    except Exception as exc:  # noqa: BLE001
                        run.metadata = {**run.metadata, "cancel_warning": str(exc)}
                else:
                    run.metadata = {**run.metadata, "cancel_warning": "adapter cannot cancel in-flight work"}
            self._finish_cancelled(run, "cancelled by user")
        return self._refresh_status(experiment_id)

    def retry_run(self, experiment_id: str, run_id: str) -> Run:
        """Explicitly re-queue a FAILED run (a failed run never retries implicitly)."""
        run = self.repository.get_run(run_id)
        if run.experiment_id != experiment_id:
            raise ExecutionError("run does not belong to this experiment")
        if run.status != RunStatus.FAILED:
            raise InvalidTransitionError(f"only failed runs can be retried; run is {run.status.value}")
        run.transition(RunStatus.QUEUED, reason="explicit retry")
        run.error = None
        run.completed_at = None
        self.repository.save_run(run)
        self._cancel_runs.discard(run_id)
        self._advance_experiment(experiment_id, ExperimentStatus.QUEUED, "run retried")
        return run

    async def recover(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        """Resolve runs interrupted by a restart according to adapter capabilities and policy.

        * ``resume``: adapter is queried (``status``/``collect_result``) with the stored handle.
        * ``retry``: run is re-queued (only if the adapter is retryable).
        * ``fail``: run is marked FAILED.
        * ``auto`` (default): resumable -> resume; idempotent -> retry; otherwise FAILED (never silently retried
          or completed; use :meth:`retry_run` explicitly).
        Returns a list of actions taken.
        """
        actions: list[dict[str, Any]] = []
        self.repository.release_claimed(experiment_id)
        ids = [experiment_id] if experiment_id else self.repository.list_ids()
        for exp in ids:
            spec = self.get_spec(exp)
            try:
                name, adapter = self.registry.select(spec)
            except AdapterError:
                name, adapter = None, None
            caps = adapter.capabilities if adapter else None
            for run in self.repository.list_runs(exp):
                if run.status not in (RunStatus.RUNNING, RunStatus.WAITING_FOR_MEASUREMENTS):
                    continue
                policy = spec.execution.recovery
                action = "fail"
                reason = "interrupted"
                if caps is None:
                    reason = "adapter not registered; cannot recover"
                elif run.status == RunStatus.WAITING_FOR_MEASUREMENTS and not caps.resumable:
                    reason = "adapter cannot resume delayed work after restart"
                else:
                    if policy == "auto":
                        action = "resume" if caps.resumable else "retry" if caps.idempotent else "fail"
                    elif policy == "retry":
                        action = "retry" if caps.retryable else "fail"
                    elif policy == "resume":
                        action = "resume" if caps.resumable else "fail"
                    if action == "fail":
                        reason = (
                            "recovery policy is 'fail'"
                            if policy == "fail"
                            else "adapter does not allow safe automatic recovery; retry explicitly with retry_run()"
                        )
                if action == "resume":
                    action, reason = await self._resume(spec, name, adapter, run)
                elif action == "retry":
                    run.transition(RunStatus.QUEUED, reason="recovery: retry after restart")
                    self.repository.save_run(run)
                    self.repository.enqueue(exp, run.run_id)
                    reason = "re-queued"
                if action == "fail":
                    self._fail(run, f"interrupted by restart: {reason}")
                actions.append({"experiment_id": exp, "run_id": run.run_id, "action": action, "reason": reason})
            self._refresh_status(exp)
        return actions

    async def _resume(self, spec, name, adapter, run: Run) -> tuple[str, str]:
        await self.registry.start(name)
        state = None
        status_hook = getattr(adapter, "status", None)
        if status_hook is not None and run.adapter_handle:
            try:
                state = (await status_hook(run.adapter_handle)).state
            except Exception:  # noqa: BLE001
                state = "unknown"
        try:
            if state in (None, "completed", "unknown"):
                resp = await adapter.collect_result(run.run_id)
                if resp.completed:
                    self._finalize(spec, run, resp.measurements, resp.metadata)
                    return "resume", "measurements collected"
        except Exception as exc:  # noqa: BLE001
            return "fail", f"resume failed: {exc}"
        if state in ("pending", "running", None):
            if run.status == RunStatus.RUNNING:
                run.transition(RunStatus.WAITING_FOR_MEASUREMENTS, reason="recovery: still running at adapter")
                self.repository.save_run(run)
            return "resume", "still running; waiting for measurements"
        return "fail", f"adapter reports state '{state}'"

    # ==================================================== analysis / optimise
    def analyse(self, result: ExperimentResult, **kwargs: Any) -> AnalysisResult:
        """Analyse a result; the analysis is persisted for reproducibility and history."""
        analysis = self.analysis_engine.analyse(result, **kwargs)
        if self.repository.has_experiment(result.spec.experiment_id):
            self.repository.save_analysis(analysis)
        return analysis

    def optimise(
        self,
        spec: ExperimentSpec,
        result: ExperimentResult | AnalysisResult | None = None,
        *,
        count: int = 1,
        method: str = "sequential",
    ) -> list[Recommendation]:
        """Recommend next configurations using stored results (loaded from the repository when ``result`` is omitted)."""
        if result is None or isinstance(result, AnalysisResult):
            result = self.get(spec.experiment_id)
        recs = self.optimizer.suggest(spec, result, count=count, method=method)
        if self.repository.has_experiment(spec.experiment_id):
            self.repository.save_recommendations(spec.experiment_id, recs)
        return recs

    def add_runs(
        self, experiment_id: str, configurations: list[dict[str, Any]], *, iteration: int = 1, source: str = "optimizer"
    ) -> list[Run]:
        """Append sequential runs (validated, budget-checked, persisted as QUEUED)."""
        spec = self.get_spec(experiment_id)
        result = self.get(experiment_id)
        plan = self.designer.sequential(spec, result, configurations, iteration=iteration, source=source)
        for r in plan.runs:
            r.idempotency_key = f"{experiment_id}:{r.run_id}"
        stored = self.repository.get_plan(experiment_id)
        stored.runs.extend(plan.runs)
        stored.design_info.setdefault("sequential_extensions", []).append(
            {"iteration": iteration, "source": source, "run_ids": [r.run_id for r in plan.runs]}
        )
        self.repository.save_plan(stored)
        self.repository.save_runs(plan.runs)
        self._advance_experiment(experiment_id, ExperimentStatus.QUEUED, f"{len(plan.runs)} sequential run(s) added")
        return plan.runs

    async def run_sequential(
        self,
        experiment_id: str,
        *,
        iterations: int = 5,
        batch_size: int = 1,
        method: str = "bayesian",
        criteria: StoppingCriteria | None = None,
    ) -> tuple[ExperimentResult, StopDecision]:
        """Optimise loop: stop-check -> suggest -> add runs -> execute -> repeat. Returns final result and stop reason."""
        spec = self.get_spec(experiment_id)
        criteria = criteria or StoppingCriteria()
        decision = StopDecision(stop=False, reason="continue")
        for it in range(1, iterations + 1):
            result = self.get(experiment_id)
            decision = self.optimizer.should_stop(spec, result, criteria, iteration=it - 1)
            if decision.stop:
                break
            recs = self.optimise(spec, result, count=batch_size, method=method)
            if not recs:
                decision = StopDecision(
                    stop=True,
                    reason="no_feasible_candidate" if len(result.runs) < spec.run_budget else "budget_exhausted",
                    detail="optimizer returned no candidates",
                )
                break
            self.add_runs(experiment_id, [r.configuration for r in recs], iteration=it, source=f"optimizer:{method}")
            await self.run_pending(experiment_id)
        else:
            decision = self.optimizer.should_stop(spec, self.get(experiment_id), criteria, iteration=iterations)
            if not decision.stop:
                decision = StopDecision(stop=True, reason="max_iterations", detail=f"{iterations} iterations completed")
        self.last_stop[experiment_id] = decision
        return self.get(experiment_id), decision

    # ================================================================ reports
    def report(self, result: ExperimentResult, analysis: AnalysisResult | None = None) -> ExperimentReport:
        """Build (and persist) a versioned report."""
        analysis = analysis or self.analysis_engine.analyse(result)
        recs = (
            self.repository.list_recommendations(result.spec.experiment_id)
            if self.repository.has_experiment(result.spec.experiment_id)
            else []
        )
        report = self.report_engine.build(result, analysis, recommendations=recs, adapters=self.registry.list())
        if self.repository.has_experiment(result.spec.experiment_id):
            self.repository.save_report(report)
        return report

    def history(self, experiment_id: str):
        """Read-only history view for a stored experiment."""
        from trialforge.history import ExperimentHistory

        return ExperimentHistory(self.repository, experiment_id)

    # ============================================================ retrieval
    def get(self, experiment_id: str) -> ExperimentResult:
        """Load the current persisted ExperimentResult (with the attached spec object if one was provided)."""
        try:
            result = self.repository.get(experiment_id)
        except KeyError as exc:
            raise KeyError(experiment_id) from exc
        if experiment_id in self._spec_overrides:
            result.spec = self._spec_overrides[experiment_id]
        return result

    def list_experiments(self) -> list[str]:
        """IDs of all persisted experiments."""
        return self.repository.list_ids()

    # ======================================================= status tracking
    @staticmethod
    def _experiment_status(runs: list[Run]) -> ExperimentStatus:
        s = {r.status for r in runs}
        if not s:
            return ExperimentStatus.DESIGNED
        if RunStatus.RUNNING in s:
            return ExperimentStatus.RUNNING
        if RunStatus.WAITING_FOR_MEASUREMENTS in s:
            return ExperimentStatus.WAITING_FOR_MEASUREMENTS
        if s & {RunStatus.QUEUED, RunStatus.CREATED}:
            return ExperimentStatus.QUEUED
        if s == {RunStatus.CANCELLED}:
            return ExperimentStatus.CANCELLED
        if RunStatus.FAILED in s:
            return ExperimentStatus.FAILED
        if RunStatus.CANCELLED in s:
            return ExperimentStatus.CANCELLED
        return ExperimentStatus.COMPLETED

    def _refresh_status(self, experiment_id: str) -> ExperimentResult:
        runs = self.repository.list_runs(experiment_id)
        self._advance_experiment(experiment_id, self._experiment_status(runs), "derived from run states")
        return self.get(experiment_id)

    def _advance_experiment(self, experiment_id: str, target: ExperimentStatus, reason: str = "") -> None:
        """Move the experiment to ``target`` through *legal* intermediate states only."""
        current = self.repository.get_status(experiment_id)
        if current == target:
            return
        prev: dict[ExperimentStatus, ExperimentStatus | None] = {current: None}
        q = deque([current])
        while q:
            node = q.popleft()
            if node == target:
                break
            for nxt in EXPERIMENT_TRANSITIONS[node]:
                if nxt not in prev:
                    prev[nxt] = node
                    q.append(nxt)
        if target not in prev:
            raise InvalidTransitionError(f"experiment {experiment_id}: {current.value} -> {target.value} is not allowed")
        path = []
        node: ExperimentStatus | None = target
        while node is not None and node != current:
            path.append(node)
            node = prev[node]
        for step in reversed(path):
            self.repository.set_status(experiment_id, step, reason)
            self._emit("experiment.status", experiment_id, data={"status": step.value})

    def _emit(self, name: str, experiment_id: str, run_id: str | None = None, data: dict[str, Any] | None = None) -> None:
        self.events.publish(Event(name=name, experiment_id=experiment_id, run_id=run_id, data=data or {}))
