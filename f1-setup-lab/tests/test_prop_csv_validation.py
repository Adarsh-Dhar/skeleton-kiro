"""Properties 16, 17, 18, 19 and 27: row validation policies, blocking errors, coverage and limits.

Feature: f1-unified-telemetry-import
"""
from __future__ import annotations

import hashlib
from unittest import mock

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from db.engine import create_db_engine
from db.migrations import migrate
from f1lab.integrations.historical import csv_importer as ci
from f1lab.integrations.historical import csv_normalizer as norm
from f1lab.integrations.historical.importer import HistoricalImportError
from f1lab.integrations.historical.repository import HistoricalRepository

SETTINGS = settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
COLS = ["t", "lap", "v", "thr", "g", "d"]
MAP = {"session_time_s": "t", "lap_number": "lap", "speed_kph": "v", "throttle_pct": "thr", "gear": "g", "drs": "d"}

META = {"year": 2024, "event": "Prop event", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}
UNITS = {"time": "seconds", "distance": "metres", "speed": "kph"}


def _frame(rows: list[list]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS).astype(str)


def _row(t, lap=1, v=100, thr=50, g=3, d=0) -> list:
    return [t, lap, v, thr, g, d]


# --- strategies (local) ---------------------------------------------------------------------------------------


@st.composite
def duplicated_samples(draw):
    """(rows, groups): strictly increasing times, some rows repeated verbatim right after themselves."""
    n = draw(st.integers(min_value=2, max_value=12))
    laps = draw(st.lists(st.integers(1, 3), min_size=n, max_size=n).map(sorted))
    rows, groups, dropped = [], 0, 0
    for i in range(n):
        base = _row(i + 1, laps[i], draw(st.integers(0, 300)), draw(st.integers(0, 100)))
        copies = draw(st.sampled_from([1, 1, 2, 3]))
        rows.extend([list(base) for _ in range(copies)])
        if copies > 1:
            groups += 1
            dropped += copies - 1
    if groups == 0:  # guarantee at least one duplicate group
        rows.insert(1, list(rows[0]))
        groups, dropped = 1, 1
    return rows, groups, dropped


@st.composite
def unordered_samples(draw):
    """Distinct times in one lap, shuffled so at least one adjacent pair decreases."""
    times = draw(st.lists(st.integers(0, 1000), min_size=2, max_size=15, unique=True))
    order = draw(st.permutations(times))
    if list(order) == sorted(order):
        order = list(reversed(order))
    return list(order)


cell = st.one_of(st.integers(0, 150).map(str), st.sampled_from(["", " ", "abc", "NaN", "inf", "-5", "7.5"]))


@st.composite
def messy_rows(draw):
    n = draw(st.integers(min_value=1, max_value=25))
    times = draw(st.lists(st.one_of(st.integers(0, 40).map(str), st.just(""), st.just("x")), min_size=n, max_size=n))
    rows = []
    for t in times:
        rows.append([t, draw(st.one_of(st.integers(1, 3).map(str), cell)), draw(cell), draw(cell), draw(cell),
                     draw(st.one_of(cell, st.sampled_from(["0", "1", "8", "5"])))])
    return rows


# --- Property 16 -----------------------------------------------------------------------------------------------


@SETTINGS
@given(duplicated_samples())
def test_property_16_duplicate_policy_outcomes(data):
    """**Validates: Requirements 6.2, 6.7, 6.8**"""
    rows, groups, dropped = data
    frame = _frame(rows)
    kept = ci.validate_rows(frame, MAP, policy={"duplicate_policy": "drop_exact_duplicates"})
    assert kept.ok and kept.error_count == 0
    assert kept.duplicate_group_count == groups
    assert kept.duplicates_dropped == dropped
    assert kept.samples == len(rows) - dropped  # exactly the first row of every group survives
    assert not any(w["reason"] == "duplicate_time_and_lap" for w in kept.warnings)  # groups are exact copies

    rejected = ci.validate_rows(frame, MAP, policy={"duplicate_policy": "reject"})
    assert not rejected.ok and rejected.duplicates_dropped == 0
    dup_errors = [e for e in rejected.errors if e["reason"] == "duplicate_time_and_lap"]
    assert len(dup_errors) == groups == rejected.error_count  # every group counted, nothing else reported
    assert rejected.samples == len(rows)  # nothing removed


