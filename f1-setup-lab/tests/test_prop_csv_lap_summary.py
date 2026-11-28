"""Task 3.4: property tests for classification and lap-summary rules (f1-unified-telemetry-import, Properties 1-6)."""
from __future__ import annotations

import hashlib
import itertools
import tempfile
from pathlib import Path

import pandas as pd
import pytest
from csv_strategies import invalid_lap_cells, to_csv_text
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
REQ = list(ci.LAP_REQUIRED_COLUMNS)
OPTIONAL = list(ci.LAP_OPTIONAL_COLUMNS)
UNITS = {"time": "seconds", "distance": "metres", "speed": "kph"}
_counter = itertools.count()


# ---- strategies -------------------------------------------------------------------------------------------------

def variant(name: str):
    """Case and surrounding-whitespace variants of a column name."""
    return st.builds(lambda up, a, b: (name.upper() if up else name) if not a and not b else
                     (" " * a) + (name.upper() if up else name) + ("\t" * b),
                     st.booleans(), st.integers(0, 2), st.integers(0, 2))


sector_cell = st.integers(min_value=1, max_value=90000).map(lambda k: f"{k / 1000:.3f}")
driver_cell = st.sampled_from(["", "VER", "HAM", "LEC"])


@st.composite
def lap_table(draw, with_lap_time=None, with_driver=None, with_sector4=None, min_rows=1, max_rows=6):
    """Columns (name -> cells) of a valid Lap_Summary_CSV; optional columns present per flag (drawn when None)."""
    n = draw(st.integers(min_rows, max_rows))
    flags = [draw(st.booleans()) if f is None else f for f in (with_lap_time, with_driver, with_sector4)]
    laps = draw(st.lists(st.integers(1, 200), min_size=n, max_size=n))
    cols: dict[str, list[str]] = {"lap": [str(v) for v in laps]}
    for s in ("sector_1_s", "sector_2_s", "sector_3_s"):
        cols[s] = draw(st.lists(sector_cell, min_size=n, max_size=n))
    if flags[0]:
        cols["lap_time_s"] = draw(st.lists(st.one_of(st.just(""), sector_cell), min_size=n, max_size=n))
    if flags[1]:
        cols["driver"] = draw(st.lists(driver_cell, min_size=n, max_size=n))
    if flags[2]:
        cols["sector_4_s"] = draw(st.lists(st.one_of(st.just(""), sector_cell), min_size=n, max_size=n))
    return cols


# ---- repository helpers -----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def env():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        engine = create_db_engine(root / "trialforge.db")
        migrate(engine)
        up = root / "csv-uploads"
        up.mkdir()
        yield HistoricalRepository(engine), up, engine


def do_import(env, body: str, driver="VER"):
    repo, up, engine = env
    # a unique trailing column keeps every example's file hash distinct (duplicate-file detection is not under test)
    k = next(_counter)
    lines = body.splitlines()
    body = "\n".join([lines[0] + ",run_id"] + [f"{ln},{k}" for ln in lines[1:]]) + "\n"
    h = hashlib.sha256(body.encode()).hexdigest()
    (up / f"{h}.csv").write_text(body)
    with engine.begin() as c:
        c.execute(text("INSERT OR REPLACE INTO f1_csv_upload (file_hash, original_filename, size_bytes, uploaded_at) "
                       "VALUES (:h,'laps.csv',:s,'2024-01-01')"), {"h": h, "s": len(body)})
    meta = {"year": 2024, "event": f"event {next(_counter)}", "circuit": "Imola", "session_type": "FP2",
            "driver": driver, "session_date": "2024-05-18", "timezone": "Europe/Rome"}
    payload = {"provider": "csv_upload", "csv": {"file_hash": h, "units": dict(UNITS), "metadata": meta}}
    return repo, engine, ci.import_session_csv(repo, payload, upload_dir=up)


