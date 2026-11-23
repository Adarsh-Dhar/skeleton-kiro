"""Property 12: the new historical routes never write to the database (Requirements 4.7, 5.8, 6.9, 7.8, 8.1)."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from f1lab.api.app import create_app
from f1lab.integrations.historical.openf1_client import OpenF1Client, RateLimiter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import gen_historical_manifest as gen  # noqa: E402
from openf1_fakes import FakeOpenF1  # noqa: E402

PRE = "/api/historical"
TABLES = ("laps", "results", "standings", "weather", "tyre_degradation", "mini_sectors")
METRICS = ("points", "best_lap", "median_lap", "grid_position")
FIXTURES = sorted((Path(__file__).parent / "fixtures" / "openf1").glob("*.json"))


def _import(http, year, event, code, driver):
    iid = http.post(f"{PRE}/import", json={"year": year, "event": event, "session": code, "drivers": [driver]}).json()["import_id"]
    for _ in range(1200):
        s = http.get(f"{PRE}/imports/{iid}").json()
        if s["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.05)
    assert s["state"] == "COMPLETED", s
    return s["session_id"]


def snapshot(db: Path) -> dict:
    """Every table of the SQLite file with its sorted row contents."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: sorted(map(repr, con.execute(f'SELECT * FROM "{n}"').fetchall())) for n in names}
    finally:
        con.close()


@pytest.fixture(scope="module")
def sessions():
    """(client, session_id, driver code, db path) per fixture session, imported once."""
    out, stack = [], []
    with tempfile.TemporaryDirectory(prefix="read-only-") as tmp:
        sources = []
        for f in FIXTURES:
            d = json.loads(f.read_text())
            sess, meeting = d["sessions"][0], d["meetings"][0]
            num = str(d["_meta"]["driver_number"])
            drv = next(x["name_acronym"] for x in d["drivers"] if str(x["driver_number"]) == num)
            sources.append((f.stem, gen.fixture_fetch(d), int(sess["year"]), meeting["meeting_name"], gen.CODES[sess["session_name"]], drv))
        sources.append(("fake", FakeOpenF1(), 2025, "Abu Dhabi Grand Prix", "R", "HAM"))
        try:
            for name, fetch, year, event, code, drv in sources:
                p = Path(tmp) / name
                client = OpenF1Client(p / "cache", fetch=fetch, limiter=RateLimiter(per_second=10_000, per_minute=10_000_000), sleep=lambda s: None)
                db = p / "trialforge.db"
                app = create_app(str(db), frontend_dir=str(p / "none"), openf1_client_factory=lambda c=client: c)
                http = TestClient(app)
                http.__enter__()
                stack.append(http)
                out.append((http, _import(http, year, event, code, drv), drv, db))
            yield out
        finally:
            for http in stack:
                http.__exit__(None, None, None)


_call = st.one_of(
    st.tuples(st.just("tyre"), st.just(None), st.just(None), st.just(None)),
    st.tuples(st.just("mini"), st.integers(10, 50), st.one_of(st.none(), st.integers(0, 80)), st.one_of(st.none(), st.integers(0, 80))),
    st.tuples(st.just("compare"), st.sampled_from(METRICS), st.just(None), st.just(None)),
    st.tuples(st.just("export"), st.sampled_from(TABLES), st.just(None), st.just(None)),
    st.tuples(st.just("report"), st.just(None), st.just(None), st.just(None)),
)


def _do(http, sid, drv, laps, call):
    kind, a, b, c = call
    base = f"{PRE}/sessions/{sid}"
    if kind == "tyre":
        return http.get(f"{base}/tyre-degradation")
    if kind == "mini":
        params = {"driver": drv, "count": a}
        if b is not None and laps:
            params["lap"] = laps[b % len(laps)]
        if c is not None and laps:
            params["ref_driver"] = drv
            params["ref_lap"] = laps[c % len(laps)]
        return http.get(f"{base}/mini-sectors", params=params)
    if kind == "compare":
        return http.post(f"{PRE}/compare-sessions", json={"sessions": [sid, sid], "drivers": [drv], "metric": a})
    if kind == "export":
        params = {"table": a}
        if a == "mini_sectors":
            params["driver"] = drv
        return http.get(f"{base}/export.csv", params=params)
    return http.get(f"{base}/report.html")


# Feature: f1-historical-phase1, Property 12: Read-only
# **Validates: Requirements 4.7, 5.8, 6.9, 7.8, 8.1**
@settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))
@given(idx=st.integers(0, 10), calls=st.lists(_call, min_size=1, max_size=8))
def test_property_12_read_only(sessions, idx, calls):
    http, sid, drv, db = sessions[idx % len(sessions)]
    laps = sorted({x["lap_number"] for x in http.get(f"{PRE}/sessions/{sid}/laps").json() if x.get("lap_number") is not None})
    before = snapshot(db)
    for call in calls:
        r = _do(http, sid, drv, laps, call)
        assert r.status_code < 500, (call, r.status_code, r.text)
    assert snapshot(db) == before
