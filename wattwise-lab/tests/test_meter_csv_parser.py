"""Smart-meter CSV parsing (pure parsing logic; the SDK is only needed for the Measurement type)."""

from __future__ import annotations

import pytest

from wattwise.adapters.meter_csv import MAX_ROWS, parse_meter_csv


def test_parses_kw_and_w_columns():
    kw = parse_meter_csv("timestamp,power_kw\n2026-03-03T16:00:00,1.5\n2026-03-03T16:15:00,2.0\n")
    w = parse_meter_csv("timestamp,power_w\n2026-03-03T16:00:00,1500\n2026-03-03T16:15:00,2000\n")
    assert kw.unit == "kW" and w.unit == "W" and [v for _, v in kw.readings] == [1.5, 2.0] and [v for _, v in w.readings] == [1500, 2000]
    assert kw.errors == [] and kw.readings[0][0].tzinfo is not None  # naive timestamps are treated as UTC


def test_plain_power_column_uses_the_unit_argument():
    assert parse_meter_csv("timestamp,power\n2026-03-03T16:00:00,900\n", unit="W").unit == "W"
    assert parse_meter_csv("timestamp,power\n2026-03-03T16:00:00,0.9\n", unit="kW").unit == "kW"
    with pytest.raises(ValueError):
        parse_meter_csv("timestamp,power\n", unit="MW")


def test_missing_columns_and_empty_files_are_reported():
    assert "required columns" in parse_meter_csv("time,watts\n1,2\n").errors[0]
    assert parse_meter_csv("").errors == ["empty file"]
    assert parse_meter_csv("timestamp,power_kw\n").errors == ["no data rows"]


def test_bad_rows_are_skipped_with_file_line_numbers():
    text = "timestamp,power_kw\n2026-03-03T16:00:00,1\nyesterday,2\n2026-03-03T16:30:00,abc\n2026-03-03T16:45:00,-3\n2026-03-03T17:00:00,nan\n2026-03-03T17:15:00,4\n"
    p = parse_meter_csv(text)
    assert [v for _, v in p.readings] == [1.0, 4.0]
    joined = " | ".join(p.errors)
    assert "line 3" in joined and "line 4" in joined and "line 5" in joined and "line 6" in joined and len(p.errors) == 4


def test_bom_header_case_and_zulu_timestamps_are_accepted():
    p = parse_meter_csv("\ufeffTimestamp,Power_KW\n2026-03-03T16:00:00Z,1.0\n")
    assert p.errors == [] and len(p.readings) == 1


def test_row_and_size_limits():
    rows = "\n".join(f"2026-03-03T16:00:{i % 60:02d},1" for i in range(MAX_ROWS + 5))
    p = parse_meter_csv("timestamp,power_kw\n" + rows)
    assert len(p.readings) == MAX_ROWS and any("only the first" in e for e in p.errors)
    big = parse_meter_csv("timestamp,power_kw\n" + "x" * 2_100_000)
    assert big.readings == [] and "larger than" in big.errors[0]
