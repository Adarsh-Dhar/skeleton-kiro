"""Property tests for CSV replacement, deletion and preview (f1-unified-telemetry-import, task 5.3)."""
from __future__ import annotations

import contextlib
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from f1lab.api.app import create_app

SETTINGS = settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))
FINAL = ("COMPLETED", "FAILED", "CANCELLED")
UNITS = {"time": "s", "distance": "m"}
HEADER = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n"


@contextlib.contextmanager
def fresh_client():
    with tempfile.TemporaryDirectory() as d:
        with TestClient(create_app(str(Path(d) / "trialforge.db"), frontend_dir=str(Path(d) / "none"))) as c:
            yield c


# ---- local strategies ----
@st.composite
def lap_csv(draw, offset=None):
    """Valid lap-summary CSV; `offset` makes content differ between two files."""
    n = draw(st.integers(min_value=1, max_value=6))
    base = draw(st.integers(min_value=20, max_value=40)) if offset is None else offset
    rows = [f"{i},{base + i / 10:.1f},{base + 1:.1f},{base + 2:.1f},{3 * base + 3 + i / 10:.1f}" for i in range(1, n + 1)]
    return HEADER + "\n".join(rows) + "\n"


@st.composite
def two_distinct_csvs(draw):
    a = draw(lap_csv(offset=draw(st.integers(20, 29))))
    b = draw(lap_csv(offset=draw(st.integers(30, 39))))
    return a, b


@st.composite
def metadata(draw):
    return {"year": draw(st.integers(2018, 2025)), "event": draw(st.sampled_from(["Imola test", "Monza", "Spa"])),
            "circuit": "Circuit", "session_type": draw(st.sampled_from(["FP1", "FP2", "Q", "R"])),
            "driver": draw(st.sampled_from(["VER", "HAM", "LEC", "NOR"])), "session_date": "2024-05-18",
            "timezone": "Europe/Rome"}


invalid_files = st.one_of(
    st.just(b""), st.just(b"\xff\xfe\x00garbage"), st.just(b"a,b\n"),
    st.text(alphabet="abc,;\n 12.", max_size=80).map(str.encode),
)


# ---- helpers ----
def preview(c, body: str | bytes):
    raw = body.encode() if isinstance(body, str) else body
    return c.post("/api/historical/csv/preview", files={"file": ("f.csv", raw, "text/csv")})


def start(c, h, meta):
    r = c.post("/api/historical/import", json={"provider": "csv_upload", "csv": {"file_hash": h, "metadata": meta, "units": UNITS}})
    assert r.status_code == 202, r.text
    return r.json()["import_id"]


def wait(c, jid):
    end = time.time() + 20
    while time.time() < end:
        j = c.get(f"/api/historical/imports/{jid}").json()
        if j["state"] in FINAL or j.get("awaiting_decision"):
            return j
        time.sleep(0.02)
    raise AssertionError(f"job stuck: {j}")


