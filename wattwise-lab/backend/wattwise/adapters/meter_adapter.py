"""The application's SDK adapter: simulates a smart-meter day for a candidate schedule."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from trialforge import (
    AdapterCapabilities,
    AdapterError,
    AdapterMetadata,
    ExecutionContext,
    ExecutionResponse,
    ExecutionStatusResponse,
    Measurement,
    MeasurementResponse,
)

from wattwise.adapters.household_model import FACTORS, METRIC_UNITS, STEP_H, rng_for, simulate

SOURCE = "wattwise-simulated-meter v1"
T0 = datetime(2026, 1, 12, 16, 0, tzinfo=UTC)  # reference calendar day (simulated)


class MeterSimulatorAdapter:
    """Concurrency-safe, idempotent household simulator. One instance per experiment (tariff, EV and appliances differ per experiment)."""

    capabilities = AdapterCapabilities(
        immediate_execution=True, time_series=True, concurrent=True, max_concurrency=4, retryable=True, idempotent=True, resumable=False
    )

    def __init__(self, name: str, household: dict[str, Any], *, latency_seconds: float = 0.0, fault_rate: float = 0.0) -> None:
        self.household = household
        self.metadata = AdapterMetadata(
            name=name, version="1.0", domain="household electricity", synthetic=True,
            description="SIMULATED smart-meter day for an EV charging + appliance schedule. No real meter, vehicle or tariff API.",
            input_schema={n: {"type": "continuous", "min": f["low"], "max": f["high"], "required": False} for n, f in FACTORS.items()},
            tags=("wattwise", "simulated"),
        )  # fmt: skip
        self.latency, self.fault_rate = latency_seconds, fault_rate
        self._prepared: ExecutionContext | None = None

    async def validate(self, config: dict[str, Any]) -> None:
        for name, value in config.items():
            if name not in FACTORS:
                raise AdapterError(f"unknown schedule parameter '{name}'")
            f = FACTORS[name]
            if not (f["low"] <= float(value) <= f["high"]):
                raise AdapterError(f"{name}={value} outside the supported range [{f['low']}, {f['high']}]")

    async def prepare(self, context: ExecutionContext) -> None:
        self._prepared = context

    async def available(self) -> bool:
        return True

    def _measure(self, run_id: str, cfg: dict[str, Any], requested: list[str] | None) -> list[Measurement]:
        sim = simulate(cfg, self.household, rng_for(run_id, "meter"))
        m = sim["metrics"]
        meta = {"synthetic": True, "reading_kind": "simulated"}
        out = [Measurement(run_id=run_id, metric=k, value=round(float(m[k]), 5), unit=METRIC_UNITS[k], timestamp=T0, source=SOURCE, metadata=dict(meta))
               for k in ("energy_cost", "energy_kwh", "peak_power_kw", "departure_charge_pct", "ev_energy_kwh") if not requested or k in requested]  # fmt: skip
        if requested and "grid_power" in requested:
            for i, st in enumerate(sim["steps"]):
                out.append(Measurement(run_id=run_id, metric="grid_power", value=round(st["kw"], 4), unit="kW", timestamp=T0 + timedelta(hours=i * STEP_H),
                                       source=SOURCE, channel="grid", metadata={**meta, "sample_index": i}))  # fmt: skip
        return out

    def _faulty(self, run_id: str, attempt: int) -> bool:
        return attempt == 1 and self.fault_rate > 0 and rng_for(run_id, "fault").random() < self.fault_rate

    async def execute_run(self, ctx: ExecutionContext) -> ExecutionResponse:
        if self.latency:
            await asyncio.sleep(self.latency)
        if self._faulty(ctx.run_id, ctx.attempt):
            raise RuntimeError("meter gateway timeout (injected fault)")
        return ExecutionResponse(completed=True, metadata={"synthetic": True, "simulator": SOURCE},
                                 measurements=self._measure(ctx.run_id, ctx.configuration, ctx.metadata.get("requested_metrics")))  # fmt: skip

    async def execute(self, config: dict[str, Any]) -> ExecutionResponse:
        rid = self._prepared.run_id if self._prepared else "unbound"
        return ExecutionResponse(completed=True, measurements=self._measure(rid, config, None), metadata={"synthetic": True, "simulator": SOURCE})

    async def collect_result(self, run_id: str) -> MeasurementResponse:
        raise AdapterError("this adapter completes immediately; nothing to collect")

    async def status(self, handle: str) -> ExecutionStatusResponse:
        return ExecutionStatusResponse(state="unknown", detail="immediate adapter keeps no handles")

    async def cancel(self, run_id: str) -> None:
        return None