# --- Property 17 -----------------------------------------------------------------------------------------------


@SETTINGS
@given(unordered_samples())
def test_property_17_ordering_policy_outcomes(times):
    """**Validates: Requirements 6.3, 6.9, 6.10**"""
    frame = _frame([_row(t) for t in times])
    descents = sum(1 for a, b in zip(times, times[1:]) if b < a)
    assert descents >= 1
    moved = sum(1 for a, b in zip(times, sorted(times)) if a != b)

    srt = ci.validate_rows(frame, MAP, policy={"order_policy": "sort"})
    assert srt.ok and srt.error_count == 0
    assert srt.out_of_order_count == descents
    assert srt.rows_moved == moved and srt.rows_moved > 0
    assert srt.samples == len(times)

    rej = ci.validate_rows(frame, MAP, policy={"order_policy": "reject"})
    assert not rej.ok and rej.rows_moved == 0
    assert rej.out_of_order_count == descents
    assert rej.error_count == descents
    assert all(e["reason"] == "out_of_order" for e in rej.errors)


# --- Property 18 -----------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("prop18")
    engine = create_db_engine(root / "trialforge.db")
    migrate(engine)
    up = root / "csv-uploads"
    up.mkdir()
    return HistoricalRepository(engine), up, engine


def _stage(up, engine, body: str) -> str:
    h = hashlib.sha256(body.encode()).hexdigest()
    (up / f"{h}.csv").write_text(body)
    with engine.begin() as c:
        c.execute(text("INSERT OR REPLACE INTO f1_csv_upload (file_hash, original_filename, size_bytes, uploaded_at) "
                       "VALUES (:h,'p.csv',:s,'2024-01-01')"), {"h": h, "s": len(body)})
    return h


def _payload(h, **over):
    return {"provider": "csv_upload", "csv": {"file_hash": h, "units": dict(UNITS), "metadata": dict(META),
                                              "data_type": "sampled_telemetry", **over}}


