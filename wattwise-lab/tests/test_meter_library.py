"""Tasks 12 (WATTWISE-K2/K3): historical meter library and configurable CSV interpretation.

All CSVs here are small hand-written fixtures that test the parsing rules; none is real household data.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from db import create_db_engine
from wattwise.adapters.meter_import import ImportOptions, MeterImportError, interpret


def _csv(header, rows):
    return header + "\n" + "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------------------------- parser
def test_power_kw_and_w_with_auto_detection():
    r = interpret(_csv("timestamp,power_w", ["2026-01-01T00:00:00Z,1000", "2026-01-01T00:15:00Z,2000"]))
    assert r.options["resolved"] == {"timestamp_column": "timestamp", "value_column": "power_w", "reading_kind": "power", "unit": "W", "delimiter": ","}
    assert [x.kw for x in r.readings] == [1.0, 2.0] and [x.kwh for x in r.readings] == [0.25, 0.5]
    assert any("converted from W" in w for w in r.warnings)


def test_configurable_columns_semicolon_and_decimal_comma():
    text = _csv("Datum;Verbrauch", ["01/02/2026 00:00;0,5", "01/02/2026 00:30;1,25"])
    with pytest.raises(MeterImportError, match="no timestamp column"):
        interpret(text)
    r = interpret(text, ImportOptions(timestamp_column="Datum", value_column="Verbrauch", reading_kind="interval_energy", unit="kWh",
                                      timestamp_format="%d/%m/%Y %H:%M", decimal=",", timezone="Europe/Berlin"))
    assert r.options["resolved"]["delimiter"] == ";"
    assert [x.kwh for x in r.readings] == [0.5, 1.25] and r.readings[1].kw == pytest.approx(2.5)
    assert r.readings[0].start_utc == datetime(2026, 1, 31, 23, 0, tzinfo=UTC)  # 00:00 CET = 23:00 UTC the day before


def test_iso_offsets_are_converted_and_naive_times_use_the_timezone():
    r = interpret(_csv("timestamp,power_kw", ["2026-06-01T10:00:00+02:00,1", "2026-06-01T08:15:00Z,1"]), ImportOptions(timezone="America/New_York"))
    assert [x.start_utc.hour for x in r.readings] == [8, 8] and [x.start_utc.minute for x in r.readings] == [0, 15]
    r = interpret(_csv("timestamp,power_kw", ["2026-06-01T10:00:00,1", "2026-06-01T10:15:00,1"]), ImportOptions(timezone="America/New_York"))
    assert r.readings[0].start_utc == datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    assert any("America/New_York local time" in w for w in r.warnings)
    with pytest.raises(MeterImportError, match="unknown timezone"):
        interpret("timestamp,power_kw\n2026-01-01T00:00,1\n", ImportOptions(timezone="Mars/Olympus"))


def test_dst_fall_back_and_spring_forward():
    fall = _csv("timestamp,energy_kwh", ["2025-10-26 00:30,1", "2025-10-26 01:00,1", "2025-10-26 01:30,1", "2025-10-26 01:00,1", "2025-10-26 01:30,1", "2025-10-26 02:00,1"])
    r = interpret(fall, ImportOptions(timezone="Europe/London", interval_minutes=30))
    starts = [x.start_utc.strftime("%H:%M") for x in r.readings]
    assert starts == ["23:30", "00:00", "00:30", "01:00", "01:30", "02:00"]  # 6 distinct UTC intervals, nothing dropped as duplicate
    assert sum("ambiguous local time" in e["reason"] for e in r.row_log) == 4
    spring = _csv("timestamp,energy_kwh", ["2026-03-29 00:30,1", "2026-03-29 01:30,1", "2026-03-29 02:00,1"])
    r = interpret(spring, ImportOptions(timezone="Europe/London", interval_minutes=30))
    assert len(r.readings) == 2 and any("nonexistent local time" in e["reason"] for e in r.row_log)


def test_duplicates_out_of_order_and_bad_rows_are_logged():
    text = _csv("timestamp,power_kw", ["2026-01-01T00:30Z,2", "2026-01-01T00:00Z,1", "2026-01-01T00:15Z,1.5", "2026-01-01T00:15Z,9",
                                        "not-a-time,1", "2026-01-01T00:45Z,abc", "2026-01-01T01:00Z,-3", ","])
    r = interpret(text)
    assert [x.kw for x in r.readings] == [1.0, 1.5, 2.0]
    reasons = " | ".join(e["reason"] for e in r.row_log)
    for s in ("out of time order", "duplicate timestamp", "values differ", "invalid timestamp", "not a number", "negative value", "blank row"):
        assert s in reasons, s
    assert r.rows_total == 8 and r.rows_skipped == 5


def test_gaps_are_reported_not_filled():
    r = interpret(_csv("timestamp,power_kw", ["2026-01-01T00:00Z,1", "2026-01-01T00:15Z,1", "2026-01-01T01:15Z,1"]))
    assert len(r.readings) == 3 and r.gaps[0]["missing_intervals"] == 3
    assert r.summary()["missing_intervals"] == 3


def test_interval_end_timestamps():
    r = interpret(_csv("timestamp,energy_kwh", ["2026-01-01T00:30Z,0.5", "2026-01-01T01:00Z,0.5"]), ImportOptions(timestamp_marks="end"))
    assert r.readings[0].start_utc == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_cumulative_register_reset_and_rollover():
    reset = _csv("timestamp,meter_reading_kwh", ["2026-01-01T00:00Z,100", "2026-01-01T01:00Z,101", "2026-01-01T02:00Z,3", "2026-01-01T03:00Z,4.5"])
    r = interpret(reset)
    assert [x.kwh for x in r.readings] == [1.0, 1.5]  # the reset interval is not counted ...
    assert all(x.kwh >= 0 for x in r.readings)  # ... and never negative
    assert any("meter reset" in e["reason"] for e in r.row_log) and any("reset" in (g.get("note") or "") for g in r.gaps)
    roll = _csv("timestamp,meter_reading_kwh", ["2026-01-01T00:00Z,99998", "2026-01-01T01:00Z,99999.5", "2026-01-01T02:00Z,1.0"])
    r = interpret(roll, ImportOptions(rollover_kwh=100000))
    assert [round(x.kwh, 6) for x in r.readings] == [1.5, 1.5] and "rollover" in r.readings[1].flags
    gap = _csv("timestamp,cumulative_kwh", ["2026-01-01T00:00Z,10", "2026-01-01T01:00Z,11", "2026-01-01T04:00Z,14"])
    r = interpret(gap)
    assert r.readings[1].interval_minutes == 180 and "spans_gap" in r.readings[1].flags and r.readings[1].kw == pytest.approx(1.0)


def test_resampling_flags_partial_buckets():
    rows = [f"2026-01-01T00:{m:02d}Z,{1 + i}" for i, m in enumerate((0, 15, 30, 45))] + ["2026-01-01T01:00Z,10"]
    r = interpret(_csv("timestamp,energy_kwh", rows), ImportOptions(resample_minutes=60))
    assert [x.kwh for x in r.readings] == [10.0, 10.0] and r.readings[1].flags == ["partial:25%"]
    with pytest.raises(MeterImportError, match="finer"):
        interpret(_csv("timestamp,energy_kwh", rows), ImportOptions(resample_minutes=5))


def test_ambiguous_or_mismatched_columns_need_explicit_options():
    with pytest.raises(MeterImportError, match="cannot tell"):
        interpret(_csv("timestamp,reading", ["2026-01-01T00:00Z,1", "2026-01-01T00:15Z,2"]), ImportOptions(value_column="reading"))
    with pytest.raises(MeterImportError, match="does not fit"):
        interpret(_csv("timestamp,power_kw", ["2026-01-01T00:00Z,1"]), ImportOptions(unit="kWh"))
    with pytest.raises(MeterImportError, match="not in the file"):
        interpret(_csv("timestamp,power_kw", ["2026-01-01T00:00Z,1"]), ImportOptions(value_column="nope"))


def test_limits_are_kept():
    with pytest.raises(MeterImportError, match="larger than"):
        interpret("timestamp,power_kw\n" + "2026-01-01T00:00Z,1\n" * 110_000)


# ---------------------------------------------------------------------------------------------- API + storage
DEMO = None


def _demo(client):
    return client.get("/api/demo/meter.csv").text


def test_preview_stores_nothing_and_import_persists(client, db_path):
    csv = _demo(client)
    p = client.post("/api/meter/import/preview", json={"csv": csv, "file_name": "demo.csv"}).json()
    assert p["stored"] is False and p["columns"] == ["timestamp", "power_kw"] and p["summary"]["rows_used"] == 96
    assert p["readings_preview"][0]["original_ts"] == "2026-03-03T16:00:00" and p["provenance"] == "historical_measured"
    assert client.get("/api/meter/imports").json() == []
    m = client.post("/api/meter/import", json={"csv": csv, "file_name": "demo.csv", "source": "bundled demo (synthetic)"})
    assert m.status_code == 201, m.text
    m = m.json()
    assert m["rows_used"] == 96 and m["timezone"] == "UTC" and m["reading_kind"] == "power"
    assert client.post("/api/meter/import", json={"csv": csv, "file_name": "demo.csv"}).status_code == 409  # same file + options
    assert client.post("/api/meter/import", json={"csv": csv, "file_name": "demo.csv", "force": True}).status_code == 201
    rd = client.get(f"/api/meter/imports/{m['import_id']}/readings", params={"limit": 10}).json()
    assert rd["count"] == 10 and rd["total"] == 96 and rd["readings"][0]["kw"] == pytest.approx(0.397)
    with create_db_engine(db_path).connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM ww_meter_reading WHERE import_id=:i"), {"i": m["import_id"]}).scalar() == 96
        assert c.execute(text("SELECT COUNT(*) FROM measurements")).scalar() == 0  # never mixed into experiment measurements
    assert client.delete(f"/api/meter/imports/{m['import_id']}").status_code == 200
    assert client.get(f"/api/meter/imports/{m['import_id']}").status_code == 404
    with create_db_engine(db_path).connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM ww_meter_reading WHERE import_id=:i"), {"i": m["import_id"]}).scalar() == 0


def test_import_errors_are_422(client):
    r = client.post("/api/meter/import/preview", json={"csv": "a,b\n1,2\n", "options": {}})
    assert r.status_code == 422 and "timestamp" in r.json()["detail"][0]["message"]
    r = client.post("/api/meter/import/preview", json={"csv": "timestamp,power_kw\n2026-01-01T00:00Z,1\n", "options": {"bogus": 1}})
    assert r.status_code == 422 and "unknown import option" in r.json()["detail"][0]["message"]


def test_existing_preview_and_run_attach_routes_unchanged(client):
    r = client.post("/api/meter/preview", json={"csv": _demo(client), "unit": "kW"})
    assert r.status_code == 200 and r.json()["reading_kind"] == "imported"
