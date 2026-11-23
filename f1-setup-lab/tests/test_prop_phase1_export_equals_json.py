"""Property 10: every CSV export table equals its JSON route (Requirement 7.3)."""
from __future__ import annotations

import csv
import io
import math
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
import json  # noqa: E402

import gen_historical_manifest as gen  # noqa: E402
from openf1_fakes import FakeOpenF1  # noqa: E402

PRE = "/api/historical"
TABLES = ("laps", "results", "standings", "weather", "tyre_degradation", "mini_sectors")
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


@pytest.fixture(scope="module")
def sessions():
    """(client, session_id, driver code) for each OpenF1 fixture session and the fake-server session, imported once."""
    out, stack = [], []
    with tempfile.TemporaryDirectory(prefix="export-eq-") as tmp:
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
                app = create_app(str(p / "trialforge.db"), frontend_dir=str(p / "none"), openf1_client_factory=lambda c=client: c)
                http = TestClient(app)
                http.__enter__()
                stack.append(http)
                out.append((http, _import(http, year, event, code, drv), drv))
            yield out
        finally:
            for http in stack:
                http.__exit__(None, None, None)


def _cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, str) and v.startswith(("=", "+", "-", "@")):
        return "'" + v
    return str(v)


def _expected(rows, default_header=None):
    header: list[str] = []
    for r in rows:
        header += [k for k in r if k not in header]
    if not header:
        return None
    return [header, *[[_cell(r.get(k)) for k in header] for r in rows]]


def _json_rows(http, sid, table, params):
    base = f"{PRE}/sessions/{sid}"
    if table == "laps":
        return http.get(f"{base}/laps")
    if table == "results":
        r = http.get(f"{base}/datasets/session_result", params={"limit": 0})
        return r, r.json()["rows"] if r.status_code == 200 else None
    if table == "weather":
        r = http.get(f"{base}/weather")
        return r, r.json()["samples"] if r.status_code == 200 else None
    if table == "tyre_degradation":
        r = http.get(f"{base}/tyre-degradation")
        rows = []
        for s in r.json()["stints"]:
            row = {k: v for k, v in s.items() if k != "laps_excluded"}
            for k, v in (s.get("laps_excluded") or {}).items():
                row[f"laps_excluded_{k}"] = v
            rows.append(row)
        return r, rows
    if table == "mini_sectors":
        r = http.get(f"{base}/mini-sectors", params=params)
        return r, r.json()["slices"] if r.status_code == 200 else None
    # standings: the two championship datasets, flattened as the report does
    drivers = http.get(f"{base}/drivers").json()
    team_of = {str(d.get("code")): d.get("team") for d in drivers}
    rows = []
    for kind, ds in (("driver", "drivers_championship"), ("team", "teams_championship")):
        for x in http.get(f"{base}/datasets/{ds}", params={"limit": 0}).json()["rows"]:
            name = x.get("driver") if kind == "driver" else x.get("team")
            rows.append({"kind": kind, "position": x.get("position_current"), "name": name,
                         "team": team_of.get(str(name)) if kind == "driver" else name,
                         "points_start": x.get("points_start"), "points_current": x.get("points_current")})
    return None, rows


# Feature: f1-historical-phase1, Property 10: Export equals JSON
# **Validates: Requirements 7.3**
@settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))
@given(data=st.data(), table=st.sampled_from(TABLES), idx=st.integers(0, 10), count=st.integers(5, 55),
       use_lap=st.booleans(), lap_pick=st.integers(0, 80), ref=st.sampled_from([None, "same"]), ref_lap_pick=st.one_of(st.none(), st.integers(0, 80)))
def test_property_10_export_equals_json(sessions, data, table, idx, count, use_lap, lap_pick, ref, ref_lap_pick):
    http, sid, drv = sessions[idx % len(sessions)]
    base = f"{PRE}/sessions/{sid}"
    params: dict = {}
    if table == "mini_sectors":
        lap_numbers = sorted({x["lap_number"] for x in http.get(f"{base}/laps").json() if x.get("lap_number") is not None}) or [1]
        params = {"driver": drv, "count": count}
        if use_lap:
            params["lap"] = lap_numbers[lap_pick % len(lap_numbers)]
        if ref:
            params["ref_driver"] = drv
        if ref_lap_pick is not None:
            params["ref_lap"] = lap_numbers[ref_lap_pick % len(lap_numbers)]
    resp = http.get(f"{base}/export.csv", params={"table": table, **params})
    if table == "laps":
        jr = http.get(f"{base}/laps")
        jrows = jr.json() if jr.status_code == 200 else None
    else:
        jr, jrows = _json_rows(http, sid, table, params)
    if jr is not None and jr.status_code != 200:
        assert resp.status_code == jr.status_code  # the same error as the JSON route
        return
    assert resp.status_code == 200, resp.text
    parsed = list(csv.reader(io.StringIO(resp.text, newline="")))
    want = _expected(jrows)
    if want is None:
        assert len(parsed) == 1 and parsed[0]  # empty table: header only
    else:
        assert parsed == want