def write_tmp(body: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "x.csv"
    p.write_text(body)
    return p


# ---- Property 1 -------------------------------------------------------------------------------------------------

NEUTRAL = ["notes", "comment", "team", "tyre", "driver", "lap_time_s", "sector_4_s"]
TIMES = list(ci.TIME_COLUMNS)
CHANNELS = ["distance_m", "speed_kph", "throttle_pct", "brake_pct", "steer_pct", "gear", "rpm", "drs", "lap_number"]


@st.composite
def vary_all(draw, names):
    return [draw(variant(n)) for n in names]


@SETTINGS
@given(data=st.data())
def test_property_1_classification(data):
    """Feature: f1-unified-telemetry-import, Property 1. **Validates: Requirements 2.1, 3.1, 3.8**"""
    extras = data.draw(st.lists(st.sampled_from(NEUTRAL), unique=True, max_size=4))
    shuffle = st.permutations
    # lap summary: all four required columns, any variant, plus extras that are neither time nor channel columns
    cols = data.draw(vary_all(REQ + extras))
    assert ci.classify(data.draw(shuffle(cols))) == ci.LAP_SUMMARY

    # sampled telemetry: a time column and a channel column, not satisfying the lap-summary set
    time = data.draw(st.sampled_from(TIMES))
    chans = data.draw(st.lists(st.sampled_from(CHANNELS), min_size=1, unique=True, max_size=3))
    some_req = data.draw(st.lists(st.sampled_from(REQ), unique=True, max_size=3))  # proper subset of required
    cols = data.draw(vary_all([time] + chans + some_req + extras))
    assert ci.classify(data.draw(shuffle(cols))) == ci.SAMPLED_TELEMETRY

    # both sets present -> ambiguous; neither -> unrecognized
    both = data.draw(vary_all(REQ + [time] + chans))
    assert ci.classify(both) == ci.AMBIGUOUS
    none = data.draw(vary_all(extras + some_req))
    assert ci.classify(none) == ci.UNRECOGNIZED


# ---- Property 2 -------------------------------------------------------------------------------------------------

@SETTINGS
@given(data=st.data())
def test_property_2_missing_columns_named_exactly(data):
    """Feature: f1-unified-telemetry-import, Property 2. **Validates: Requirements 2.4**"""
    present = data.draw(st.lists(st.sampled_from(REQ), unique=True, max_size=3))
    extras = data.draw(st.lists(st.sampled_from(NEUTRAL), unique=True, max_size=4))
    header = data.draw(vary_all(present + extras))
    body = ",".join(header) + "\n" + ",".join(["1"] * len(header)) + "\n"
    res = ci.validate_lap_summary(write_tmp(body))
    assert not res.ok
    assert res.missing_columns == [c for c in REQ if c not in present]
    assert res.first_error is None


# ---- Property 3 -------------------------------------------------------------------------------------------------

@SETTINGS
@given(cols=lap_table())
def test_property_3_optional_columns_round_trip(cols):
    """Feature: f1-unified-telemetry-import, Property 3. **Validates: Requirements 2.2**"""
    body = to_csv_text(cols)
    df = pd.read_csv(pd.io.common.StringIO(body), dtype=str, keep_default_na=False)
    frame = norm.build_lap_frame(df, ci.suggest_mapping(list(df.columns)), UNITS)
    n = len(df)
    assert len(frame) == n
    assert [int(v) for v in frame["lap_number"]] == [int(v) for v in cols["lap"]]
    for s in ("sector_1_s", "sector_2_s", "sector_3_s"):
        assert list(frame[s]) == pytest.approx([float(v) for v in cols[s]])
    for f in ("lap_time_s", "sector_4_s"):
        if f in cols:
            expect = [float(v) if v != "" else None for v in cols[f]]
            for got, want in zip(frame[f], expect):
                assert (pd.isna(got) if want is None else got == pytest.approx(want))
        else:
            assert frame[f].isna().all()
    if "driver" in cols:
        assert list(frame["driver"]) == [v or None for v in cols["driver"]]
    else:
        assert frame["driver"].isna().all()


# ---- Property 4 -------------------------------------------------------------------------------------------------

@SETTINGS
@given(cols=lap_table(with_lap_time=False, with_driver=False))
def test_property_4_missing_lap_time_is_never_sector_sum(env, cols):
    """Feature: f1-unified-telemetry-import, Property 4. **Validates: Requirements 2.3**"""
    repo, _, m = do_import(env, to_csv_text(cols))
    laps = repo.laps(m["session_id"])
    assert len(laps) == len(cols["lap"])
    assert laps["lap_time_s"].isna().all()
    assert laps[["sector_1_s", "sector_2_s", "sector_3_s"]].notna().all().all()  # a sum was available


# ---- Property 5 -------------------------------------------------------------------------------------------------

bad_sector = st.sampled_from(["0", "0.0", "-1", "-2.5", "abc", "n/a", "NaN", "inf", "-inf", "1,2x"])


@SETTINGS
@given(data=st.data())
def test_property_5_first_bad_cell_rejected(env, data):
    """Feature: f1-unified-telemetry-import, Property 5. **Validates: Requirements 2.5**"""
    cols = data.draw(lap_table(with_lap_time=True, with_driver=False, with_sector4=True, min_rows=1, max_rows=6))
    n = len(cols["lap"])
    target = data.draw(st.sampled_from(["lap", "sector_1_s", "sector_2_s", "sector_3_s", "lap_time_s", "sector_4_s"]))
    row = data.draw(st.integers(0, n - 1))
    if target == "lap":
        bad = data.draw(invalid_lap_cells)
    elif target in REQ:
        bad = data.draw(st.one_of(bad_sector, st.just("")))
    else:  # blank optional cells are legitimate missing values
        bad = data.draw(bad_sector)
    cols = {k: list(v) for k, v in cols.items()}
    cols[target][row] = bad
    body = to_csv_text(cols)

    res = ci.validate_lap_summary(write_tmp(body))
    assert not res.ok and res.first_error is not None
    assert (res.first_error.row, res.first_error.column) == (row + 1, target)

    repo = env[0]
    before = {s["session_id"] for s in repo.list_sessions()}
    with pytest.raises(HistoricalImportError) as ei:
        do_import(env, body)
    err = ei.value.details["errors"][0]
    assert ei.value.code == "ROW_VALIDATION_FAILED" and (err["row"], err["column"]) == (row + 1, target)
    assert {s["session_id"] for s in repo.list_sessions()} == before


# ---- Property 6 -------------------------------------------------------------------------------------------------

@SETTINGS
@given(cols=lap_table())
def test_property_6_lap_summary_session_has_no_telemetry(env, cols):
    """Feature: f1-unified-telemetry-import, Property 6. **Validates: Requirements 2.6, 2.7, 2.8**"""
    selected = "VER"
    if "driver" in cols:
        cols = {k: list(v) for k, v in cols.items()}
        cols["driver"] = ["VER"] * len(cols["driver"])  # the selected driver must match at least one row
    repo, engine, m = do_import(env, to_csv_text(cols), driver=selected)
    sid = m["session_id"]
    stored = repo.manifest(sid)
    assert stored["data_type"] == "Lap summaries" == m["data_type"]
    assert stored["channels"]["telemetry"] == {}
    with engine.connect() as c:
        tel_frames = c.execute(text("SELECT COUNT(*) FROM f1_hist_frame WHERE session_id=:s AND kind='telemetry'"), {"s": sid}).scalar()
    assert tel_frames == 0
    for drv in {d["code"] for d in stored["drivers"]} | {"VER"}:
        assert repo.telemetry(sid, drv).empty
