"""The application's SDK adapter: simulates a short stint per run and returns per-lap measurements."""

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

from f1lab.adapters.vehicle_model import FACTORS, METRIC_UNITS, rng_for, run_level, stint

SOURCE = "f1lab-synthetic-simulator v1"
T0 = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)  # fixed reference clock => reproducible timestamps


class F1SimulatorAdapter:
    """Concurrency-safe, idempotent synthetic simulator. One instance per experiment (settings differ per experiment)."""

    capabilities = AdapterCapabilities(
        immediate_execution=True, concurrent=True, max_concurrency=4, retryable=True, idempotent=True, resumable=False
    )

    def __init__(self, name: str, *, latency_seconds: float = 0.0, fault_rate: float = 0.0, stint_laps: int = 5,
                 outlier_probability: float = 0.03) -> None:  # fmt: skip
        self.metadata = AdapterMetadata(
            name=name, version="1.0", domain="motorsport setup", synthetic=True,
            description="SYNTHETIC vehicle setup simulator (invented response surface). Not real telemetry.",
            input_schema={n: {"type": "continuous", "min": f["low"], "max": f["high"], "required": False}
                          for n, f in FACTORS.items()},
            tags=("f1lab", "synthetic"),
        )  # fmt: skip
        self.latency, self.fault_rate = latency_seconds, fault_rate
        self.stint_laps, self.outlier_probability = stint_laps, outlier_probability
        self._prepared: ExecutionContext | None = None

    async def validate(self, config: dict[str, Any]) -> None:
        for name, value in config.items():
            if name not in FACTORS:
                raise AdapterError(f"unknown setup parameter '{name}'")
            f = FACTORS[name]
            if not (f["low"] <= float(value) <= f["high"]):
                raise AdapterError(f"{name}={value} outside the simulator range [{f['low']}, {f['high']}]")

    async def prepare(self, context: ExecutionContext) -> None:
        self._prepared = context

    async def available(self) -> bool:
        return True

    def _faulty(self, run_id: str, attempt: int) -> bool:
        """Optional demo fault injection: the first attempt of a deterministic subset of runs fails."""
        return attempt == 1 and self.fault_rate > 0 and rng_for(run_id, "fault").random() < self.fault_rate

    def _measure(self, run_id: str, cfg: dict[str, Any], requested: list[str] | None) -> list[Measurement]:
        def want(metric: str) -> bool:
            return not requested or metric in requested

        out: list[Measurement] = []
        for rec in stint(cfg, run_id, self.stint_laps, self.outlier_probability):
            ts = T0 + timedelta(seconds=90 * rec["lap"])
            for metric in ("sector_1", "sector_2", "sector_3", "lap_time"):
                if want(metric):
                    out.append(Measurement(run_id=run_id, metric=metric, value=round(rec[metric], 4), unit="seconds",
                                           timestamp=ts, source=SOURCE, metadata={"synthetic": True, "lap": rec["lap"]}))  # fmt: skip
        for metric, value in run_level(cfg, run_id).items():
            if want(metric):
                out.append(Measurement(run_id=run_id, metric=metric, value=round(value, 4), unit=METRIC_UNITS[metric],
                                       timestamp=T0, source=SOURCE, metadata={"synthetic": True}))  # fmt: skip
        return out

    async def execute_run(self, ctx: ExecutionContext) -> ExecutionResponse:
        if self.latency:
            await asyncio.sleep(self.latency)
        if self._faulty(ctx.run_id, ctx.attempt):
            raise RuntimeError("simulator connection dropped (injected fault)")
        return ExecutionResponse(completed=True, metadata={"synthetic": True, "simulator": SOURCE},
                                 measurements=self._measure(ctx.run_id, ctx.configuration, ctx.metadata.get("requested_metrics")))  # fmt: skip

    async def execute(self, config: dict[str, Any]) -> ExecutionResponse:
        rid = self._prepared.run_id if self._prepared else "unbound"
        return ExecutionResponse(completed=True, measurements=self._measure(rid, config, None),
                                 metadata={"synthetic": True, "simulator": SOURCE})  # fmt: skip

    async def collect_result(self, run_id: str) -> MeasurementResponse:
        raise AdapterError("this adapter completes immediately; nothing to collect")

    async def status(self, handle: str) -> ExecutionStatusResponse:
        return ExecutionStatusResponse(state="unknown", detail="immediate adapter keeps no handles")

    async def cancel(self, run_id: str) -> None:
        return None
