"""Row validation, policies, coverage and chunked reading in csv_importer.

Validates: Requirements 3.9, 6.1-6.12, 12.1, 12.4, 12.5
"""
from __future__ import annotations

import pandas as pd
import pytest

from f1lab.integrations.historical import csv_importer as ci

MAP = {"session_time_s": "t", "lap_number": "lap", "speed_kph": "v", "throttle_pct": "thr", "gear": "g", "drs": "d"}


def _frame(rows):
    return pd.DataFrame(rows, columns=["t", "lap", "v", "thr", "g", "d"]).astype(str)


def test_clean_rows_have_full_coverage():
    rep = ci.validate_rows(_frame([[0, 1, 100, 50, 3, 0], [1, 1, 110, 60, 4, 0], [2, 1, 120, 70, 4, 8]]), MAP)
    assert rep.ok and rep.error_count == 0 and rep.samples == 3
    assert rep.coverage["speed_kph"] == {"count": 3, "percent": 100.0}
    assert rep.coverage["rpm"] == {"count": 0, "percent": 0.0}


def test_missing_counts_non_numeric_and_skipped_time_rows():
    rep = ci.validate_rows(_frame([[0, 1, "", 50, 3, 0], [" ", 1, "abc", 50, 3, 0], [2, 1, 120, 50, 3, 0]]), MAP)
    assert rep.missing_counts["v"] == 1 and rep.missing_counts["t"] == 1
    assert rep.skipped_time_rows == 1 and rep.samples == 2 and rep.ok
    assert any(w["reason"] == "non_numeric" and w["row"] == 2 and w["value"] == "abc" for w in rep.warnings)
    assert rep.coverage["speed_kph"] == {"count": 1, "percent": 50.0}


def test_range_gear_and_drs_errors_are_blocking():
    rep = ci.validate_rows(_frame([[0, 1, 1, 101, 3.5, 5], [1, 1, 1, 50, 3, 8]]), MAP)
    reasons = {(e["row"], e["column"], e["reason"]) for e in rep.errors}
    assert reasons == {(1, "thr", "out_of_range"), (1, "g", "non_integer_gear"), (1, "d", "drs_not_allowed")}
    assert not rep.ok


def test_duplicate_policies():
    rows = [[0, 1, 1, 1, 1, 0], [0, 1, 1, 1, 1, 0], [1, 1, 1, 1, 1, 0]]
    rejected = ci.validate_rows(_frame(rows), MAP, policy={"duplicate_policy": "reject"})
    assert not rejected.ok and rejected.duplicate_groups == [[1, 2]] and rejected.duplicate_rows_affected == 2
    dropped = ci.validate_rows(_frame(rows), MAP, policy={"duplicate_policy": "drop_exact_duplicates"})
    assert dropped.ok and dropped.duplicates_dropped == 1 and dropped.samples == 2


def test_order_policies():
    rows = [[0, 1, 1, 1, 1, 0], [2, 1, 1, 1, 1, 0], [1, 1, 1, 1, 1, 0]]
    rejected = ci.validate_rows(_frame(rows), MAP, policy={"order_policy": "reject"})
    assert not rejected.ok and rejected.out_of_order_rows == [3]
    sorted_ = ci.validate_rows(_frame(rows), MAP, policy={"order_policy": "sort"})
    assert sorted_.ok and sorted_.rows_moved == 2 and sorted_.out_of_order_count == 1


def test_error_list_capped_with_total_count():
    rows = [[i, 1, 1, 500, 1, 0] for i in range(450)]
    rep = ci.validate_rows(_frame(rows), MAP)
    assert len(rep.errors) == 200 and rep.error_count == 450 and not rep.ok


def test_no_usable_time_is_blocking():
    rep = ci.validate_rows(_frame([["x", 1, 1, 1, 1, 0]]), MAP)
    assert not rep.ok and rep.errors[0]["reason"] == "time column has no usable values"


def test_coverage_percent_rounds_to_one_decimal():
    rows = [[0, 1, 1, 1, 1, 0], [1, 1, "", 1, 1, 0], [2, 1, "", 1, 1, 0]]
    assert ci.validate_rows(_frame(rows), MAP).coverage["speed_kph"]["percent"] == 33.3


def test_read_chunks_size_and_row_limits(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("t,v\n" + "\n".join(f"{i},1" for i in range(25)) + "\n")
    chunks = list(ci.read_chunks(p, chunksize=10))
    assert [len(c) for c in chunks] == [10, 10, 5] and chunks[2].index[0] == 20
    assert max(len(c) for c in ci.read_chunks(p, chunksize=10**9)) <= ci.CHUNK_ROWS
    with pytest.raises(ci.CsvLimitError) as big:  # raised before any iteration
        ci.read_chunks(p, max_bytes=10)
    assert big.value.code == "FILE_TOO_LARGE" and big.value.limit == 10
    with pytest.raises(ci.CsvLimitError) as many:
        list(ci.read_chunks(p, chunksize=10, max_rows=24))
    assert many.value.code == "TOO_MANY_ROWS" and many.value.limit == 24
