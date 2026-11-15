"""F1 SETUP LAB adapter built on reproducible SYNTHETIC vehicle-performance data.

This is NOT connected to any Formula 1 team, simulator licence or live telemetry feed. All values come
from an invented smooth response surface (aero balance, suspension stiffness, drag) plus seeded noise, and every
measurement is tagged ``metadata['synthetic'] = True``. Recorded telemetry (e.g. exported from a simulator) can
be imported with :func:`measurements_from_telemetry_csv`; imported rows are tagged ``synthetic = False`` and
``source = 'telemetry-import'``.

Factors : front_wing (deg 0-50), rear_wing (deg 0-50), suspension (stiffness 1-11), optionally tyre_pressure_psi
Metrics : lap_time (s), sector_1/2/3 (s), stability (score 0-100), top_speed (km/h), tyre_degradation (percent)
"""

from __future__ import annotations

import csv
import hashlib
import io
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

SOURCE = "synthetic-f1-setup-simulator v1"
UNITS = {
    "lap_time": "seconds",
    "sector_1": "seconds",
    "sector_2": "seconds",
    "sector_3": "seconds",
    "stability": "score",
    "top_speed": "km/h",
    "tyre_degradation": "percent",
}
RANGES = {"front_wing": (0.0, 50.0), "rear_wing": (0.0, 50.0), "suspension": (1.0, 11.0), "tyre_pressure_psi": (18.0, 26.0)}


def _rng(*parts: Any) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def vehicle_model(setup: dict[str, Any]) -> dict[str, float]:
    """Noise-free synthetic performance. Sector 1 is drag-dominated, sector 2 downforce-dominated."""
    fw, rw = float(setup["front_wing"]), float(setup["rear_wing"])
    sus = float(setup["suspension"])
    tp = float(setup.get("tyre_pressure_psi", 22.0))
    balance = (rw - fw) - 4.0  # ideal: rear slightly more loaded than front
    drag = 0.012 * (fw + rw)
    s1 = 24.0 + 0.9 * drag + 0.0008 * (sus - 6) ** 2
    s2 = 33.0 - 0.020 * (fw + rw) + 0.0009 * (fw + rw) ** 2 / 10 + 0.015 * balance**2 / 10 + 0.02 * (sus - 6) ** 2
    s3 = 23.0 + 0.5 * drag - 0.004 * (fw + rw) + 0.004 * (tp - 22.0) ** 2
    stability = 100.0 - 0.08 * balance**2 - 1.2 * (sus - 6.0) ** 2 / 5 - 0.5 * abs(tp - 22.0)
    top_speed = 345.0 - 1.1 * (fw + rw) / 2 - 0.2 * (sus - 6) ** 2
    wear = 8.0 + 0.04 * (sus - 6) ** 2 + 0.18 * (tp - 22.0) ** 2 + 0.03 * (fw + rw)
    return {
        "sector_1": s1,
        "sector_2": s2,
        "sector_3": s3,
        "lap_time": s1 + s2 + s3,
        "stability": max(0.0, min(100.0, stability)),
        "top_speed": top_speed,
        "tyre_degradation": wear,
    }


class SyntheticF1Adapter:
    """Synthetic, concurrency-safe, idempotent vehicle-setup 'simulator'."""

    metadata = AdapterMetadata(
        name="f1-setup-sim",
        version="1.0",
        domain="motorsport setup",
        synthetic=True,
        description="Reproducible SYNTHETIC F1-style setup performance model. Not real telemetry.",
        input_schema={
            n: {"type": "continuous", "min": lo, "max": hi, "required": n != "tyre_pressure_psi"}
            for n, (lo, hi) in RANGES.items()
        },
        tags=("example", "synthetic", "f1"),
    )
    capabilities = AdapterCapabilities(
        immediate_execution=True, concurrent=True, max_concurrency=8, retryable=True, idempotent=True, resumable=False
    )

    def __init__(self, noise_sd: float = 0.04) -> None:
        self.noise_sd = noise_sd
        self._prepared: ExecutionContext | None = None

    async def validate(self, config: dict[str, Any]) -> None:
        for name in ("front_wing", "rear_wing", "suspension"):
            if name not in config:
                raise AdapterError(f"missing setup parameter '{name}'")
        for name, value in config.items():
            if name in RANGES and not (RANGES[name][0] <= float(value) <= RANGES[name][1]):
                raise AdapterError(f"{name}={value} outside supported range {RANGES[name]}")

    async def prepare(self, context: ExecutionContext) -> None:
        self._prepared = context

    async def available(self) -> bool:
        return True

    def _measure(self, run_id: str, cfg: dict[str, Any], requested: list[str] | None) -> list[Measurement]:
        truth = vehicle_model(cfg)
        rng = _rng(run_id, sorted(cfg.items()))
        scale = {"stability": 20.0, "top_speed": 15.0, "tyre_degradation": 4.0}  # sd relative to noise_sd, per metric
        noise = {m: rng.gauss(0, self.noise_sd * scale.get(m, 1.0)) for m in truth}
        for s in ("sector_1", "sector_2", "sector_3"):  # keep lap_time == sum(sectors)
            truth[s] += noise[s]
        truth["lap_time"] = truth["sector_1"] + truth["sector_2"] + truth["sector_3"]
        for m in ("stability", "top_speed", "tyre_degradation"):
            truth[m] += noise[m]
        return [
            Measurement(run_id=run_id, metric=m, value=round(v, 4), unit=UNITS[m], source=SOURCE, metadata={"synthetic": True})
            for m, v in truth.items()
            if not requested or m in requested
        ]

    async def execute_run(self, context: ExecutionContext) -> ExecutionResponse:
        return ExecutionResponse(
            completed=True,
            metadata={"synthetic": True, "simulator": SOURCE},
            measurements=self._measure(context.run_id, context.configuration, context.metadata.get("requested_metrics")),
        )

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


def measurements_from_telemetry_csv(run_id: str, csv_text: str, *, source: str = "telemetry-import") -> list[Measurement]:
    """Import recorded telemetry: columns ``sector_1,sector_2,sector_3`` (seconds) and optional
    ``stability``, ``top_speed``, ``tyre_degradation``. One row per lap; ``lap_time`` is derived as the
    sector sum. Rows are tagged ``synthetic=False`` (recorded) with the supplied ``source``."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows:
        raise AdapterError("telemetry CSV has no rows")
    out: list[Measurement] = []
    for i, row in enumerate(rows):
        try:
            s = [float(row[f"sector_{k}"]) for k in (1, 2, 3)]
        except (KeyError, ValueError, TypeError) as exc:
            raise AdapterError(f"row {i + 2}: sector_1..3 must be numeric ({exc})") from exc
        values = {"sector_1": s[0], "sector_2": s[1], "sector_3": s[2], "lap_time": sum(s)}
        for extra in ("stability", "top_speed", "tyre_degradation"):
            if row.get(extra) not in (None, ""):
                values[extra] = float(row[extra])
        for metric, v in values.items():
            out.append(
                Measurement(
                    run_id=run_id,
                    metric=metric,
                    value=v,
                    unit=UNITS[metric],
                    source=source,
                    metadata={"synthetic": False, "imported": True, "lap": i + 1, "row": i + 2},
                )
            )
    return out