def _hist_row_counts(engine) -> dict[str, int]:
    with engine.connect() as c:
        names = [r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'f1_hist_%'"))]
        return {n: c.execute(text(f"SELECT COUNT(*) FROM {n}")).scalar() for n in sorted(names)}


@st.composite
def blocking_files(draw):
    n = draw(st.integers(min_value=3, max_value=450))
    bad = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    dup = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    rows, bad_count, groups = [], 0, 0
    for i in range(n):
        line = f"{i + 1},1,{100 + i % 50},{150 if bad[i] else i % 100}"
        rows.append(line)
        bad_count += bad[i]
        if dup[i] and not bad[i]:
            rows.append(line)  # exact repeat; the default policy rejects it
            groups += 1
    if bad_count + groups == 0:
        rows[0] = "1,1,100,150"
        bad_count = 1
    return "session_time_s,lap,speed_kph,throttle_pct\n" + "\n".join(rows) + f"\n{n + 1000},1,1,1\n", bad_count + groups


@SETTINGS
@given(blocking_files())
def test_property_18_blocking_error_leaves_storage_unchanged(env, data):
    """**Validates: Requirements 6.11**"""
    repo, up, engine = env
    body, expected = data
    h = _stage(up, engine, body)
    before = _hist_row_counts(engine)
    with pytest.raises(HistoricalImportError) as ei:
        ci.import_session_csv(repo, _payload(h), upload_dir=up)
    err = ei.value
    assert err.code == "ROW_VALIDATION_FAILED"
    assert err.details["error_count"] == expected  # the true total of blocking errors
    assert len(err.details["errors"]) == min(expected, 200)
    assert repo.list_sessions() == []
    assert _hist_row_counts(engine) == before and not any(before.values())


# --- Property 19 -----------------------------------------------------------------------------------------------


@SETTINGS
@given(messy_rows(), st.sampled_from(["reject", "drop_exact_duplicates"]), st.sampled_from(["reject", "sort"]))
def test_property_19_coverage_is_internally_consistent(rows, dup, order):
    """**Validates: Requirements 6.12**"""
    rep = ci.validate_rows(_frame(rows), MAP, policy={"duplicate_policy": dup, "order_policy": order})
    assert rep.coverage
    for fld, c in rep.coverage.items():
        assert 0 <= c["count"] <= rep.samples, (fld, c, rep.samples)
        expected = round(c["count"] / rep.samples * 100, 1) if rep.samples else 0.0
        assert c["percent"] == expected, (fld, c, rep.samples)


# --- Property 27 -----------------------------------------------------------------------------------------------


@st.composite
def sized_files(draw):
    rows = draw(st.integers(min_value=1, max_value=60))
    body = "session_time_s,speed_kph\n" + "".join(f"{i},{100 + i}\n" for i in range(rows))
    return body, rows


@SETTINGS
@given(sized_files(), st.integers(min_value=1, max_value=500), st.integers(min_value=1, max_value=70))
def test_property_27_read_chunks_limits(tmp_path_factory, data, byte_slack, chunk):
    """**Validates: Requirements 12.4, 12.5**"""
    body, rows = data
    path = tmp_path_factory.mktemp("lim") / "f.csv"
    path.write_text(body)
    size = path.stat().st_size

    # size limit: raised eagerly, before the iterator is returned, so no row is read
    max_bytes = max(0, size - min(byte_slack, size))
    if max_bytes < size:
        with pytest.raises(ci.CsvLimitError) as ei:
            ci.read_chunks(path, chunk, max_bytes=max_bytes)
        assert ei.value.code == ci.CsvLimitError.FILE_TOO_LARGE and ei.value.limit == max_bytes
        assert str(max_bytes) in str(ei.value)
    assert sum(len(c) for c in ci.read_chunks(path, chunk, max_bytes=size, max_rows=rows)) == rows  # boundary is allowed

    # row limit: rejected with the limit named; a chunk that crosses the limit is never yielded
    if rows > 1:
        limit = rows - 1
        yielded = 0
        with pytest.raises(ci.CsvLimitError) as ei:
            for c in ci.read_chunks(path, chunk, max_rows=limit):
                yielded += len(c)
        assert ei.value.code == ci.CsvLimitError.TOO_MANY_ROWS and ei.value.limit == limit and str(limit) in str(ei.value)
        assert yielded <= limit


@SETTINGS
@given(sized_files(), st.sampled_from(["bytes", "rows"]))
def test_property_27_import_rejects_before_normalizing(env, data, which):
    """**Validates: Requirements 12.4, 12.5**"""
    repo, up, engine = env
    body, rows = data
    if which == "rows" and rows < 2:
        rows_body = body + "99,1\n"
        body, rows = rows_body, rows + 1
    h = _stage(up, engine, body)
    before = _hist_row_counts(engine)
    size = (up / f"{h}.csv").stat().st_size
    limit = size - 1 if which == "bytes" else rows - 1
    name = "CSV_MAX_UPLOAD_BYTES" if which == "bytes" else "CSV_MAX_ROWS"
    with mock.patch.object(ci, name, limit), \
            mock.patch.object(ci, "validate_rows", side_effect=AssertionError("row processed")) as vr, \
            mock.patch.object(norm, "build_telemetry_frame", side_effect=AssertionError("row normalized")) as bt:
        with pytest.raises(HistoricalImportError) as ei:
            ci.import_session_csv(repo, _payload(h), upload_dir=up)
    err = ei.value
    assert err.code == ("FILE_TOO_LARGE" if which == "bytes" else "TOO_MANY_ROWS")
    assert err.details["value"] == limit and str(limit) in str(err)
    assert not vr.called and not bt.called
    assert repo.list_sessions() == [] and _hist_row_counts(engine) == before
