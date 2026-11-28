"""Properties 20-24: session metadata, driver filtering, atomicity, provenance and duplicate detection (task 3.6).

Drives ``import_session_csv`` against a real SQLite repository, as in test_csv_import_session.py.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import event, text

from db.engine import create_db_engine
from db.migrations import migrate
from f1lab.integrations.historical import csv_importer
from f1lab.integrations.historical.csv_importer import METADATA_ITEMS, validate_session_metadata
from f1lab.integrations.historical.importer import (
    DRIVER_NOT_FOUND,
    DUPLICATE_KEY_CONFLICT,
    DUPLICATE_SESSION,
    METADATA_INVALID,
    HistoricalImportError,
)
from f1lab.integrations.historical.repository import HistoricalRepository

CFG = settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))

NULLABLE = ("year", "event", "circuit", "session_type", "driver", "session_date", "timezone")
ZONES = ["Europe/Rome", "UTC", "America/New_York", "Asia/Tokyo", "Australia/Melbourne"]
EVENTS = ["Imola testing", "Monza", "Spa", "Suzuka", "Interlagos"]
STYPES = ["FP1", "FP2", "FP3", "Q", "R"]
DRIVERS = ["VER", "HAM", "LEC", "ver", "NOR"]
BASE_META = {"year": 2024, "event": "Imola testing", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
             "session_date": "2024-05-18", "timezone": "Europe/Rome"}
UNITS = {"time": "seconds", "distance": "metres"}

_TEXT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ0123456789 -_."
free_text = st.one_of(
    st.text(_TEXT_ALPHABET, min_size=1, max_size=200).filter(lambda s: s == s.strip() and s),
    st.just("a" * 200),
)
filenames = st.text("abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=40).map(lambda s: s + ".csv")


# ----------------------------------------------------------------------------- helpers
@contextmanager
def make_env():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        engine = create_db_engine(root / "trialforge.db")
        migrate(engine)
        up = root / "csv-uploads"
        up.mkdir()
        try:
            yield HistoricalRepository(engine), up, engine
        finally:
            engine.dispose()


def lap_csv(rows: list[tuple[str, float]]) -> str:
    """Lap summary text; each row is (driver, lap time seed) so different seeds give different bytes."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["lap", "sector_1_s", "sector_2_s", "sector_3_s", "lap_time_s", "driver"])
    for i, (drv, seed) in enumerate(rows, start=1):
        s1, s2, s3 = 20 + seed, 30 + seed, 25 + seed
        w.writerow([i, s1, s2, s3, s1 + s2 + s3, drv])
    return buf.getvalue()


