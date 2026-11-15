"""WATTWISE LAB adapter: household electricity experiments with SIMULATED smart-meter readings.

No paid APIs and no hardware are required. Simulated readings are tagged ``metadata['reading_kind'] =
'simulated'`` / ``synthetic = True``; readings imported from a CSV are tagged ``'imported'`` /
``synthetic = False``.

Scenario (24 h window starting 18:00): household base load + cooling load (depends on setpoint) + an EV
charging session starting at ``charge_start_hour`` at ``charge_power_kw``. The vehicle leaves at ``departure_hour``
(07:00 next day); ``departure_charge_pct`` is the state of charge at that moment.

Factors : charge_start_hour (0-23), charge_power_kw (1-11), cooling_setpoint (degC 18-30)
Metrics : energy_cost (currency), energy_kwh (kWh), peak_power_kw (kW), departure_charge_pct (percent),
          grid_power (kW time series, only when requested)
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
from dataclasses import dataclass, field
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

SOURCE = "synthetic-wattwise-meter v1"
STEP_MIN = 15
WINDOW_START = datetime(2026, 1, 12, 18, 0, tzinfo=UTC)  # reference day (synthetic calendar)


@dataclass(frozen=True)
class Tariff:
    """Time-of-use tariff: ``periods`` = (start_hour, end_hour, price_per_kwh); wraps past midnight if start > end."""

    periods: tuple[tuple[int, int, float], ...] = ((22, 6, 0.12), (6, 17, 0.20), (17, 22, 0.32))
    demand_charge_per_kw: float = 0.0
    currency: str = "currency"

    def price_at(self, hour: float) -> float:
        h = hour % 24
        for start, end, price in self.periods:
            if (start <= h < end) if start < end else (h >= start or h < end):
                return price
        raise ValueError(f"tariff has no period covering hour {hour}")


@dataclass
class MeterSchedule:
    """Household schedule parameters (simulated)."""

    base_load_kw: float = 0.45
    evening_peak_kw: float = 1.4  # cooking/lighting 18:00-21:00
    battery_kwh: float = 60.0
    initial_soc_pct: float = 30.0
    charge_efficiency: float = 0.92
    target_soc_pct: float = 100.0
    departure_hour: float = 7.0  # hours after midnight, next day
    tariff: Tariff = field(default_factory=Tariff)


def _rng(*parts: Any) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def simulate_day(cfg: dict[str, Any], schedule: MeterSchedule, rng: random.Random | None = None) -> dict[str, Any]:
    """Return {'readings': [(timestamp, kW)], 'departure_soc_pct': float} for one configuration (noise optional)."""
    start_h = float(cfg["charge_start_hour"])
    power = float(cfg["charge_power_kw"])
    setpoint = float(cfg["cooling_setpoint"])
    soc_kwh = schedule.battery_kwh * schedule.initial_soc_pct / 100
    cap_kwh = schedule.battery_kwh * schedule.target_soc_pct / 100
    readings: list[tuple[datetime, float]] = []
    soc_at_departure = None
    steps = 24 * 60 // STEP_MIN
    for i in range(steps):
        t = WINDOW_START + timedelta(minutes=i * STEP_MIN)
        hour_abs = 18 + i * STEP_MIN / 60  # hours since midnight of day 0
        hod = hour_abs % 24
        load = schedule.base_load_kw
        if 18 <= hod < 21:
            load += schedule.evening_peak_kw
        load += max(0.0, 26.5 - setpoint) * 0.16 * (1.0 if 11 <= hod or hod < 1 else 0.5)  # cooling load
        charging = hour_abs >= (
            start_h if start_h >= 18 else 24 + start_h
        )  # absolute window time: never switches off at midnight
        if rng:
            load += abs(rng.gauss(0, 0.03))
        ev_kw = 0.0
        if charging and soc_kwh < cap_kwh - 1e-9:
            ev_kw = min(power, (cap_kwh - soc_kwh) / (schedule.charge_efficiency * STEP_MIN / 60))
            soc_kwh += ev_kw * schedule.charge_efficiency * STEP_MIN / 60
        readings.append((t, load + ev_kw))
        if soc_at_departure is None and hour_abs >= 24 + schedule.departure_hour:
            soc_at_departure = soc_kwh / schedule.battery_kwh * 100
    return {
        "readings": readings,
        "departure_soc_pct": soc_at_departure if soc_at_departure is not None else soc_kwh / schedule.battery_kwh * 100,
    }


def summarise_readings(readings: list[tuple[datetime, float]], tariff: Tariff) -> dict[str, float]:
    """Energy (kWh), tariff cost and peak power (kW) from regular kW readings (step inferred from timestamps)."""
    if len(readings) < 2:
        raise AdapterError("need at least two readings")
    step_h = (readings[1][0] - readings[0][0]).total_seconds() / 3600
    energy = sum(kw * step_h for _, kw in readings)
    cost = sum(kw * step_h * tariff.price_at(t.hour + t.minute / 60) for t, kw in readings)
    peak = max(kw for _, kw in readings)
    return {"energy_kwh": energy, "energy_cost": cost + tariff.demand_charge_per_kw * peak, "peak_power_kw": peak}


class SyntheticWattwiseAdapter:
    """Simulated smart-meter experiments. Concurrency-safe and idempotent."""

    metadata = AdapterMetadata(
        name="wattwise-meter",
        version="1.0",
        domain="household electricity",
        synthetic=True,
        description="SIMULATED smart-meter readings with tariff cost; no real meter or paid API involved.",
        input_schema={
            "charge_start_hour": {"type": "discrete", "min": 0, "max": 23},
            "charge_power_kw": {"type": "continuous", "min": 1.0, "max": 11.0},
            "cooling_setpoint": {"type": "continuous", "min": 18.0, "max": 30.0},
        },
        tags=("example", "synthetic", "energy"),
    )
    capabilities = AdapterCapabilities(
        immediate_execution=True,
        time_series=True,
        concurrent=True,
        max_concurrency=8,
        retryable=True,
        idempotent=True,
        resumable=False,
    )

    def __init__(self, schedule: MeterSchedule | None = None, *, noise: bool = True) -> None:
        self.schedule = schedule or MeterSchedule()
        self.noise = noise
        self._prepared: ExecutionContext | None = None

    async def validate(self, config: dict[str, Any]) -> None:
        for name in ("charge_start_hour", "charge_power_kw", "cooling_setpoint"):
            if name not in config:
                raise AdapterError(f"missing parameter '{name}'")
        if not 0 <= float(config["charge_start_hour"]) <= 23:
            raise AdapterError("charge_start_hour must be within 0-23")

    async def prepare(self, context: ExecutionContext) -> None:
        self._prepared = context

    async def available(self) -> bool:
        return True

    def _measure(self, run_id: str, cfg: dict[str, Any], requested: list[str] | None) -> list[Measurement]:
        sim = simulate_day(cfg, self.schedule, _rng(run_id, sorted(cfg.items())) if self.noise else None)
        s = summarise_readings(sim["readings"], self.schedule.tariff)
        values = {
            "energy_cost": (s["energy_cost"], "currency"),
            "energy_kwh": (s["energy_kwh"], "kWh"),
            "peak_power_kw": (s["peak_power_kw"], "kW"),
            "departure_charge_pct": (sim["departure_soc_pct"], "percent"),
        }
        meta = {"synthetic": True, "reading_kind": "simulated"}
        out = [
            Measurement(run_id=run_id, metric=m, value=round(v, 5), unit=u, source=SOURCE, metadata=dict(meta))
            for m, (v, u) in values.items()
            if not requested or m in requested
        ]
        if requested and "grid_power" in requested:
            out += [
                Measurement(
                    run_id=run_id,
                    metric="grid_power",
                    value=round(kw, 5),
                    unit="kW",
                    timestamp=t,
                    source=SOURCE,
                    channel="grid",
                    metadata={**meta, "sample_index": i, "sampling_interval_s": STEP_MIN * 60},
                )
                for i, (t, kw) in enumerate(sim["readings"])
            ]
        return out

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


def measurements_from_csv(
    run_id: str,
    csv_text: str,
    *,
    tariff: Tariff | None = None,
    unit: str = "kW",
    source: str = "csv-import",
    departure_charge_pct: float | None = None,
    include_series: bool = True,
) -> list[Measurement]:
    """Turn a meter CSV (``timestamp,power`` ISO-8601 timestamps, regular interval) into TRIALFORGE measurements.

    ``unit`` may be 'kW' or 'W'. Rows are tagged ``reading_kind='imported'`` and ``synthetic=False``.
    ``departure_charge_pct`` (from the vehicle) can be supplied for the departure constraint.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or not {"timestamp", "power"} <= set(reader.fieldnames):
        raise AdapterError("CSV needs 'timestamp' and 'power' columns")
    rows = []
    for n, r in enumerate(reader, start=2):
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            rows.append((ts if ts.tzinfo else ts.replace(tzinfo=UTC), float(r["power"])))
        except (ValueError, TypeError) as exc:
            raise AdapterError(f"row {n}: invalid timestamp/power ({exc})") from exc
    rows.sort(key=lambda x: x[0])
    kw = [(t, v / 1000.0 if unit == "W" else v) for t, v in rows]
    s = summarise_readings(kw, tariff or Tariff())
    meta = {"synthetic": False, "reading_kind": "imported"}
    out = [
        Measurement(run_id=run_id, metric="energy_kwh", value=s["energy_kwh"], unit="kWh", source=source, metadata=dict(meta)),
        Measurement(
            run_id=run_id, metric="energy_cost", value=s["energy_cost"], unit="currency", source=source, metadata=dict(meta)
        ),
        Measurement(
            run_id=run_id, metric="peak_power_kw", value=s["peak_power_kw"], unit="kW", source=source, metadata=dict(meta)
        ),
    ]
    if departure_charge_pct is not None:
        out.append(
            Measurement(
                run_id=run_id,
                metric="departure_charge_pct",
                value=departure_charge_pct,
                unit="percent",
                source=source,
                metadata=dict(meta),
            )
        )
    if include_series:
        out += [
            Measurement(
                run_id=run_id,
                metric="grid_power",
                value=v,
                unit="kW",
                timestamp=t,
                source=source,
                channel="grid",
                metadata={**meta, "sample_index": i},
            )
            for i, (t, v) in enumerate(kw)
        ]
    return out
