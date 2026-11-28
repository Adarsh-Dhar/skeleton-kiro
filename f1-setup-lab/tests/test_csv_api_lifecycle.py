"""Task 5.4: integration tests for the CSV HTTP contract and import lifecycle.

Covers status codes (404, 409, 413, 422), job states and progress, cancellation, retry without duplicate rows, duplicate
file rejection, deletion, restart persistence, shared analysis code for CSV and OpenF1 sessions, API responsiveness during
an import, and a 1,000,000-row import within the 300 s limit.
Validates: Requirements 8.5, 9.3, 9.5, 9.9, 11.3, 11.6, 11.7, 12.2, 12.6, 17.4, 17.6, 18.2, 18.4
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from f1lab.api.app import create_app
from f1lab.integrations.historical import analysis, csv_importer

LAPS_A = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n1,30.1,40.2,20.3,90.6\n2,30.0,40.0,20.0,90.0\n"
META = {"year": 2024, "event": "Imola test", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}
UNITS = {"time": "s", "distance": "m"}
FINAL = ("COMPLETED", "FAILED", "CANCELLED")
PRE = "/api/historical"


def sampled_csv(rows: int, drivers=("VER",), lap_len: int = 50) -> str:
    """Sampled telemetry: per driver, strictly increasing session time, laps of `lap_len` samples, distance 0..lap_len-1."""
    i = np.arange(rows)
    frames = []
    for k, d in enumerate(drivers):
        frames.append(pd.DataFrame({
            "session_time_s": np.round(i * 0.1 + k * 0.01, 2), "lap_number": i // lap_len + 1,
            "distance_m": (i % lap_len) * 10.0, "speed_kph": 100.0 + (i % 7) * 5 + k, "driver": d}))
    return pd.concat(frames).sort_values("session_time_s", kind="stable").to_csv(index=False, float_format="%.2f")


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "trialforge.db"), frontend_dir=str(tmp_path / "none"))) as c:
        c.data_dir = tmp_path
        c.hist = c.app.state.historical
        yield c


def preview(c, body, name="data.csv"):
    r = c.post(f"{PRE}/csv/preview", files={"file": (name, body.encode() if isinstance(body, str) else body, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()["file_hash"]


def start(c, h, meta=None, units=None):
    r = c.post(f"{PRE}/import", json={"provider": "csv_upload", "csv": {"file_hash": h, "metadata": meta or META, "units": units or UNITS}})
    assert r.status_code == 202, r.text
    return r.json()["import_id"]


def wait(c, jid, timeout=30):
    end = time.time() + timeout
    j = {}
    while time.time() < end:
        j = c.get(f"{PRE}/imports/{jid}").json()
        if j["state"] in FINAL:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish: {j}")


def imported(c, body=LAPS_A, meta=None):
    j = wait(c, start(c, preview(c, body), meta))
    assert j["state"] == "COMPLETED", j
    return j


def hist_rows(c):
    with c.app.state.engine.connect() as conn:
        names = sorted(r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'f1_hist_%'")))
        return {n: conn.execute(text(f"SELECT COUNT(*) FROM {n}")).scalar() for n in names}


# ------------------------------------------------------------------ status codes (11.6, 11.7, 11.12, 11.13)
def test_status_codes_404_409_413_422(client, monkeypatch):
    before = client.get(f"{PRE}/imports").json()
    for verb, path in (("get", ""), ("post", "/cancel"), ("post", "/retry")):
        assert getattr(client, verb)(f"{PRE}/imports/imp-ghost{path}").status_code == 404
    assert client.get(f"{PRE}/imports").json() == before  # 404s changed nothing
    assert client.delete(f"{PRE}/sessions/ghost").status_code == 404

    job = imported(client)
    snap = client.get(f"{PRE}/imports/{job['import_id']}").json()
    for path in ("cancel", "retry"):
        assert client.post(f"{PRE}/imports/{job['import_id']}/{path}").status_code == 409
    assert client.get(f"{PRE}/imports/{job['import_id']}").json() == snap

    # 422 lists every invalid field and creates no job
    n = len(client.get(f"{PRE}/imports").json())
    r = client.post(f"{PRE}/import", json={"provider": "csv_upload", "csv": {"file_hash": "nope", "metadata": {"year": "x"}}})
    assert r.status_code == 422 and "csv.file_hash" in {d["field"] for d in r.json()["detail"]}
    assert len(client.get(f"{PRE}/imports").json()) == n and len(client.get(f"{PRE}/sessions").json()) == 1

    # 413: the file is not stored
    monkeypatch.setattr(csv_importer, "CSV_MAX_UPLOAD_BYTES", 40)
    r = client.post(f"{PRE}/csv/preview", files={"file": ("big.csv", (LAPS_A * 3).encode(), "text/csv")})
    assert r.status_code == 413 and r.json()["limit"] == 40
    assert len(list((client.data_dir / "csv-uploads").glob("*.csv"))) == 1  # only the earlier upload


# ------------------------------------------------------------------ job states and progress (9.3, 9.4, 11.3, 12.3)
def test_job_states_progress_and_terminal_state_is_final(client, monkeypatch):
    seen: list[tuple[str, str]] = []
    real_progress, real_update = client.hist.jobs.progress, client.hist.jobs.update
    monkeypatch.setattr(client.hist.jobs, "progress", lambda i, s, d: (seen.append((s, d)), real_progress(i, s, d))[1])
    monkeypatch.setattr(client.hist.jobs, "update", lambda i, **v: (v.get("state") and seen.append((v["state"], "")), real_update(i, **v))[1])
    h = preview(client, sampled_csv(4000))
    jid = start(client, h)
    first = client.get(f"{PRE}/imports/{jid}").json()
    assert first["state"] in ("QUEUED", "PROCESSING", "SAVING", "COMPLETED") and first["import_id"] == jid
    done = wait(client, jid)
    assert done["state"] == "COMPLETED" and done["session_id"] and done["finished_at"]

    order = ["QUEUED", "PROCESSING", "SAVING", "COMPLETED"]
    ranks = [order.index(s) for s, _ in seen if s in order]
    assert ranks == sorted(ranks) and ranks[-1] == 3 and 1 in ranks and 2 in ranks  # only forward transitions (9.3)
    pct = [int(d.split("%")[0]) for s, d in seen if s == "PROCESSING" and "%" in d.split(" ")[0]]
    assert pct[0] == 0 and pct[-1] == 100 and pct == sorted(pct)
    assert all(b - a <= 5 for a, b in zip(pct, pct[1:])), pct  # a report for every 5 percent (12.3)
    assert "(4000/4000 rows)" in [d for s, d in seen if "%" in d][-1]

    real_update(jid, state="FAILED")  # a COMPLETED job never changes (9.3)
    assert client.get(f"{PRE}/imports/{jid}").json()["state"] == "COMPLETED"


# ------------------------------------------------------------------ cancellation (9.5, 11.4, 18.2)
def test_cancel_in_progress_leaves_zero_rows(client, monkeypatch):
    reached, go = threading.Event(), threading.Event()
    real = client.hist.jobs.progress

    def spy(import_id, state, detail):
        real(import_id, state, detail)
        if state == "PROCESSING" and detail.startswith("5%"):
            reached.set()
            go.wait(10)  # hold the worker until the cancel request has been made

    monkeypatch.setattr(client.hist.jobs, "progress", spy)
    h = preview(client, sampled_csv(6000))
    before = hist_rows(client)
    jid = start(client, h)
    assert reached.wait(20)
    t0 = time.time()
    r = client.post(f"{PRE}/imports/{jid}/cancel")
    assert r.status_code == 200 and r.json()["import_id"] == jid
    go.set()
    job = wait(client, jid)
    assert job["state"] == "CANCELLED" and time.time() - t0 < 5  # within 5 s (9.5)
    assert hist_rows(client) == before and client.get(f"{PRE}/sessions").json() == []
    assert client.post(f"{PRE}/imports/{jid}/cancel").status_code == 409


# ------------------------------------------------------------------ retry, duplicates, deletion (9.9, 18.2)
def test_retry_of_failed_import_succeeds_without_duplicate_rows(client, monkeypatch):
    h = preview(client, sampled_csv(500))
    real_save = client.hist.repo.save_session

    def boom(*a, **k):
        raise RuntimeError("disk full")

    before = hist_rows(client)
    monkeypatch.setattr(client.hist.repo, "save_session", boom)
    failed = wait(client, start(client, h))
    assert failed["state"] == "FAILED" and "disk full" in failed["error"]
    assert hist_rows(client) == before and client.get(f"{PRE}/sessions").json() == []  # rollback: zero rows (8.2)
    monkeypatch.setattr(client.hist.repo, "save_session", real_save)

    r = client.post(f"{PRE}/imports/{failed['import_id']}/retry")
    assert r.status_code == 202 and r.json()["state"] in ("QUEUED", "PROCESSING", "SAVING", "COMPLETED")
    ok = wait(client, r.json()["import_id"])
    assert ok["state"] == "COMPLETED" and ok["retry_of"] == failed["import_id"]
    assert client.get(f"{PRE}/imports/{failed['import_id']}").json()["state"] == "FAILED"
    rows = hist_rows(client)
    assert rows["f1_hist_session"] == 1
    assert client.post(f"{PRE}/imports/{ok['import_id']}/retry").status_code == 409  # a completed job cannot be retried

    # the retry stored exactly what a clean first-time import of the same file stores
    with TestClient(create_app(str(client.data_dir / "other.db"), frontend_dir=str(client.data_dir / "none"))) as other:
        imported(other, sampled_csv(500))
        assert hist_rows(other) == rows


def test_second_import_of_same_file_is_rejected_as_duplicate(client):
    first = imported(client, sampled_csv(200))
    h = preview(client, sampled_csv(200))
    rows = hist_rows(client)
    for meta in (META, {**META, "driver": "HAM", "event": "Other"}):  # same bytes: rejected whatever the metadata
        dup = wait(client, start(client, h, meta))
        assert dup["state"] == "FAILED" and first["session_id"] in dup["error"], dup
    assert hist_rows(client) == rows and len(client.get(f"{PRE}/sessions").json()) == 1


def test_delete_removes_session_and_telemetry_rows(client):
    sid = imported(client, sampled_csv(300))["session_id"]
    rows = hist_rows(client)
    assert rows["f1_hist_session"] == 1 and rows["f1_hist_telemetry"] == 300
    assert client.delete(f"{PRE}/sessions/{sid}").status_code == 200
    assert all(n == 0 for n in hist_rows(client).values())
    assert client.get(f"{PRE}/sessions/{sid}").status_code == 404 and client.get(f"{PRE}/sessions").json() == []
    assert client.delete(f"{PRE}/sessions/{sid}").status_code == 404


# ------------------------------------------------------------------ restart persistence (8.5, 8.6, 18.2)
def test_restart_keeps_completed_sessions_with_identical_counts_and_fails_active_jobs(tmp_path):
    db = str(tmp_path / "trialforge.db")
    frontend = str(tmp_path / "none")
    with TestClient(create_app(db, frontend_dir=frontend)) as c1:
        c1.hist = c1.app.state.historical
        c1.data_dir = tmp_path
        job = imported(c1, sampled_csv(700, ("VER", "HAM")), {**META, "driver": None, "unavailable": ["driver"]})
        imported(c1, LAPS_A, {**META, "event": "Other GP"})
        listing, rows = c1.get(f"{PRE}/sessions").json(), hist_rows(c1)
        detail = c1.get(f"{PRE}/sessions/{job['session_id']}").json()
        running = c1.hist.jobs.create({"provider": "csv_upload", "csv": {"file_hash": "a" * 64, "metadata": META, "units": UNITS}})
        c1.hist.jobs.update(running["import_id"], state="SAVING")
    assert rows["f1_hist_telemetry"] == 1400 and len(listing) == 2

    with TestClient(create_app(db, frontend_dir=frontend)) as c2:
        c2.hist = c2.app.state.historical
        assert hist_rows(c2) == rows
        assert c2.get(f"{PRE}/sessions").json() == listing
        again = c2.get(f"{PRE}/sessions/{job['session_id']}").json()
        for key in ("file_hash", "original_filename", "imported_at", "provider", "verification", "metadata", "provenance_status"):
            assert again.get(key) == detail.get(key), key
        j = c2.get(f"{PRE}/imports/{running['import_id']}").json()
        assert j["state"] == "FAILED" and j["error_code"] == "INTERRUPTED" and j["error"].startswith("interrupted")
        assert c2.get(f"{PRE}/imports/{job['import_id']}").json()["state"] == "COMPLETED"


# ------------------------------------------------------------------ same analysis code for CSV and OpenF1 (17.4, 17.6, 18.4)
def _seed_openf1(c, sid="of1-session"):
    n = 40
    tel = {code: pd.DataFrame({"lap_number": [1] * n + [2] * n, "distance_m": [100.0 * i for i in range(n)] * 2,
                               "session_time_s": [2.0 * (j % n) + k + 80.0 * (j // n) for j in range(2 * n)],
                               "speed_kph": [200.0] * (2 * n)}) for k, code in enumerate(("VER", "HAM"))}
    laps = pd.DataFrame({"driver": ["VER", "VER", "HAM", "HAM"], "lap_number": [1, 2, 1, 2], "lap_time_s": [80.0, 79.0, 81.0, 82.0]})
    manifest = {"session_id": sid, "provider": "openf1", "source_type": "external_provider", "verification": "verified", "year": 2024,
                "event": "Imola test", "session_type": "R", "session_date": "2024-05-19",
                "drivers": [{"code": "VER", "number": "1", "team": "RB"}, {"code": "HAM", "number": "44", "team": "M"}], "channels": {}}
    c.hist.repo.save_session(sid, manifest, laps, pd.DataFrame(), tel, {}, None)
    return sid


def test_csv_session_uses_the_same_analysis_functions_as_openf1(client, monkeypatch):
    calls = {"mini_sectors": [], "season_compare": []}
    for name in calls:
        real = getattr(analysis, name)
        monkeypatch.setattr(analysis, name, lambda *a, _r=real, _n=name, **k: (calls[_n].append(a), _r(*a, **k))[1])
    csv_sid = imported(client, sampled_csv(100, ("VER", "HAM")), {**META, "driver": None, "unavailable": ["driver"]})["session_id"]
    lap_sid = imported(client, LAPS_A, {**META, "event": "Laps GP"})["session_id"]
    of_sid = _seed_openf1(client)

    # session list: both providers through the one endpoint, with the same summary keys
    summaries = {s["session_id"]: s for s in client.get(f"{PRE}/sessions").json()}
    assert {csv_sid, of_sid, lap_sid} <= set(summaries)
    assert summaries[csv_sid].keys() == summaries[of_sid].keys()
    assert (summaries[csv_sid]["provider"], summaries[of_sid]["provider"]) == ("csv_upload", "openf1")

    # driver comparison, lap selection and track chart data: same response shape for both providers
    shapes = {}
    for sid in (csv_sid, of_sid):
        base = f"{PRE}/sessions/{sid}"
        assert {d["code"] for d in client.get(f"{base}/drivers").json()} == {"VER", "HAM"}
        cmp = client.post(f"{base}/compare", json={"driver_a": "VER", "lap_a": 1, "driver_b": "HAM", "lap_b": 2})
        assert cmp.status_code == 200, cmp.text
        lap1 = client.get(f"{base}/telemetry", params={"driver": "VER", "lap": 1}).json()
        lap2 = client.get(f"{base}/telemetry", params={"driver": "VER", "lap": 2}).json()
        assert lap1["lap"] == 1 and lap2["lap"] == 2 and lap1["count"] > 0 and lap2["count"] > 0
        assert lap1["samples"] != lap2["samples"] and lap1["count"] < 2 * (lap1["count"] + lap2["count"])
        whole = client.get(f"{base}/telemetry", params={"driver": "VER"}).json()
        assert whole["count"] == lap1["count"] + lap2["count"]  # lap selection narrows the same stored samples
        ms = client.get(f"{base}/mini-sectors", params={"driver": "VER", "lap": 1, "count": 10})
        assert ms.status_code == 200, ms.text
        shapes[sid] = (sorted(cmp.json()), sorted(cmp.json()["delta"]), sorted(lap1), sorted(ms.json()))
        assert len(ms.json()["slices"]) == 10
    assert shapes[csv_sid] == shapes[of_sid]
    assert len(calls["mini_sectors"]) == 2  # one call per session, through analysis.mini_sectors

    r = client.post(f"{PRE}/compare-sessions", json={"sessions": [lap_sid, of_sid], "drivers": ["VER"], "metric": "best_lap"})
    assert r.status_code == 200, r.text
    assert len(calls["season_compare"]) == 1
    assert {s["id"] for s in calls["season_compare"][0][0]} == {lap_sid, of_sid}  # CSV and OpenF1 in one analysis call


# ------------------------------------------------------------------ large import and responsiveness (12.2, 12.6)
def _timed_get(c, path):
    t0 = time.time()
    r = c.get(path)
    return r.status_code, time.time() - t0


def test_api_responds_within_2s_while_an_import_runs(client):
    h = preview(client, sampled_csv(150_000))
    jid = start(client, h)
    latencies = []
    for _ in range(200):
        for path in (f"{PRE}/sessions", f"{PRE}/imports/{jid}"):
            code, dt = _timed_get(client, path)
            assert code == 200
            latencies.append(dt)
        if client.get(f"{PRE}/imports/{jid}").json()["state"] in FINAL:
            break
        time.sleep(0.1)
    assert max(latencies) < 2.0, max(latencies)
    assert wait(client, jid, 120)["state"] == "COMPLETED"


@pytest.mark.slow
def test_one_million_row_import_completes_within_300_seconds(client):
    body = sampled_csv(1_000_000, lap_len=1000)
    h = preview(client, body)
    del body
    t0 = time.time()
    jid = start(client, h)
    assert time.time() - t0 < 2  # job id returned without waiting for the import (9.1, 11.2)
    worst = 0.0
    while True:
        for path in (f"{PRE}/sessions", f"{PRE}/imports/{jid}"):
            code, dt = _timed_get(client, path)
            assert code == 200
            worst = max(worst, dt)
        job = client.get(f"{PRE}/imports/{jid}").json()
        if job["state"] in FINAL:
            break
        assert time.time() - t0 < 300, f"still {job['state']} after 300 s: {job['detail']}"
        time.sleep(0.5)
    elapsed = time.time() - t0
    assert job["state"] == "COMPLETED", job
    assert elapsed < 300 and worst < 2.0, (elapsed, worst)
    assert hist_rows(client)["f1_hist_telemetry"] == 1_000_000