def tel_csv(rows: list[tuple[str, float]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["session_time_s", "lap", "speed_kph", "driver"])
    counters: dict[str, int] = {}
    for drv, seed in rows:
        n = counters[drv] = counters.get(drv, 0) + 1
        w.writerow([n * 0.1, 1, 100 + seed + n % 7, drv])
    return buf.getvalue()


def stage(up: Path, engine, body: str, name: str = "upload.csv") -> str:
    h = hashlib.sha256(body.encode()).hexdigest()
    (up / f"{h}.csv").write_text(body)
    with engine.begin() as c:
        c.execute(text("INSERT OR REPLACE INTO f1_csv_upload (file_hash, original_filename, size_bytes, uploaded_at) "
                       "VALUES (:h,:n,:s,'2024-01-01')"), {"h": h, "n": name, "s": len(body)})
    return h


def payload(h, metadata=None, units=None, **over):
    csv_ = {"file_hash": h, "units": dict(UNITS if units is None else units),
            "metadata": dict(BASE_META if metadata is None else metadata), **over}
    return {"provider": "csv_upload", "csv": csv_}


def snapshot(engine) -> dict[str, list]:
    """Full contents of every f1_hist_* table."""
    out = {}
    with engine.connect() as c:
        names = [r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'f1_hist_%' ORDER BY name"))]
        for n in names:
            out[n] = sorted(map(repr, c.execute(text(f"SELECT * FROM {n}")).all()))
    return out


def session_rows(engine, sid: str) -> int:
    with engine.connect() as c:
        names = [r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'f1_hist_%'"))]
        total = 0
        for n in names:
            total += c.execute(text(f"SELECT COUNT(*) FROM {n} WHERE session_id=:s"), {"s": sid}).scalar()
        return total


# ----------------------------------------------------------------------------- Property 20
@st.composite
def valid_metadata(draw):
    """(metadata dict with markers, units, filename): satisfies every rule, all 11 items supplied or marked."""
    meta: dict = {
        "year": draw(st.integers(1950, 2100)), "event": draw(free_text), "circuit": draw(free_text),
        "session_type": draw(free_text), "driver": draw(free_text),
        "session_date": draw(st.dates(min_value=__import__("datetime").date(1950, 1, 1),
                                      max_value=__import__("datetime").date(2100, 12, 31))).isoformat(),
        "timezone": draw(st.sampled_from(ZONES)),
    }
    units = {"time": draw(st.sampled_from(["seconds", "milliseconds"])),
             "distance": draw(st.sampled_from(["metres", "kilometres", "feet"]))}
    marked = draw(st.sets(st.sampled_from(NULLABLE + ("time_unit", "distance_unit"))))
    for item in marked:
        if item in meta:
            meta[item] = draw(st.sampled_from([None, "", "   "]))  # absent in every form
        elif item == "time_unit":
            del units["time"]
        else:
            del units["distance"]
    meta["unavailable"] = sorted(marked)
    return meta, units, draw(filenames)


def expected_metadata(meta, units, filename) -> dict:
    marked = set(meta["unavailable"])
    out = {"provider": "User CSV"}
    for item in METADATA_ITEMS[1:]:
        if item in marked:
            out[item] = None
        elif item == "original_filename":
            out[item] = filename
        elif item == "time_unit":
            out[item] = units["time"]
        elif item == "distance_unit":
            out[item] = units["distance"]
        else:
            out[item] = meta[item]
    out["unavailable"] = [i for i in METADATA_ITEMS if i in marked]
    return out


@CFG
@given(valid_metadata())
def test_p20_valid_metadata_imports_and_is_stored_verbatim(case):
    """**Validates: Requirements 7.1, 7.3, 7.4, 7.5, 7.9**"""
    meta, units, filename = case
    marked = set(meta["unavailable"])
    driver = meta["driver"] if "driver" not in marked else "VER"
    rows = [(driver, 0.0), (driver, 0.5)]
    with make_env() as (repo, up, engine):
        h = stage(up, engine, lap_csv(rows), filename)
        m = csv_importer.import_session_csv(repo, payload(h, meta, units), upload_dir=up)
        want = expected_metadata(meta, units, filename)
        assert m["metadata"] == want
        assert repo.manifest(m["session_id"])["metadata"] == want
        with engine.connect() as c:
            assert json.loads(c.execute(text("SELECT metadata_json FROM f1_hist_session")).scalar()) == want
        assert len(want) == 12  # 11 items + marker list


@st.composite
def invalid_metadata(draw):
    """(metadata, units, item that must be named) with exactly one rule violated."""
    meta = dict(BASE_META)
    units = dict(UNITS)
    kind = draw(st.sampled_from(["year", "date", "tz", "text_long", "text_empty", "unit", "missing", "bad_marker"]))
    if kind == "year":
        meta["year"] = draw(st.one_of(st.integers(max_value=1949), st.integers(min_value=2101), st.just(1.5),
                                      st.sampled_from(["abc", "20x4", "-3"])))
        return meta, units, "year"
    if kind == "date":
        meta["session_date"] = draw(st.sampled_from(["2024-13-01", "2024-02-30", "24-1-1", "not a date", "2024/05/18",
                                                      "2023-02-29", "20240518"]))
        return meta, units, "session_date"
    if kind == "tz":
        meta["timezone"] = draw(st.sampled_from(["Mars/Base", "Not/AZone", "Europe/Atlantis", "GMT+99", "nowhere"]))
        return meta, units, "timezone"
    if kind in ("text_long", "text_empty"):
        item = draw(st.sampled_from(["event", "circuit", "session_type", "driver"]))
        meta[item] = ("x" * draw(st.integers(201, 260))) if kind == "text_long" else draw(st.sampled_from(["", " ", "   \t "]))
        return meta, units, item
    if kind == "unit":
        which = draw(st.sampled_from(["time", "distance"]))
        units[which] = draw(st.sampled_from(["fortnights", "parsecs", "", "SECONDS!", "furlongs"]))
        return meta, units, f"{which}_unit"
    if kind == "missing":
        item = draw(st.sampled_from(NULLABLE))
        del meta[item]
        return meta, units, item
    meta["unavailable"] = [draw(st.sampled_from(["bogus", "provider"]))]
    return meta, units, meta["unavailable"][0]


@CFG
@given(invalid_metadata())
def test_p20_invalid_metadata_rejected_naming_the_item(case):
    """**Validates: Requirements 7.1, 7.3, 7.4, 7.5**"""
    meta, units, item = case
    with make_env() as (repo, up, engine):
        h = stage(up, engine, lap_csv([("VER", 0.0), ("VER", 1.0)]))
        with pytest.raises(HistoricalImportError) as ei:
            csv_importer.import_session_csv(repo, payload(h, meta, units), upload_dir=up)
        err = ei.value
        if item.endswith("_unit"):  # unit outside the supported set is rejected before metadata, naming the field
            assert err.code == "UNIT_NOT_SUPPORTED" and err.details["field"] == item.removesuffix("_unit")
            assert err.details["supported"]
        else:
            assert err.code == METADATA_INVALID
            named = set(err.details["missing"]) | {i["item"] for i in err.details["invalid"]}
            assert item in named
            for i in err.details["invalid"]:
                assert i["rule"]
        assert repo.list_sessions() == [] and snapshot(engine)["f1_hist_session"] == []


@CFG
@given(invalid_metadata().filter(lambda c: not c[2].endswith("_unit")))
def test_p20_validate_session_metadata_names_every_violation(case):
    """**Validates: Requirements 7.3, 7.4, 7.5**"""
    meta, units, item = case
    with pytest.raises(HistoricalImportError) as ei:
        validate_session_metadata(meta, "f.csv", units)
    assert ei.value.code == METADATA_INVALID
    assert item in set(ei.value.details["missing"]) | {i["item"] for i in ei.value.details["invalid"]}


# ----------------------------------------------------------------------------- Property 21
@CFG
@given(rows=st.lists(st.sampled_from(DRIVERS), min_size=1, max_size=25), selected=st.sampled_from(DRIVERS + ["ZZZ", "Ver"]),
       kind=st.sampled_from(["lap_summary", "sampled_telemetry"]))
def test_p21_driver_filter_is_exact_and_exhaustive(rows, selected, kind):
    """**Validates: Requirements 7.6, 7.7**"""
    matching = sum(1 for r in rows if r == selected)
    body = (lap_csv if kind == "lap_summary" else tel_csv)([(r, 0.0) for r in rows])
    meta = {**BASE_META, "driver": selected}
    with make_env() as (repo, up, engine):
        h = stage(up, engine, body)
        if matching == 0:
            with pytest.raises(HistoricalImportError) as ei:
                csv_importer.import_session_csv(repo, payload(h, meta, data_type=kind), upload_dir=up)
            assert ei.value.code == DRIVER_NOT_FOUND
            assert repo.list_sessions() == [] and all(v == [] for v in snapshot(engine).values())
            return
        m = csv_importer.import_session_csv(repo, payload(h, meta, data_type=kind), upload_dir=up)
        st_ = m["import_stats"]
        assert st_["rows_total"] == len(rows)
        assert st_["rows_imported"] == matching
        assert st_["rows_excluded"] == len(rows) - matching
        assert st_["rows_imported"] + st_["rows_excluded"] == st_["rows_total"]
        sid = m["session_id"]
        if kind == "lap_summary":
            laps = repo.laps(sid)
            assert len(laps) == matching and set(laps["driver"]) == {selected}
        else:
            assert len(repo.telemetry(sid, selected)) == matching


# ----------------------------------------------------------------------------- Property 22
@contextmanager
def fail_on_statement(engine, n: int | None, final_update: bool):
    """Raise inside the database layer on the n-th statement, or on the status='complete' UPDATE."""
    state = {"i": 0, "fired": False}

    def before(conn, cursor, statement, parameters, context, executemany):
        state["i"] += 1
        hit = (final_update and statement.lstrip().upper().startswith("UPDATE F1_HIST_SESSION")) or \
              (n is not None and state["i"] == n)
        if hit:
            state["fired"] = True
            raise RuntimeError("injected failure")

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield state
    finally:
        event.remove(engine, "before_cursor_execute", before)


@CFG
@given(kind=st.sampled_from(["lap_summary", "sampled_telemetry"]), n=st.integers(1, 40), final=st.booleans(),
       cancel_at=st.one_of(st.none(), st.integers(1, 12)))
def test_p22_failure_before_commit_leaves_no_rows(kind, n, final, cancel_at):
    """**Validates: Requirements 1.8, 1.9, 8.1, 8.2**"""
    maker = lap_csv if kind == "lap_summary" else tel_csv
    with make_env() as (repo, up, engine):
        h0 = stage(up, engine, maker([("VER", 0.0), ("VER", 1.0), ("HAM", 2.0)]), "old.csv")
        old = csv_importer.import_session_csv(repo, payload(h0, {**BASE_META, "event": "Monza"}, data_type=kind), upload_dir=up)
        before = snapshot(engine)
        meta = {**BASE_META, "driver": None, "unavailable": ["driver"]}  # all drivers: several frames are written
        h = stage(up, engine, maker([("VER", 5.0), ("HAM", 6.0), ("LEC", 7.0), ("VER", 8.0)]), "new.csv")
        calls = {"i": 0}

        def cancel():
            calls["i"] += 1
            return cancel_at is not None and calls["i"] == cancel_at

        failed = False
        with fail_on_statement(engine, None if final else n, final) as state:
            try:
                new = csv_importer.import_session_csv(repo, payload(h, meta, data_type=kind), should_cancel=cancel, upload_dir=up)
            except Exception:
                failed = True
        if failed:
            assert snapshot(engine) == before
            assert repo.exists(old["session_id"]) and len(repo.list_sessions()) == 1
            assert repo.find_by_file_hash(h) is None
        else:
            assert not state["fired"]
            assert repo.exists(new["session_id"]) and len(repo.list_sessions()) == 2
            assert session_rows(engine, new["session_id"]) > 0 and session_rows(engine, old["session_id"]) > 0
        if final:
            assert failed  # the status='complete' write always happens, so the import must fail with nothing left behind


# ----------------------------------------------------------------------------- Property 23
@CFG
@given(valid_metadata(), st.sampled_from(["lap_summary", "sampled_telemetry"]), st.sampled_from(["reject", "drop_exact_duplicates"]))
def test_p23_stored_provenance_round_trips(case, kind, dup_policy):
    """**Validates: Requirements 7.8, 8.3**"""
    meta, units, filename = case
    marked = set(meta["unavailable"])
    driver = meta["driver"] if "driver" not in marked else "VER"
    body = (lap_csv if kind == "lap_summary" else tel_csv)([(driver, 0.0), (driver, 1.0), (driver, 2.0)])
    with make_env() as (repo, up, engine):
        h = stage(up, engine, body, filename)
        m = csv_importer.import_session_csv(repo, payload(h, meta, units, data_type=kind, duplicate_policy=dup_policy), upload_dir=up)
        sid = m["session_id"]
        stored = repo.manifest(sid)
        want = expected_metadata(meta, units, filename)
        assert stored == json.loads(json.dumps(m, default=str))
        assert stored["original_filename"] == filename and stored["file_hash"] == h
        assert stored["metadata"] == want and stored["metadata"]["original_filename"] == filename
        assert stored["provider"] == "csv_upload" and stored["verification"] == "unverified"
        assert stored["source_type"] == "user_uploaded" and stored["imported_at"]
        with engine.connect() as c:
            row = c.execute(text("SELECT file_hash, provider, source_type, verification, metadata_json, imported_at "
                                 "FROM f1_hist_session WHERE session_id=:s"), {"s": sid}).one()
            cm = c.execute(text("SELECT mapping_json, units_json, duplicate_policy FROM f1_hist_column_mapping WHERE session_id=:s"),
                           {"s": sid}).one()
        assert row[0] == h and row[1] == "csv_upload" and (row[2], row[3]) == ("user_uploaded", "unverified")
        assert json.loads(row[4]) == want and row[5] is not None
        assert json.loads(cm[0]) == m["column_mapping"] and json.loads(cm[1]) == {**{"speed": "kph"}, **{
            "time": units.get("time", "seconds"), "distance": units.get("distance", "metres")}}
        assert cm[2] == dup_policy


# ----------------------------------------------------------------------------- Property 24
@st.composite
def key_pairs(draw):
    """A stored key and a candidate key that is equal or differs in exactly one key part."""
    stored = {"year": draw(st.integers(2000, 2030)), "event": draw(st.sampled_from(EVENTS)),
              "session_type": draw(st.sampled_from(STYPES)), "driver": draw(st.sampled_from(["VER", "HAM", "LEC"]))}
    cand = dict(stored)
    part = draw(st.sampled_from([None, "year", "event", "session_type", "driver"]))
    if part == "year":
        cand["year"] = stored["year"] + draw(st.integers(1, 5))
    elif part == "event":
        cand["event"] = draw(st.sampled_from([e for e in EVENTS if e != stored["event"]]))
    elif part == "session_type":
        cand["session_type"] = draw(st.sampled_from([s for s in STYPES if s != stored["session_type"]]))
    elif part == "driver":
        cand["driver"] = draw(st.sampled_from([d for d in ["VER", "HAM", "LEC"] if d != stored["driver"]]))
    return stored, cand, part is None


@CFG
@given(key_pairs(), st.sampled_from(["same_file", "other_file"]))
def test_p24_duplicate_detection_is_exact(pair, relation):
    """**Validates: Requirements 10.1, 10.2, 10.3**"""
    stored_key, cand_key, same_key = pair
    rows_a = [("VER", 0.0), ("HAM", 1.0), ("LEC", 2.0)]
    rows_b = [("VER", 3.0), ("HAM", 4.0), ("LEC", 5.0)]
    with make_env() as (repo, up, engine):
        ha = stage(up, engine, lap_csv(rows_a), "a.csv")
        first = csv_importer.import_session_csv(repo, payload(ha, {**BASE_META, **stored_key}), upload_dir=up)["session_id"]
        before = snapshot(engine)
        hb = ha if relation == "same_file" else stage(up, engine, lap_csv(rows_b), "b.csv")
        req = payload(hb, {**BASE_META, **cand_key})
        if relation == "same_file":  # equal hashes: always rejected, regardless of the key
            with pytest.raises(HistoricalImportError) as ei:
                csv_importer.import_session_csv(repo, req, upload_dir=up)
            assert ei.value.code == DUPLICATE_SESSION and ei.value.details["session_id"] == first
            assert snapshot(engine) == before
        elif same_key:  # different hash, equal key: paused pending a choice, nothing stored changes
            with pytest.raises(HistoricalImportError) as ei:
                csv_importer.import_session_csv(repo, req, upload_dir=up)
            assert ei.value.code == DUPLICATE_KEY_CONFLICT and ei.value.details["session_id"] == first
            assert snapshot(engine) == before
        else:  # neither: proceeds, and the stored session is untouched
            new = csv_importer.import_session_csv(repo, req, upload_dir=up)
            assert new["session_id"] != first and repo.exists(first) and len(repo.list_sessions()) == 2
            assert repo.manifest(first)["file_hash"] == ha
