from datetime import UTC, datetime, timedelta

import pytest

from trialforge import (
    DerivedMetric,
    Measurement,
    MeasurementDefinition,
    MeasurementEngine,
    PluginRegistry,
    StreamingWindowAggregator,
    UnitRegistry,
    ValidationError,
    convert,
    make_series,
)
from trialforge.measurements.quality import is_usable, quality_status

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def M(metric="x", value=1.0, unit="u", **kw):
    return Measurement(run_id=kw.pop("run_id", "r"), metric=metric, value=value, unit=unit, **kw)


# ---------------------------------------------------------------------------- units
@pytest.mark.parametrize(
    "value,src,dst,expected",
    [
        (1500, "ms", "s", 1.5),
        (2, "s", "ms", 2000),
        (2.5, "kW", "W", 2500),
        (1500, "W", "kW", 1.5),
        (1.5, "kWh", "Wh", 1500),
        (3.6e6, "J", "kWh", 1.0),
        (100, "degC", "degF", 212),
        (32, "degF", "degC", 0),
        (0, "degC", "K", 273.15),
        (50, "percent", "fraction", 0.5),
        (1, "h", "min", 60),
        (36, "km/h", "m/s", 10),
        (1, "psi", "kPa", 6.894757293168),
        (5, "seconds", "s", 5),
        (1, "kilowatt-hour", "kWh", 1),
    ],
)
def test_unit_conversions_correct(value, src, dst, expected):
    assert convert(value, src, dst) == pytest.approx(expected)


@pytest.mark.parametrize("src,dst", [("s", "W"), ("kWh", "kW"), ("degC", "kg"), ("score", "index"), ("EUR", "USD"), ("m", "s")])
def test_incompatible_or_unknown_unit_conversions_rejected(src, dst):
    with pytest.raises(ValidationError):
        convert(1.0, src, dst)


def test_units_are_not_treated_as_arbitrary_matching_strings():
    reg = UnitRegistry()
    assert reg.compatible("kW", "W") and not reg.compatible("kW", "kWh") and reg.same_unit("seconds", "s")
    assert not reg.same_unit("kW", "W") and reg.convert(5, "score", "score") == 5
    reg.register("money", "cent", 0.01)
    reg.register("money", "dollar", 1.0)
    assert reg.convert(250, "cent", "dollar") == pytest.approx(2.5)


def test_engine_converts_units_keeps_raw_value_and_traces_it():
    eng = MeasurementEngine([MeasurementDefinition(metric="t", unit="s", expected_min=0, expected_max=10)])
    src = M("t", 2500.0, "ms")
    valid, assessed, _ = eng.assess([src], "r")
    m = assessed[0]
    assert src.unit == "ms" and src.value == 2500.0 and src.quality_flags == []  # source untouched
    assert m.value == pytest.approx(2.5) and m.unit == "s" and m.raw_value == 2500.0 and m.raw_unit == "ms"
    assert "unit_converted" in m.quality_flags and is_usable(m.quality_flags) and quality_status(m.quality_flags) == "accepted"
    proc = eng.process(assessed, "r")[0]
    ops = [t.operation for t in proc.transformations]
    assert ops[0] == "unit_conversion" and proc.transformations[0].parameters["from"] == "ms" and ops[-1] == "mean"
    assert proc.source_measurement_ids == [src.measurement_id] and proc.value == pytest.approx(2.5)


def test_incompatible_unit_is_flagged_invalid_not_converted():
    eng = MeasurementEngine([MeasurementDefinition(metric="p", unit="kW")])
    valid, assessed, warnings = eng.assess([M("p", 5.0, "kWh")], "r")
    assert valid == [] and "unit_mismatch" in assessed[0].quality_flags and any("unit mismatch" in w for w in warnings)