def hist_rows(c):
    with c.app.state.engine.connect() as conn:
        names = sorted(r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'f1_hist_%'")))
        return {n: conn.execute(text(f"SELECT COUNT(*) FROM {n}")).scalar() for n in names}


def import_one(c, body, meta):
    j = wait(c, start(c, preview(c, body).json()["file_hash"], meta))
    assert j["state"] == "COMPLETED", j
    return j["session_id"]


def snapshot(c):
    return (c.get("/api/historical/sessions").json(), c.get("/api/historical/imports").json())


# ---- Property 25 ----
@SETTINGS
@given(files=two_distinct_csvs(), meta=metadata(), fail=st.booleans())
def test_property_25_replacement_is_all_or_nothing(files, meta, fail):
    """**Validates: Requirements 10.5, 10.6**"""
    a, b = files
    with fresh_client() as c:
        sid = import_one(c, a, meta)
        old = c.get(f"/api/historical/sessions/{sid}").json()
        hb = preview(c, b).json()["file_hash"]
        paused = wait(c, start(c, hb, meta))
        assert paused["awaiting_decision"]["existing_session_id"] == sid
        before = hist_rows(c)
        repo = c.app.state.historical.repo
        if fail:
            def boom(*a, **k):
                raise RuntimeError("disk full")
            repo.save_session = boom  # instance attribute shadows the method
        try:
            assert c.post(f"/api/historical/imports/{paused['import_id']}/decision", params={"choice": "replace"}).status_code == 200
            done = wait(c, paused["import_id"])
        finally:
            repo.__dict__.pop("save_session", None)
        now = c.get(f"/api/historical/sessions/{sid}").json()
        if fail:
            assert done["state"] == "FAILED" and "replacement failed" in done["error"]
            assert now == old and hist_rows(c) == before
        else:
            assert done["state"] == "COMPLETED" and done["session_id"] == sid
            assert now["file_hash"] == hb and now["file_hash"] != old["file_hash"]
            assert len(c.get("/api/historical/sessions").json()) == 1
            rows = hist_rows(c)
            assert rows["f1_hist_session"] == 1
            laps = b.count("\n") - 1
            assert rows["f1_hist_lap"] == laps and (before["f1_hist_lap"] == a.count("\n") - 1)


@SETTINGS
@given(body=lap_csv(), meta=metadata(), fail=st.booleans())
def test_property_25_deletion_is_all_or_nothing(body, meta, fail):
    """**Validates: Requirements 10.7, 10.8**"""
    with fresh_client() as c:
        sid = import_one(c, body, meta)
        before = hist_rows(c)
        assert before["f1_hist_session"] == 1 and before["f1_hist_lap"] > 0
        repo = c.app.state.historical.repo
        if fail:
            def boom(s):
                raise RuntimeError("locked")
            repo.delete_session = boom
        try:
            r = c.delete(f"/api/historical/sessions/{sid}")
        finally:
            repo.__dict__.pop("delete_session", None)
        if fail:
            assert r.status_code == 500 and "deleting session" in r.text
            assert c.get(f"/api/historical/sessions/{sid}").status_code == 200 and hist_rows(c) == before
        else:
            assert r.status_code == 200
            assert c.get(f"/api/historical/sessions/{sid}").status_code == 404
            assert all(n == 0 for n in hist_rows(c).values()), hist_rows(c)


@SETTINGS
@given(body=lap_csv(), meta=metadata())
def test_property_25_failed_repository_transaction_rolls_back_deletion(body, meta):
    """**Validates: Requirements 10.8** (a failure inside the repository's own transaction keeps every row)."""
    with fresh_client() as c:
        sid = import_one(c, body, meta)
        before = hist_rows(c)
        repo = c.app.state.historical.repo
        orig_engine = repo.engine
        # Make the DELETE on a dependent table fail midway via a trigger on the lap table.
        with orig_engine.begin() as conn:
            conn.execute(text("CREATE TRIGGER block_lap_delete BEFORE DELETE ON f1_hist_lap BEGIN SELECT RAISE(ABORT, 'blocked'); END"))
        try:
            r = c.delete(f"/api/historical/sessions/{sid}")
            assert r.status_code == 500
            assert c.get(f"/api/historical/sessions/{sid}").status_code == 200
            assert hist_rows(c) == before
        finally:
            with orig_engine.begin() as conn:
                conn.execute(text("DROP TRIGGER IF EXISTS block_lap_delete"))


# ---- Property 26 ----
@SETTINGS
@given(payload=st.one_of(lap_csv().map(str.encode), invalid_files), prior=st.booleans(), meta=metadata())
def test_property_26_preview_never_creates_session_or_job(payload, prior, meta):
    """**Validates: Requirements 11.1**"""
    with fresh_client() as c:
        if prior:
            import_one(c, HEADER + "1,30.1,40.2,20.3,90.6\n", meta)
        before = snapshot(c)
        rows = hist_rows(c)
        r = preview(c, payload)
        assert r.status_code in (200, 400, 413, 415, 422), r.text
        assert snapshot(c) == before and hist_rows(c) == rows
