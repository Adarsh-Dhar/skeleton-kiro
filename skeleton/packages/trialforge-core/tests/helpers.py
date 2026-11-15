"""Test adapters with scriptable behaviour (public SDK only)."""

from __future__ import annotations

import asyncio

from trialforge import (
    AdapterCapabilities,
    AdapterError,
    AdapterMetadata,
    ExecutionContext,
    ExecutionResponse,
    ExecutionStatusResponse,
    ExperimentSpec,
    Factor,
    Measurement,
    MeasurementDefinition,
    MeasurementResponse,
    Objective,
)


def basic_spec(n_levels=3, **kw) -> ExperimentSpec:
    kw.setdefault("run_budget", 50)
    return ExperimentSpec(
        name="t",
        factors=[Factor(name="x", factor_type="discrete", values=list(range(n_levels)))],
        objectives=[Objective(metric="y", direction="minimize")],
        measurement_definitions=[MeasurementDefinition(metric="y", unit="u")],
        **kw,
    )


class ScriptedAdapter:
    """y = 10 + x. Behaviour knobs: fail_first (per run), delay, delayed, hang, in-flight tracking."""

    def __init__(
        self,
        name="scripted",
        *,
        caps: AdapterCapabilities | None = None,
        fail_first=0,
        delay=0.0,
        delayed=False,
        hang=False,
        always_fail=False,
        validate_error=False,
        metrics=("y",),
    ):
        self.metadata = AdapterMetadata(name)
        self.capabilities = caps or AdapterCapabilities(concurrent=True, max_concurrency=8, retryable=True)
        self.fail_first, self.delay, self.delayed, self.hang, self.always_fail = fail_first, delay, delayed, hang, always_fail
        self.validate_error, self.metrics = validate_error, metrics
        self.calls: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.ready: set[str] = set()
        self.collected: list[str] = []
        self.completed_remote: dict[str, float] = {}
        self._prepared: ExecutionContext | None = None

    async def validate(self, config):
        if self.validate_error:
            raise AdapterError("bad config per adapter")

    async def prepare(self, context):
        self._prepared = context
        await asyncio.sleep(0)  # yield: exposes races if calls were not serialised

    def _ms(self, run_id, cfg):
        return [Measurement(run_id=run_id, metric=m, value=10.0 + cfg["x"], unit="u") for m in self.metrics]

    async def execute_run(self, ctx: ExecutionContext) -> ExecutionResponse:
        self.calls[ctx.run_id] = self.calls.get(ctx.run_id, 0) + 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hang:
                await asyncio.sleep(3600)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.always_fail or self.calls[ctx.run_id] <= self.fail_first:
                raise RuntimeError(f"transient failure #{self.calls[ctx.run_id]}")
            if self.delayed:
                return ExecutionResponse(completed=False, handle=f"h-{ctx.run_id}", metadata={"cfg": ctx.configuration})
            return ExecutionResponse(completed=True, measurements=self._ms(ctx.run_id, ctx.configuration))
        finally:
            self.in_flight -= 1

    async def execute(self, config):  # legacy path (used when execute_run deleted)
        rid = self._prepared.run_id
        self.calls[rid] = self.calls.get(rid, 0) + 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return ExecutionResponse(completed=True, measurements=self._ms("unused", config))
        finally:
            self.in_flight -= 1

    async def collect_result(self, run_id):
        self.collected.append(run_id)
        if run_id in self.ready:
            return MeasurementResponse(
                completed=True, measurements=[Measurement(run_id=run_id, metric="y", value=42.0, unit="u")]
            )
        return MeasurementResponse(completed=False)

    async def status(self, handle):
        return ExecutionStatusResponse(state="completed" if handle[2:] in self.ready else "running")

    async def cancel(self, run_id):
        self.cancelled.append(run_id)


class LegacyAdapter:
    """Implements ONLY the five-method protocol (no execute_run). prepare() stores per-call state, so
    interleaved calls would attribute results to the wrong run unless the engine serialises them."""

    metadata = AdapterMetadata("legacy")
    capabilities = AdapterCapabilities(concurrent=True, max_concurrency=8)

    def __init__(self):
        self._prepared = None
        self.in_flight = 0
        self.max_in_flight = 0

    async def validate(self, config):
        return None

    async def prepare(self, context):
        self._prepared = context
        await asyncio.sleep(0.005)  # a yield point inside the prepare->execute window

    async def execute(self, config):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.005)
        ctx_cfg = self._prepared.configuration  # would be another run's config if interleaved
        self.in_flight -= 1
        return ExecutionResponse(
            completed=True, measurements=[Measurement(run_id="unused", metric="y", value=10.0 + ctx_cfg["x"], unit="u")]
        )

    async def collect_result(self, run_id):
        raise AdapterError("n/a")

    async def cancel(self, run_id):
        return None