# --------------------------------------------------------------------- data quality taxonomy
def test_quality_statuses_are_distinguished_and_nothing_is_deleted():
    eng = MeasurementEngine(
        [
            MeasurementDefinition(metric="x", unit="u", expected_min=0, expected_max=100),
            MeasurementDefinition(metric="need", unit="u"),
        ]
    )
    ms = [M("x", v) for v in (10, 10.5, 9.8, 10.2, 10.1, 9.9, 55.0)]
    ms += [
        M("x", float("nan")),
        M("x", "text"),
        M("x", 500.0),
        M("x", 10.3, metadata={"sensor_warning": True}),
    ]
    valid, assessed, warnings = eng.assess(ms, "r")
    status = [quality_status(m.quality_flags) for m in assessed]
    assert len(assessed) == len(ms)  # all retained
    assert status[6] == "outlier" and status[7] == "invalid" and status[8] == "invalid" and status[9] == "invalid"
    assert status[10] == "sensor_warning" and status[0] == "accepted"
    assert "statistical_outlier" in assessed[6].quality_flags and assessed[6] in valid  # outlier kept & usable
    assert "non_finite" in assessed[7].quality_flags and "invalid_type" in assessed[8].quality_flags
    assert "out_of_range" in assessed[9].quality_flags
    assert any("missing required metric: need" in w for w in warnings)
    rep = eng.quality_report(assessed)
    assert rep["missing_metrics"] == ["need"] and rep["counts"]["outlier"] == 1 and len(rep["invalid"]) == 3
    kept = eng.process(assessed, "r")
    x_kept = next(p for p in kept if p.metric == "x")
    assert x_kept.value == pytest.approx((10 + 10.5 + 9.8 + 10.2 + 10.1 + 9.9 + 55.0 + 10.3) / 8)  # outlier kept in the mean
    excl = MeasurementEngine(eng.definitions.values(), exclude_outliers=True).process(assessed, "r")
    assert next(p for p in excl if p.metric == "x").value == pytest.approx((10 + 10.5 + 9.8 + 10.2 + 10.1 + 9.9 + 10.3) / 7)


def test_duplicate_run_id_mismatch_and_binding_of_unbound_ids():
    eng = MeasurementEngine([MeasurementDefinition(metric="x", unit="u")])
    a, dup = M("x", 1.0, timestamp=T0), M("x", 1.0, timestamp=T0)
    other = M("x", 2.0, run_id="someone-else")
    unbound = M("x", 3.0, run_id="unused")
    valid, assessed, w = eng.assess([a, dup, other, unbound], "r")
    assert "duplicate" in assessed[1].quality_flags and "run_id_mismatch" in assessed[2].quality_flags
    assert (
        assessed[3].run_id == "r"
        and "run_id_bound" in assessed[3].quality_flags
        and assessed[3].metadata["adapter_run_id"] == "unused"
    )
    assert [m.measurement_id for m in valid] == [a.measurement_id, unbound.measurement_id]


def test_naive_timestamps_assumed_utc_and_flagged():
    eng = MeasurementEngine([MeasurementDefinition(metric="x", unit="u")])
    _, assessed, _ = eng.assess([M("x", 1.0, timestamp=datetime(2026, 1, 1, 0, 0))], "r")
    assert "timestamp_assumed_utc" in assessed[0].quality_flags and assessed[0].timestamp.tzinfo is not None


# ------------------------------------------------------------------------------ time series
def series_engine(**kw):
    return MeasurementEngine(
        [
            MeasurementDefinition(
                metric="p", unit="kW", time_series=True, sampling_interval_seconds=60, aggregation=kw.get("agg", "mean")
            )
        ]
    )


def test_time_series_out_of_order_missing_and_duplicate_samples():
    eng = series_engine()
    ts = [T0 + timedelta(minutes=m) for m in (0, 1, 3, 2, 8, 9, 9)]  # out of order, a gap, duplicate at 9
    vals = [1, 2, 4, 3, 9, 10, 10]
    ms = make_series("r", "p", "kW", ts, vals, channel="a", source="meter-1")
    valid, assessed, warns = eng.assess(ms, "r")
    flags = [set(m.quality_flags) for m in assessed]
    assert "out_of_order" in flags[3] and "duplicate" in flags[6]
    assert any("missing samples for p[a]" in w and "1 gap" in w for w in warns)
    proc = eng.process(assessed, "r")[0]
    assert proc.value == pytest.approx((1 + 2 + 4 + 3 + 9 + 10) / 6)
    assert all(m.source == "meter-1" for m in assessed) and [m.metadata["sample_index"] for m in assessed] == list(range(7))


