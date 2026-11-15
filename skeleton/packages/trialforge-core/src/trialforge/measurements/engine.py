"""Non-destructive measurement validation, conversion, aggregation and provenance.

Guarantees
----------
* Source measurements are never mutated; ``assess`` works on deep copies.
* Nothing is deleted: unusual values are *flagged* (``statistical_outlier``) and kept.
* Every derived record lists the raw measurement ids it came from and a transformation
  history (unit conversion, registered transformations, aggregation, derivation).
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from trialforge.definitions.registry import DEFAULT_PLUGINS, PluginRegistry
from trialforge.errors import ValidationError
from trialforge.measurements.quality import (
    INVALID_FLAGS,
    is_usable,
    quality_status,
)
from trialforge.measurements.units import DEFAULT_UNITS, UnitRegistry
from trialforge.models.core import (
    DerivedMetric,
    Measurement,
    MeasurementDefinition,
    MeasurementTransformation,
    ProcessedMeasurement,
)
from trialforge.utilities.safe_expr import evaluate_expression

UNBOUND_RUN_IDS = frozenset({"", "unused", "unbound"})
"""Adapters may return measurements with one of these run ids; the engine binds them."""


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _utc(ts: datetime) -> tuple[datetime, bool]:
    return (ts.replace(tzinfo=UTC), True) if ts.tzinfo is None else (ts, False)


def _aggregate(values: list[float], op: str, stamps: list[datetime] | None = None) -> float:
    if op == "sum":
        return sum(values)
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    if op == "median":
        return float(statistics.median(values))
    if op == "last":
        if stamps:
            return values[max(range(len(values)), key=lambda i: stamps[i])]
        return values[-1]
    return sum(values) / len(values)


def detect_outliers(values: list[float], method: str = "mad") -> list[bool]:
    """Flag unusual values; requires >=5 samples, otherwise nothing is flagged."""
    n = len(values)
    if n < 5 or method == "none":
        return [False] * n
    if method == "iqr":
        s = sorted(values)
        q1, q3 = statistics.quantiles(s, n=4, method="inclusive")[0], statistics.quantiles(s, n=4, method="inclusive")[2]
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        return [v < lo or v > hi for v in values]
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad == 0:
        return [False] * n
    return [abs(0.6745 * (v - med) / mad) > 3.5 for v in values]


class MeasurementEngine:
    """Validate, convert, aggregate and derive measurements for one experiment."""

    INVALID_FLAGS = set(INVALID_FLAGS)  # retained for backward compatibility

    def __init__(
        self,
        definitions: list[MeasurementDefinition],
        *,
        derived_metrics: list[DerivedMetric] | None = None,
        plugins: PluginRegistry | None = None,
        units: UnitRegistry | None = None,
        exclude_outliers: bool = False,
    ) -> None:
        self.definitions = {d.metric: d for d in definitions}
        self.derived_metrics = list(derived_metrics or [])
        self.plugins = plugins or DEFAULT_PLUGINS
        self.units = units or DEFAULT_UNITS
        self.exclude_outliers = exclude_outliers
        for d in definitions:
            for t in d.transformations:
                if "name" not in t or not self.plugins.has("transformation", t["name"]):
                    raise ValidationError(f"metric '{d.metric}' references unregistered transformation {t.get('name')!r}")
        for dm in self.derived_metrics:
            if dm.function and not self.plugins.has("metric", dm.function):
                raise ValidationError(f"derived metric '{dm.name}' references unregistered metric plugin '{dm.function}'")

    # ------------------------------------------------------------ assessment
    def assess(self, measurements: Iterable[Measurement], run_id: str) -> tuple[list[Measurement], list[Measurement], list[str]]:
        """Return ``(valid, assessed, warnings)``; never mutates the inputs."""
        valid: list[Measurement] = []
        assessed: list[Measurement] = []
        warnings: list[str] = []
        seen: dict[tuple, Any] = {}
        last_ts: dict[tuple[str, str | None], datetime] = {}
        for source in measurements:
            m = source.model_copy(deep=True)
            flags = set(m.quality_flags)
            ts, assumed = _utc(m.timestamp)
            if assumed:
                m.timestamp = ts
                flags.add("timestamp_assumed_utc")
            if m.run_id in UNBOUND_RUN_IDS:
                m.metadata = {**m.metadata, "adapter_run_id": m.run_id}
                m.run_id = run_id
                flags.add("run_id_bound")
            elif m.run_id != run_id:
                warnings.append(f"measurement {m.measurement_id} has mismatched run_id")
                flags.add("run_id_mismatch")
            d = self.definitions.get(m.metric)
            if self.definitions and d is None:
                warnings.append(f"undefined metric: {m.metric}")
                flags.add("undefined_metric")
            elif d:
                self._check_definition(m, d, flags, warnings)
            key_sample = m.metadata.get("sample_index")
            ts_metric = bool(d and d.time_series)
            if ts_metric:  # a series sample is identified by when it was taken, not by its position
                key = (m.metric, m.channel, m.timestamp.isoformat())
            else:
                key = (m.metric, m.channel, str(key_sample) if key_sample is not None else m.timestamp.isoformat(), repr(m.value))
            if key in seen:
                conflict = ts_metric and repr(seen[key]) != repr(m.value)
                warnings.append(f"{'conflicting ' if conflict else ''}duplicate measurement detected for {m.metric}")
                flags.add("duplicate")
            else:
                seen[key] = m.value
            if ts_metric:
                prev = last_ts.get((m.metric, m.channel))
                if prev is not None and m.timestamp < prev:
                    flags.add("out_of_order")
                last_ts[(m.metric, m.channel)] = max(m.timestamp, prev) if prev else m.timestamp
            m.quality_flags = sorted(flags)
            assessed.append(m)
        self._flag_outliers(assessed)
        counts: Counter[str] = Counter()
        for m in assessed:
            if is_usable(m.quality_flags):
                valid.append(m)
                counts[m.metric] += 1
        for metric, d in self.definitions.items():
            if d.required and counts[metric] == 0:
                warnings.append(f"missing required metric: {metric}")
        warnings.extend(self._gap_warnings(assessed))
        return valid, assessed, warnings

    def _check_definition(self, m: Measurement, d: MeasurementDefinition, flags: set, warnings: list) -> None:
        value = m.value
        if m.unit != d.unit:
            if self.units.same_unit(m.unit, d.unit):
                m.unit = d.unit
            elif self.units.compatible(m.unit, d.unit) and _is_num(value) and math.isfinite(float(value)):
                m.raw_value, m.raw_unit = (m.value if m.raw_value is None else m.raw_value), m.unit
                m.value = self.units.convert(float(value), m.unit, d.unit)
                m.metadata = {**m.metadata, "original_unit": m.unit, "original_value": value}
                m.unit = d.unit
                flags.add("unit_converted")
                value = m.value
            else:
                warnings.append(f"unit mismatch for {m.metric}: expected {d.unit}, got {m.unit}")
                flags.add("unit_mismatch")
        if not d.time_series and m.metadata.get("sample_index") is not None:
            warnings.append(f"time-series sample supplied for non-series metric {m.metric}")
            flags.add("unexpected_time_series")
        if (
            d.time_series
            and m.metadata.get("sample_index") is None
            and m.channel is None
            and "timestamp_assumed_utc" not in flags
            and m.metadata.get("timestamped") is not True
        ):
            warnings.append(f"time-series sample index missing for {m.metric}")
            flags.add("missing_sample_index")
        if d.data_type in ("number", "integer"):
            numeric = _is_num(value)
            if not numeric or not math.isfinite(value):
                warnings.append(f"non-finite or non-numeric value for {m.metric}")
                flags.add("invalid_type" if not numeric else "non_finite")
            elif d.data_type == "integer" and not isinstance(value, int) and float(value) != int(value):
                warnings.append(f"non-integer value for {m.metric}")
                flags.add("invalid_type")
        elif d.data_type == "string" and not isinstance(value, str):
            warnings.append(f"non-string value for {m.metric}")
            flags.add("invalid_type")
        elif d.data_type == "boolean" and not isinstance(value, bool):
            warnings.append(f"non-boolean value for {m.metric}")
            flags.add("invalid_type")
        if _is_num(value) and math.isfinite(value):
            if (d.expected_min is not None and value < d.expected_min) or (d.expected_max is not None and value > d.expected_max):
                flags.add("out_of_range")
        if m.metadata.get("sensor_warning") or m.metadata.get("sensor_status") not in (None, "ok"):
            flags.add("sensor_warning")

    def _flag_outliers(self, assessed: list[Measurement]) -> None:
        groups: dict[tuple, list[Measurement]] = defaultdict(list)
        for m in assessed:
            d = self.definitions.get(m.metric)
            if d and d.outlier_method != "none" and _is_num(m.value) and is_usable(m.quality_flags):
                groups[(m.metric, m.channel)].append(m)
        for (metric, _), items in groups.items():
            method = self.definitions[metric].outlier_method
            for m, flag in zip(items, detect_outliers([float(i.value) for i in items], method)):
                if flag:
                    m.quality_flags = sorted({*m.quality_flags, "statistical_outlier"})

    def _gap_warnings(self, assessed: list[Measurement]) -> list[str]:
        out = []
        for metric, d in self.definitions.items():
            if not (d.time_series and d.sampling_interval_seconds):
                continue
            for channel in {m.channel for m in assessed if m.metric == metric}:
                ts = sorted(
                    m.timestamp for m in assessed if m.metric == metric and m.channel == channel and is_usable(m.quality_flags)
                )
                gaps = [
                    (b - a).total_seconds()
                    for a, b in zip(ts, ts[1:])
                    if (b - a).total_seconds() > 1.5 * d.sampling_interval_seconds
                ]
                if gaps:
                    missing = sum(round(g / d.sampling_interval_seconds) - 1 for g in gaps)
                    out.append(
                        f"missing samples for {metric}"
                        + (f"[{channel}]" if channel else "")
                        + f": {len(gaps)} gap(s), about {missing} sample(s)"
                    )
        return out

    def validate(self, measurements: Iterable[Measurement], run_id: str) -> tuple[list[Measurement], list[str]]:
        """Compatibility helper returning valid assessed copies and quality warnings."""
        valid, _, warnings = self.assess(measurements, run_id)
        return valid, warnings

    def quality_report(self, assessed: Iterable[Measurement]) -> dict[str, Any]:
        """Counts per quality status plus metrics with no usable data."""
        assessed = list(assessed)
        status = Counter(quality_status(m.quality_flags) for m in assessed)
        usable_metrics = {m.metric for m in assessed if is_usable(m.quality_flags)}
        return {
            "counts": dict(status),
            "missing_metrics": sorted(k for k, d in self.definitions.items() if d.required and k not in usable_metrics),
            "invalid": [
                {"measurement_id": m.measurement_id, "metric": m.metric, "flags": sorted(set(m.quality_flags) & INVALID_FLAGS)}
                for m in assessed
                if not is_usable(m.quality_flags)
            ],
        }

    # ---------------------------------------------------------- processing
    def _usable(self, m: Measurement) -> bool:
        flags = set(m.quality_flags)
        return is_usable(flags) and not (self.exclude_outliers and "statistical_outlier" in flags)

    def process(self, assessed: Iterable[Measurement], run_id: str) -> list[ProcessedMeasurement]:
        """Aggregate usable samples per metric and compute derived metrics.

        Time-series samples are time-sorted before aggregation. All outputs keep
        ``source_measurement_ids`` and a transformation history.
        """
        by_metric: dict[str, list[Measurement]] = defaultdict(list)
        for m in assessed:
            if self._usable(m):
                by_metric[m.metric].append(m)
        outputs: list[ProcessedMeasurement] = []
        for metric, sources in by_metric.items():
            d = self.definitions.get(metric)
            op = d.aggregation if d else "mean"
            sources = sorted(sources, key=lambda s: s.timestamp)
            numeric = [s for s in sources if _is_num(s.value) and math.isfinite(float(s.value))]
            if op == "none" or not numeric:
                for s in sources:
                    outputs.append(self._passthrough(s, run_id))
                continue
            outputs.append(self._aggregate_group(metric, numeric, op, run_id, d))
        outputs.extend(self._derive(outputs, run_id))
        return outputs

    def _passthrough(self, s: Measurement, run_id: str) -> ProcessedMeasurement:
        out = ProcessedMeasurement(
            run_id=run_id,
            metric=s.metric,
            value=s.value,
            unit=s.unit,
            timestamp=s.timestamp,
            source_measurement_ids=[s.measurement_id],
            quality_flags=list(s.quality_flags),
        )
        self._add_conversion(out, [s])
        return out

    @staticmethod
    def _add_conversion(out: ProcessedMeasurement, sources: list[Measurement]) -> None:
        for s in sources:
            if "unit_converted" in s.quality_flags:
                out.transformations.append(
                    MeasurementTransformation(
                        operation="unit_conversion",
                        input_measurement_ids=[s.measurement_id],
                        output_measurement_id=out.processed_id,
                        parameters={"from": s.raw_unit, "to": s.unit, "raw_value": s.raw_value},
                    )
                )

    def _aggregate_group(
        self,
        metric: str,
        numeric: list[Measurement],
        op: str,
        run_id: str,
        d: MeasurementDefinition | None,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> ProcessedMeasurement:
        values = [float(s.value) for s in numeric]
        stamps = [s.timestamp for s in numeric]
        ids = [s.measurement_id for s in numeric]
        out = ProcessedMeasurement(
            run_id=run_id, metric=metric, value=0.0, unit=numeric[0].unit, timestamp=max(stamps), source_measurement_ids=ids
        )
        self._add_conversion(out, numeric)
        if d:
            for t in d.transformations:
                fn = self.plugins.get("transformation", t["name"])
                before = len(values)
                values = [float(v) for v in fn(values, **t.get("params", {}))]
                if len(values) != before:
                    stamps = stamps[: len(values)]
                out.transformations.append(
                    MeasurementTransformation(
                        operation=f"transform:{t['name']}",
                        input_measurement_ids=ids,
                        output_measurement_id=out.processed_id,
                        parameters=dict(t.get("params", {})),
                    )
                )
        out.value = _aggregate(values, op, stamps)
        flags = {f for s in numeric for f in s.quality_flags if f not in INVALID_FLAGS}
        out.quality_flags = sorted(flags - {"run_id_bound", "timestamp_assumed_utc"})
        params: dict[str, Any] = {"aggregation": op, "sample_count": len(ids)}
        if window:
            out.window_start, out.window_end = window
            params["window_seconds"] = (window[1] - window[0]).total_seconds()
        out.transformations.append(
            MeasurementTransformation(
                operation=op, input_measurement_ids=ids, output_measurement_id=out.processed_id, parameters=params
            )
        )
        return out

    def _derive(self, processed: list[ProcessedMeasurement], run_id: str) -> list[ProcessedMeasurement]:
        if not self.derived_metrics:
            return []
        values: dict[str, float] = {}
        sources: dict[str, list[str]] = defaultdict(list)
        flags: dict[str, set[str]] = defaultdict(set)
        for p in processed:
            if _is_num(p.value) and math.isfinite(float(p.value)):
                values.setdefault(p.metric, float(p.value))
                sources[p.metric].extend(p.source_measurement_ids)
                flags[p.metric].update(p.quality_flags)
        derived: list[ProcessedMeasurement] = []
        for dm in self.derived_metrics:  # declared order allows chained derivations
            try:
                if dm.expression:
                    from trialforge.utilities.safe_expr import expression_names

                    names = expression_names(dm.expression)
                    if not names <= values.keys():
                        continue
                    v = evaluate_expression(dm.expression, values)
                else:
                    names = set(values)
                    v = float(self.plugins.get("metric", dm.function)(dict(values)))
            except (ZeroDivisionError, ValueError, OverflowError, KeyError):
                continue
            if not math.isfinite(v):
                continue
            src = sorted({i for n in names for i in sources.get(n, [])})
            out = ProcessedMeasurement(
                run_id=run_id,
                metric=dm.name,
                value=v,
                unit=dm.unit,
                timestamp=max((p.timestamp for p in processed), default=datetime.now(UTC)),
                source_measurement_ids=src,
                derived=True,
                quality_flags=sorted({f for n in names for f in flags.get(n, set())}),
            )
            out.transformations.append(
                MeasurementTransformation(
                    operation="derive",
                    input_measurement_ids=src,
                    output_measurement_id=out.processed_id,
                    parameters={"name": dm.name, "expression": dm.expression, "function": dm.function, "inputs": sorted(names)},
                )
            )
            derived.append(out)
            values[dm.name] = v
            sources[dm.name] = src
            flags[dm.name] = set(out.quality_flags)
        return derived

    # ----------------------------------------------------------- time series
    def aggregate_windows(
        self,
        assessed: Iterable[Measurement],
        run_id: str,
        metric: str,
        *,
        window_seconds: float,
        aggregation: str = "mean",
        channel: str | None = None,
        origin: datetime | None = None,
    ) -> list[ProcessedMeasurement]:
        """Tumbling-window aggregation of usable samples of ``metric``.

        Windows are ``[origin + k*w, origin + (k+1)*w)``; ``origin`` defaults to the first
        sample. Empty windows are omitted (not zero-filled).
        """
        if window_seconds <= 0:
            raise ValidationError("window_seconds must be positive")
        samples = sorted(
            (
                m
                for m in assessed
                if m.metric == metric and self._usable(m) and _is_num(m.value) and (channel is None or m.channel == channel)
            ),
            key=lambda m: m.timestamp,
        )
        if not samples:
            return []
        origin = origin or samples[0].timestamp
        buckets: dict[int, list[Measurement]] = defaultdict(list)
        for m in samples:
            buckets[int((m.timestamp - origin).total_seconds() // window_seconds)].append(m)
        d = self.definitions.get(metric)
        out = []
        for k in sorted(buckets):
            start = origin + timedelta(seconds=k * window_seconds)
            out.append(
                self._aggregate_group(
                    metric, buckets[k], aggregation, run_id, d, window=(start, start + timedelta(seconds=window_seconds))
                )
            )
        return out

    def resample(
        self, assessed: Iterable[Measurement], metric: str, *, interval_seconds: float, channel: str | None = None
    ) -> list[Measurement]:
        """Linearly interpolate usable samples onto a regular grid.

        Returns *new* measurements flagged ``interpolated``; the raw series is untouched and each
        new record lists its two neighbouring source ids in ``metadata['interpolated_from']``.
        Extrapolation is never performed.
        """
        if interval_seconds <= 0:
            raise ValidationError("interval_seconds must be positive")
        pts = sorted(
            (
                m
                for m in assessed
                if m.metric == metric and self._usable(m) and _is_num(m.value) and (channel is None or m.channel == channel)
            ),
            key=lambda m: m.timestamp,
        )
        if len(pts) < 2:
            return []
        out: list[Measurement] = []
        t = pts[0].timestamp
        end = pts[-1].timestamp
        i = 0
        while t <= end:
            while i + 1 < len(pts) - 1 and pts[i + 1].timestamp < t:
                i += 1
            a, b = pts[i], pts[i + 1]
            span = (b.timestamp - a.timestamp).total_seconds()
            frac = 0.0 if span == 0 else (t - a.timestamp).total_seconds() / span
            frac = min(max(frac, 0.0), 1.0)
            exact = t in (a.timestamp, b.timestamp)
            out.append(
                Measurement(
                    run_id=a.run_id,
                    metric=metric,
                    value=float(a.value) + frac * (float(b.value) - float(a.value)),
                    unit=a.unit,
                    timestamp=t,
                    channel=channel,
                    quality_flags=[] if exact else ["interpolated"],
                    source=a.source,
                    metadata={
                        "interpolated_from": [a.measurement_id, b.measurement_id],
                        "resample_interval_seconds": interval_seconds,
                    },
                )
            )
            t += timedelta(seconds=interval_seconds)
        return out


def make_series(
    run_id: str,
    metric: str,
    unit: str,
    timestamps: list[datetime],
    values: list[Any],
    *,
    channel: str | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[Measurement]:
    """Build time-series ``Measurement`` records with sample indices and provenance."""
    if len(timestamps) != len(values):
        raise ValidationError("timestamps and values must have equal length")
    return [
        Measurement(
            run_id=run_id,
            metric=metric,
            value=v,
            unit=unit,
            timestamp=t,
            channel=channel,
            source=source,
            metadata={**(metadata or {}), "sample_index": i},
        )
        for i, (t, v) in enumerate(zip(timestamps, values))
    ]


class StreamingWindowAggregator:
    """Incremental tumbling-window aggregation with allowed lateness.

    ``push`` accepts samples in any order. ``emit(now)`` returns windows whose end is
    older than ``now - allowed_lateness``; each window is emitted once. Samples arriving for an
    already emitted window are kept in ``late`` (flagged ``late_arrival``), never silently dropped.
    """

    def __init__(
        self,
        engine: MeasurementEngine,
        run_id: str,
        metric: str,
        *,
        window_seconds: float,
        aggregation: str = "mean",
        allowed_lateness_seconds: float = 0.0,
        origin: datetime,
    ) -> None:
        self.engine, self.run_id, self.metric = engine, run_id, metric
        self.w, self.agg, self.lateness, self.origin = window_seconds, aggregation, allowed_lateness_seconds, origin
        self._open: dict[int, list[Measurement]] = defaultdict(list)
        self._emitted: set[int] = set()
        self.late: list[Measurement] = []

    def push(self, m: Measurement) -> None:
        """Add a sample; samples for already emitted windows are kept as late arrivals."""
        k = int((m.timestamp - self.origin).total_seconds() // self.w)
        if k in self._emitted:
            m = m.model_copy(update={"quality_flags": sorted({*m.quality_flags, "late_arrival"})})
            self.late.append(m)
        else:
            self._open[k].append(m)

    def emit(self, now: datetime) -> list[ProcessedMeasurement]:
        """Return processed measurements for windows closed as of ``now`` (each window is emitted once)."""
        out = []
        for k in sorted(self._open):
            end = self.origin + timedelta(seconds=(k + 1) * self.w)
            if end + timedelta(seconds=self.lateness) <= now:
                _, assessed, _ = self.engine.assess(self._open.pop(k), self.run_id)
                usable = [m for m in assessed if self.engine._usable(m) and _is_num(m.value)]
                self._emitted.add(k)
                if usable:
                    start = end - timedelta(seconds=self.w)
                    out.append(
                        self.engine._aggregate_group(
                            self.metric,
                            usable,
                            self.agg,
                            self.run_id,
                            self.engine.definitions.get(self.metric),
                            window=(start, end),
                        )
                    )
        return out
