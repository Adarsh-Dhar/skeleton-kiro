"""Task 3.7: example tests for CSV import cases, driven through import_session_csv (Req 18.1).

Small fixtures live in tests/csv_fixtures/. The maximum-size file is generated here by lowering the limit.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import text

from db.engine import create_db_engine
from db.migrations import migrate
from f1lab.integrations.historical import csv_importer
from f1lab.integrations.historical.importer import HistoricalImportError
from f1lab.integrations.historical.repository import HistoricalRepository

FIXTURES = Path(__file__).parent / "csv_fixtures"
META = {"year": 2024, "event": "Imola testing", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}
UNITS = {"time": "seconds", "distance": "metres", "speed": "kph"}


@pytest.fixture()
def env(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    up = tmp_path / "csv-uploads"
    up.mkdir()
    return HistoricalRepository(e), up, e


def _register(up: Path, e, data: bytes, name: str) -> str:
    h = hashlib.sha256(data).hexdigest()
    (up / f"{h}.csv").write_bytes(data)
    with e.begin() as c:
        c.execute(text("INSERT OR REPLACE INTO f1_csv_upload (file_hash, original_filename, size_bytes, uploaded_at) "
                       "VALUES (:h,:n,:s,'2024-01-01')"), {"h": h, "n": name, "s": len(data)})
    return h


def run(env, fixture: str, data_type: str | None = None, units: dict | None = None, **csv_extra):
    repo, up, e = env
    h = _register(up, e, (FIXTURES / fixture).read_bytes(), fixture)
    csv = {"file_hash": h, "units": units or dict(UNITS), "metadata": dict(META), **csv_extra}
    if data_type:
        csv["data_type"] = data_type
    return csv_importer.import_session_csv(repo, {"provider": "csv_upload", "csv": csv}, upload_dir=up)


def rejected(env, fixture: str, code: str, **kw) -> HistoricalImportError:
    repo = env[0]
    with pytest.raises(HistoricalImportError) as ei:
        run(env, fixture, **kw)
    assert ei.value.code == code
    assert repo.list_sessions() == []  # a rejected file leaves no session
    return ei.value


def times(env, manifest) -> list[float]:
    return [round(float(t), 6) for t in env[0].telemetry(manifest["session_id"], "VER")["session_time_s"]]


# ---- valid files ----------------------------------------------------------------------------------------

def test_valid_lap_summary_accepted(env):
    m = run(env, "lap_summary_valid.csv")
    repo = env[0]
    assert m["data_type"] == "Lap summaries"
    assert len(repo.laps(m["session_id"])) == 3 and repo.telemetry(m["session_id"], "VER").empty


def test_valid_sampled_telemetry_accepted(env):
    m = run(env, "sampled_valid.csv", data_type="sampled_telemetry")
    tel = env[0].telemetry(m["session_id"], "VER")
    assert m["data_type"] == "Full telemetry" and len(tel) == 5 and tel["speed_kph"].notna().all()


# ---- time units and timestamp formats -------------------------------------------------------------------

def test_time_in_seconds(env):
    assert times(env, run(env, "time_seconds.csv")) == [0.0, 1.0, 2.0, 3.0]


def test_time_in_milliseconds_converted_to_seconds(env):
    m = run(env, "time_milliseconds.csv", units={**UNITS, "time": "milliseconds"})
    assert times(env, m) == [0.0, 1.0, 2.0, 3.0]


def test_iso8601_with_offset_accepted(env):
    m = run(env, "ts_iso_offset.csv")
    assert times(env, m) == [0.0, 1.0, 2.5]


def test_epoch_seconds_accepted(env):
    assert times(env, run(env, "ts_epoch.csv")) == [0.0, 1.0, 2.5]


def test_utc_and_non_utc_offset_describe_the_same_instants(env, tmp_path):
    # 14:00+02:00 and 12:00Z are the same instant, so both files normalise to identical session times.
    a = run(env, "ts_utc.csv")
    times_utc = times(env, a)
    e2 = create_db_engine(tmp_path / "second.db")
    migrate(e2)
    (tmp_path / "u2").mkdir()
    env2 = (HistoricalRepository(e2), tmp_path / "u2", e2)
    b = run(env2, "ts_iso_offset.csv")
    assert times_utc == times(env2, b) == [0.0, 1.0, 2.5]


# ---- rejected / handled data problems -------------------------------------------------------------------

def test_missing_channel_marked_unavailable_not_filled(env):
    m = run(env, "missing_channel.csv")
    tel = env[0].telemetry(m["session_id"], "VER")
    assert "brake_pct" in m["unavailable_channels"] and tel["brake_pct"].isna().all()
    assert tel["speed_kph"].notna().all()


def test_lap_summary_missing_required_column_rejected(env):
    err = rejected(env, "lap_summary_missing_sector.csv", "INVALID_CSV_STRUCTURE", data_type="lap_summary")
    assert "sector_3_s" in str(err)


def test_duplicates_rejected_by_default(env):
    err = rejected(env, "duplicates.csv", "ROW_VALIDATION_FAILED", data_type="sampled_telemetry")
    assert err.details["error_count"] >= 1


def test_duplicates_dropped_when_policy_allows(env):
    m = run(env, "duplicates.csv", data_type="sampled_telemetry", duplicate_policy="drop_exact_duplicates")
    assert m["import_stats"]["duplicates_dropped"] == 1 and times(env, m) == [0.0, 1.0, 2.0]


def test_out_of_order_rejected_by_default(env):
    rejected(env, "out_of_order.csv", "ROW_VALIDATION_FAILED", data_type="sampled_telemetry")


def test_out_of_order_sorted_when_policy_allows(env):
    m = run(env, "out_of_order.csv", data_type="sampled_telemetry", order_policy="sort")
    assert times(env, m) == [0.0, 1.0, 2.0, 3.0]


def test_invalid_unit_rejected_and_lists_supported_units(env):
    err = rejected(env, "time_seconds.csv", "UNIT_NOT_SUPPORTED", units={**UNITS, "time": "fortnights"})
    assert "seconds" in err.details["supported"] and "milliseconds" in err.details["supported"]


def test_non_numeric_lap_value_rejected_with_row_and_column(env):
    err = rejected(env, "lap_summary_non_numeric.csv", "ROW_VALIDATION_FAILED", data_type="lap_summary")
    assert err.details["errors"][0]["row"] == 2 and err.details["errors"][0]["column"] == "sector_1_s"


def test_non_numeric_channel_value_stored_as_missing(env):
    # Req 3.5: a non-numeric sample value is accepted as missing, not interpolated or defaulted.
    m = run(env, "sampled_non_numeric.csv")
    speeds = env[0].telemetry(m["session_id"], "VER")["speed_kph"].tolist()
    assert speeds[0] == 100 and speeds[2] == 110 and speeds[1] != speeds[1]  # NaN


# ---- file at the maximum size ---------------------------------------------------------------------------

def test_file_at_maximum_size_accepted_and_one_byte_over_rejected(env, monkeypatch):
    data = (FIXTURES / "time_seconds.csv").read_bytes()
    monkeypatch.setattr(csv_importer, "CSV_MAX_UPLOAD_BYTES", len(data))
    m = run(env, "time_seconds.csv")  # exactly at the limit
    assert m["data_type"] == "Full telemetry"

    repo, up, e = env
    over = data + b"4,120,90\n"  # larger than the limit
    h = _register(up, e, over, "over.csv")
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(
            repo, {"provider": "csv_upload", "csv": {"file_hash": h, "units": dict(UNITS), "metadata": {**META, "driver": "HAM"}}},
            upload_dir=up)
    assert ei.value.code == "FILE_TOO_LARGE" and len(repo.list_sessions()) == 1