def test_window_aggregation_multichannel_and_different_frequencies():
    eng = MeasurementEngine([MeasurementDefinition(metric="p", unit="kW", time_series=True)])
    fast = make_series("r", "p", "kW", [T0 + timedelta(seconds=10 * i) for i in range(12)], list(range(12)), channel="fast")
    slow = make_series("r", "p", "kW", [T0 + timedelta(seconds=60 * i) for i in range(2)], [100, 200], channel="slow")
    _, assessed, _ = eng.assess(fast + slow, "r")
    f = eng.aggregate_windows(assessed, "r", "p", window_seconds=60, channel="fast")
    assert [p.value for p in f] == [pytest.approx(2.5), pytest.approx(8.5)]  # means of 0-5 and 6-11
    assert (
        f[0].window_end - f[0].window_start == timedelta(seconds=60) and f[0].transformations[-1].parameters["sample_count"] == 6
    )
    s = eng.aggregate_windows(assessed, "r", "p", window_seconds=60, channel="slow", aggregation="sum")
    assert [p.value for p in s] == [100, 200]
    both = eng.aggregate_windows(assessed, "r", "p", window_seconds=120, aggregation="max")
    assert both[0].value == 200 and len(both[0].source_measurement_ids) == 12 + 2
    assert eng.aggregate_windows(assessed, "r", "p", window_seconds=60, channel="none") == []
    with pytest.raises(ValidationError):
        eng.aggregate_windows(assessed, "r", "p", window_seconds=0)


def test_empty_windows_are_omitted_not_zero_filled():
    eng = MeasurementEngine([MeasurementDefinition(metric="p", unit="kW", time_series=True)])
    ms = make_series("r", "p", "kW", [T0, T0 + timedelta(seconds=5), T0 + timedelta(seconds=305)], [1, 3, 7])
    _, a, _ = eng.assess(ms, "r")
    w = eng.aggregate_windows(a, "r", "p", window_seconds=60)
    assert [round(p.value, 6) for p in w] == [2.0, 7.0] and w[1].window_start == T0 + timedelta(seconds=300)


def test_resampling_interpolates_flagged_and_never_extrapolates_and_preserves_raw():
    eng = MeasurementEngine([MeasurementDefinition(metric="p", unit="kW", time_series=True)])
    raw = make_series("r", "p", "kW", [T0, T0 + timedelta(seconds=30), T0 + timedelta(seconds=90)], [0.0, 3.0, 9.0])
    _, a, _ = eng.assess(raw, "r")
    out = eng.resample(a, "p", interval_seconds=15)
    assert [round(m.value, 6) for m in out] == [0.0, 1.5, 3.0, 4.5, 6.0, 7.5, 9.0]
    assert out[-1].timestamp == T0 + timedelta(seconds=90)  # no points beyond last sample
    assert "interpolated" in out[1].quality_flags and out[2].quality_flags == []  # exact grid hit is not flagged
    assert set(out[1].metadata["interpolated_from"]) == {raw[0].measurement_id, raw[1].measurement_id}
    assert [m.value for m in raw] == [0.0, 3.0, 9.0] and len(a) == 3
    assert eng.resample(a[:1], "p", interval_seconds=10) == []


def test_streaming_aggregator_emits_closed_windows_and_tracks_late_data():
    eng = MeasurementEngine([MeasurementDefinition(metric="p", unit="kW", time_series=True)])
    agg = StreamingWindowAggregator(eng, "r", "p", window_seconds=60, allowed_lateness_seconds=10, origin=T0)
    for s, v in ((5, 1), (20, 3), (70, 10), (50, 5)):  # 50s arrives after 70s but within lateness
        agg.push(M("p", float(v), "kW", timestamp=T0 + timedelta(seconds=s), metadata={"sample_index": s}))
    assert agg.emit(T0 + timedelta(seconds=65)) == []  # window 0 not yet closed (60 + 10 lateness)
    first = agg.emit(T0 + timedelta(seconds=71))
    assert len(first) == 1 and first[0].value == pytest.approx(3.0) and first[0].window_start == T0
    agg.push(M("p", 99.0, "kW", timestamp=T0 + timedelta(seconds=30), metadata={"sample_index": 30}))
    assert len(agg.late) == 1 and "late_arrival" in agg.late[0].quality_flags
    second = agg.emit(T0 + timedelta(seconds=500))
    assert len(second) == 1 and second[0].value == 10.0 and agg.emit(T0 + timedelta(seconds=900)) == []  # emitted once


