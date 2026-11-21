"""Unit tests for the application-owned simulator model and telemetry parser."""

import pytest

from f1lab.adapters.telemetry import MAX_ROWS, parse_telemetry_csv, to_measurements
from f1lab.adapters.vehicle_model import BASELINE, FACTORS, rng_for, run_level, stint, vehicle_model


def test_vehicle_model_is_deterministic_and_internally_consistent():
    m1, m2 = vehicle_model(BASELINE), vehicle_model(dict(BASELINE))
    assert m1 == m2
    assert m1["lap_time"] == pytest.approx(m1["sector_1"] + m1["sector_2"] + m1["sector_3"])
    assert 0 <= m1["stability"] <= 100 and 60 < m1["lap_time"] < 100
    assert vehicle_model({}) == vehicle_model(BASELINE)  # missing factors fall back to the baseline


def test_less_downforce_is_faster_but_less_stable_so_the_constraint_matters():
    low = vehicle_model({"front_wing": 15, "rear_wing": 19})
    mid = vehicle_model({"front_wing": 25, "rear_wing": 29})
    assert low["lap_time"] < mid["lap_time"] and low["stability"] < mid["stability"]
    assert low["stability"] < 85 <= mid["stability"]


def test_balance_and_tyre_pressure_have_interior_optima():
    base = vehicle_model(BASELINE)["lap_time"]
    assert vehicle_model({**BASELINE, "rear_wing": BASELINE["rear_wing"] + 15})["lap_time"] > base
    assert (
        vehicle_model({**BASELINE, "tyre_pressure_psi": 18})["lap_time"] > vehicle_model({**BASELINE, "tyre_pressure_psi": 22})["lap_time"]
    )


def test_stint_is_reproducible_per_run_and_replicates_differ():
    a, b, c = stint(BASELINE, "run-1", 5, 0.0), stint(BASELINE, "run-1", 5, 0.0), stint(BASELINE, "run-2", 5, 0.0)
    assert a == b and a != c
    assert [lap["lap"] for lap in a] == [1, 2, 3, 4, 5] and all(
        abs(lap["lap_time"] - sum(lap[k] for k in ("sector_1", "sector_2", "sector_3"))) < 1e-9 for lap in a
    )
    assert run_level(BASELINE, "run-1") == run_level(BASELINE, "run-1")
    assert rng_for("x").random() == rng_for("x").random()


def test_slow_lap_injection():
    laps = stint(BASELINE, "r", 200, 1.0)
    assert all(lap["sector_2"] > vehicle_model(BASELINE)["sector_2"] + 1.0 for lap in laps)


GOOD = "lap,sector_1,sector_2,sector_3,lap_time,stability\n1,27.0,31.5,21.6,80.1,88\n2,27.1,31.6,21.7,80.4,88\n"


def test_parse_valid_csv_defaults_lap_time_and_numbers_laps():
    p = parse_telemetry_csv("sector_1,sector_2,sector_3\n27,31,21\n27.5,31.5,21.5\n")
    assert not p.errors and [lap["lap"] for lap in p.laps] == [1, 2] and p.laps[0]["lap_time"] == pytest.approx(79.0)
    assert parse_telemetry_csv(GOOD).laps[0]["stability"] == 88


def test_parse_reports_row_problems_and_keeps_good_rows():
    p = parse_telemetry_csv("lap,sector_1,sector_2,sector_3\n1,27,31,21\n2,abc,31,21\n3,27,-1,21\n4,27,,21\n5,27,31,21\n")
    assert len(p.laps) == 2 and len(p.errors) == 3
    assert "line 3" in p.errors[0] and "not a number" in p.errors[0] and "non-negative" in p.errors[1] and "required" in p.errors[2]


@pytest.mark.parametrize(
    "text,msg",
    [
        ("", "empty"),
        ("a,b\n1,2\n", "missing required"),
        ("sector_1,sector_2,sector_3\n", "no data rows"),
        ("sector_1,sector_2,sector_3\n" + "1,1,1\n" * 3 + "x" * 1_100_000, "larger than"),
    ],
)
def test_parse_rejects_unusable_files(text, msg):
    p = parse_telemetry_csv(text)
    assert not p.laps and msg in p.errors[0]


def test_parse_row_cap_and_bad_unit():
    p = parse_telemetry_csv("sector_1,sector_2,sector_3\n" + "1,1,1\n" * (MAX_ROWS + 5))
    assert len(p.laps) == MAX_ROWS and "first" in p.errors[-1]
    with pytest.raises(ValueError):
        parse_telemetry_csv(GOOD, "min")


def test_measurements_are_tagged_imported_and_carry_the_file_unit():
    ms = to_measurements("r1", parse_telemetry_csv(GOOD.replace("27.0", "27000").replace("31.5", "31500"), "ms"), "csv:x.csv")
    assert all(m.metadata["imported"] is True and m.metadata["synthetic"] is False and m.source == "csv:x.csv" for m in ms)
    assert {m.unit for m in ms if m.metric.startswith("sector")} == {"ms"} and {m.unit for m in ms if m.metric == "stability"} == {"score"}
    assert set(FACTORS) == {"front_wing", "rear_wing", "suspension", "tyre_pressure_psi"}
