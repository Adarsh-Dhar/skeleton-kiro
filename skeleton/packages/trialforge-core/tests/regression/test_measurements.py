from datetime import UTC, datetime

from trialforge import Measurement, MeasurementDefinition, MeasurementEngine


def test_quality_assessment_preserves_source_and_reports_missing_and_duplicates():
    instant = datetime(2026, 11, 13, tzinfo=UTC)
    source = Measurement(run_id="r1", metric="x", value=4.0, unit="meters", timestamp=instant)
    duplicate = Measurement(run_id="r1", metric="x", value=4.0, unit="meters", timestamp=instant)
    wrong_unit = Measurement(run_id="r1", metric="x", value=3.0, unit="seconds")
    engine = MeasurementEngine(
        [
            MeasurementDefinition(metric="x", unit="meters", expected_min=0, expected_max=5),
            MeasurementDefinition(metric="required", unit="count"),
        ]
    )
    valid, assessed, warnings = engine.assess([source, duplicate, wrong_unit], "r1")
    assert source.quality_flags == []
    assert [item.measurement_id for item in valid] == [source.measurement_id]
    assert "duplicate" in assessed[1].quality_flags
    assert "unit_mismatch" in assessed[2].quality_flags
    assert any("missing required metric" in warning for warning in warnings)
    processed = engine.process(assessed, "r1")
    assert len(processed) == 1
    assert processed[0].value == 4
    assert processed[0].source_measurement_ids == [source.measurement_id]
    assert processed[0].transformations[0].operation == "mean"


def test_outliers_and_nonfinite_numbers_are_flagged_and_excluded():
    engine = MeasurementEngine([MeasurementDefinition(metric="x", unit="u", expected_min=0, expected_max=10)])
    records = [
        Measurement(run_id="r", metric="x", value=11, unit="u"),
        Measurement(run_id="r", metric="x", value=float("nan"), unit="u"),
    ]
    valid, assessed, _ = engine.assess(records, "r")
    assert not valid
    assert "out_of_range" in assessed[0].quality_flags
    assert "non_finite" in assessed[1].quality_flags