# --------------------------------------------------------------- transformations and derived metrics
def test_registered_transformation_applies_and_is_recorded():
    reg = PluginRegistry()
    reg.register("transformation", "clip", lambda vals, lo, hi: [min(max(v, lo), hi) for v in vals])
    eng = MeasurementEngine(
        [MeasurementDefinition(metric="x", unit="u", transformations=[{"name": "clip", "params": {"lo": 0, "hi": 5}}])],
        plugins=reg,
    )
    _, a, _ = eng.assess([M("x", 2.0), M("x", 9.0, timestamp=T0 + timedelta(seconds=1))], "r")
    p = eng.process(a, "r")[0]
    assert p.value == pytest.approx(3.5)  # mean of [2, 5]
    assert [t.operation for t in p.transformations] == ["transform:clip", "mean"] and p.transformations[0].parameters == {
        "lo": 0,
        "hi": 5,
    }
    assert a[1].value == 9.0  # raw measurement unchanged
    with pytest.raises(ValidationError, match="unregistered transformation"):
        MeasurementEngine(
            [MeasurementDefinition(metric="x", unit="u", transformations=[{"name": "evil"}])], plugins=PluginRegistry()
        )


def test_derived_metrics_expression_chain_plugin_and_provenance():
    reg = PluginRegistry()
    reg.register("metric", "geo", lambda o: (o["a"] * o["b"]) ** 0.5)
    defs = [MeasurementDefinition(metric="a", unit="s"), MeasurementDefinition(metric="b", unit="s")]
    derived = [
        DerivedMetric(name="total", unit="s", expression="a + b"),
        DerivedMetric(name="share", unit="1", expression="a / total"),
        DerivedMetric(name="g", unit="s", function="geo"),
    ]
    eng = MeasurementEngine(defs, derived_metrics=derived, plugins=reg)
    ma, mb = M("a", 3.0, "s"), M("b", 12.0, "s")
    _, a, _ = eng.assess([ma, mb], "r")
    out = {p.metric: p for p in eng.process(a, "r")}
    assert out["total"].value == 15 and out["share"].value == pytest.approx(0.2) and out["g"].value == pytest.approx(6.0)
    assert out["total"].derived and set(out["total"].source_measurement_ids) == {ma.measurement_id, mb.measurement_id}
    assert (
        out["total"].transformations[0].operation == "derive"
        and out["total"].transformations[0].parameters["expression"] == "a + b"
    )


def test_derived_metric_skipped_when_inputs_missing_or_division_by_zero():
    eng = MeasurementEngine(
        [MeasurementDefinition(metric="a", unit="s", required=False), MeasurementDefinition(metric="b", unit="s")],
        derived_metrics=[DerivedMetric(name="r", unit="1", expression="a / b")],
    )
    _, a, _ = eng.assess([M("b", 0.0, "s")], "r")
    assert [p.metric for p in eng.process(a, "r")] == ["b"]
    _, a, _ = eng.assess([M("a", 1.0, "s"), M("b", 0.0, "s")], "r")
    assert [p.metric for p in eng.process(a, "r")] == ["a", "b"]


def test_aggregation_operations():
    for op, expected in (("mean", 4.0), ("median", 3.0), ("sum", 12.0), ("min", 1.0), ("max", 8.0), ("last", 8.0)):
        eng = MeasurementEngine([MeasurementDefinition(metric="x", unit="u", aggregation=op)])
        ms = [M("x", v, timestamp=T0 + timedelta(seconds=i)) for i, v in enumerate([1.0, 3.0, 8.0])]
        _, a, _ = eng.assess(ms, "r")
        assert eng.process(a, "r")[0].value == pytest.approx(expected), op
    eng = MeasurementEngine([MeasurementDefinition(metric="x", unit="u", aggregation="none")])
    _, a, _ = eng.assess([M("x", 1.0), M("x", 2.0, timestamp=T0)], "r")
    assert len(eng.process(a, "r")) == 2
