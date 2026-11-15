"""Coffee-roasting adapter: a deterministic SYNTHETIC simulation proving third-party extensibility.

Imports only the public ``trialforge`` SDK. Nothing here is real roasting data; the response surface is
an invented but internally consistent model (hotter/longer roast -> darker colour, lower moisture; quality
peaks at a target colour and moisture).

Factors  : temperature (degC, 195-215), duration (min, 6-14), airflow (%, 0-100)
Metrics  : moisture (percent), colour (index; lower = darker), quality (score 0-100)
"""

from __future__ import annotations

import hashlib
import random
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

SOURCE = "synthetic-coffee-roast-simulator v1"


def _rng(*parts: Any) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def roast_model(temperature: float, duration: float, airflow: float) -> dict[str, float]:
    """Noise-free response surface (exposed so tests can verify against analytic values)."""
    development = (temperature - 180.0) / 20.0 * duration / 10.0
    moisture = max(0.8, 11.0 - 4.2 * development + 0.012 * (airflow - 40.0))
    colour = 85.0 - 28.0 * development
    quality = 100.0 - 0.08 * (colour - 58.0) ** 2 - 4.0 * (moisture - 2.6) ** 2 - 0.01 * (airflow - 42.0) ** 2
    return {"moisture": moisture, "colour": colour, "quality": max(0.0, min(100.0, quality))}


class CoffeeRoastAdapter:
    """Synthetic roaster. Concurrency-safe and idempotent: results depend only on (run id, configuration)."""

    metadata = AdapterMetadata(
        name="coffee-roast",
        version="1.0",
        domain="coffee roasting",
        synthetic=True,
        description="Deterministic synthetic roaster for demonstrating TRIALFORGE in an unrelated domain.",
        input_schema={
            "temperature": {"type": "continuous", "min": 150.0, "max": 260.0},
            "duration": {"type": "continuous", "min": 1.0, "max": 30.0},
            "airflow": {"type": "continuous", "min": 0.0, "max": 100.0},
        },
        tags=("example", "synthetic"),
    )
    capabilities = AdapterCapabilities(
        immediate_execution=True, concurrent=True, max_concurrency=8, retryable=True, idempotent=True, resumable=False
    )

    def __init__(self, noise_sd: float = 0.15) -> None:
        self.noise_sd = noise_sd
        self._prepared: ExecutionContext | None = None

    async def validate(self, config: dict[str, Any]) -> None:
        for name in ("temperature", "duration", "airflow"):
            if name not in config:
                raise AdapterError(f"missing factor '{name}'")

    async def prepare(self, context: ExecutionContext) -> None:
        self._prepared = context

    async def available(self) -> bool:
        return True

    def _measure(self, run_id: str, cfg: dict[str, Any], requested: list[str] | None) -> list[Measurement]:
        truth = roast_model(float(cfg["temperature"]), float(cfg["duration"]), float(cfg["airflow"]))
        rng = _rng(run_id, sorted(cfg.items()))
        units = {"moisture": "percent", "colour": "index", "quality": "score"}
        out = []
        for metric, value in truth.items():
            noisy = value + rng.gauss(0, self.noise_sd * (4.0 if metric == "quality" else 1.0))
            if requested and metric not in requested:
                continue
            out.append(
                Measurement(
                    run_id=run_id,
                    metric=metric,
                    value=round(noisy, 4),
                    unit=units[metric],
                    source=SOURCE,
                    metadata={"synthetic": True},
                )
            )
        return out

    async def execute_run(self, context: ExecutionContext) -> ExecutionResponse:
        ms = self._measure(context.run_id, context.configuration, context.metadata.get("requested_metrics"))
        return ExecutionResponse(completed=True, measurements=ms, metadata={"synthetic": True, "simulator": SOURCE})

    async def execute(self, config: dict[str, Any]) -> ExecutionResponse:
        run_id = self._prepared.run_id if self._prepared else "unbound"
        return ExecutionResponse(
            completed=True, measurements=self._measure(run_id, config, None), metadata={"synthetic": True, "simulator": SOURCE}
        )

    async def collect_result(self, run_id: str) -> MeasurementResponse:
        raise AdapterError("immediate adapter has nothing to collect")

    async def status(self, handle: str) -> ExecutionStatusResponse:
        return ExecutionStatusResponse(state="unknown", detail="immediate adapter keeps no handles")

    async def cancel(self, run_id: str) -> None:
        return None
