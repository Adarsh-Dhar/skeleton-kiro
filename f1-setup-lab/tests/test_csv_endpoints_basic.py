"""Basic checks of the CSV upload endpoints (task 5.1): preview, validate, import, session-exists, summaries, 413."""
from __future__ import annotations

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from f1lab.api.app import create_app
from f1lab.integrations.historical import csv_importer

LAPS = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n1,30.1,40.2,20.3,90.6\n2,30.0,40.0,20.0,90.0\n"
META = {"year": 2024, "event": "Imola test", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "trialforge.db"), frontend_dir=str(tmp_path / "none"))) as c:
        c.data_dir = tmp_path
        yield c


def _preview(c, body=LAPS, name="laps.csv"):
    return c.post("/api/historical/csv/preview", files={"file": (name, body.encode(), "text/csv")})


def _wait(c, jid):
    end = time.time() + 20
    while time.time() < end:
        j = c.get(f"/api/historical/imports/{jid}").json()
        if j["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return j
        time.sleep(0.05)
    raise AssertionError("import did not finish")


def test_preview_stages_the_file_and_creates_no_session_or_job(client):
    r = _preview(client)
    assert r.status_code == 200, r.text
    body = r.json()
    h = hashlib.sha256(LAPS.encode()).hexdigest()
    assert body["file_hash"] == h and body["detected_type"] == "lap_summary" and body["row_count"] == 2
    assert body["columns"][0] == "lap" and body["suggested_mapping"]["sector_1_s"] == "sector_1_s"
    assert body["validation_report"]["ok"] is True
    assert (client.data_dir / "csv-uploads" / f"{h}.csv").is_file()
    assert client.get("/api/historical/sessions").json() == [] and client.get("/api/historical/imports").json() == []


def test_oversize_upload_is_413_and_not_stored(client, monkeypatch):
    monkeypatch.setattr(csv_importer, "CSV_MAX_UPLOAD_BYTES", 50)
    r = _preview(client, LAPS * 5)
    assert r.status_code == 413 and r.json()["limit"] == 50
    up = client.data_dir / "csv-uploads"
    assert not up.exists() or not list(up.iterdir())


def test_validate_import_summary_and_session_exists(client):
    h = _preview(client).json()["file_hash"]
    req = {"file_hash": h, "metadata": META, "units": {"time": "s", "distance": "m"}}
    v = client.post("/api/historical/csv/validate", json=req).json()
    assert v["ok"] is True and v["metadata"]["ok"] and v["mapping"]["ok"]
    bad = client.post("/api/historical/csv/validate", json={**req, "mapping": {"nonsense": "lap"}}).json()
    assert bad["ok"] is False and bad["mapping"]["errors"]
    assert client.post("/api/historical/csv/validate", json={"file_hash": "zz"}).status_code == 422
    assert client.get("/api/historical/csv/session-exists", params={"file_hash": h}).json()["exists"] is False

    t0 = time.time()
    r = client.post("/api/historical/import", json={"provider": "csv_upload", "csv": req})
    assert r.status_code == 202 and time.time() - t0 < 2
    job = _wait(client, r.json()["import_id"])
    assert job["state"] == "COMPLETED", job

    [s] = client.get("/api/historical/sessions").json()
    assert (s["provider"], s["source_type"], s["verification"]) == ("csv_upload", "user_uploaded", "unverified")
    assert s["data_type_label"] == "Lap summaries" and s["provenance_status"] == "USER UPLOADED — UNVERIFIED"
    assert s["metadata"]["driver"] == "VER" and s["metadata"]["circuit"] == "Imola"
    m = client.get(f"/api/historical/sessions/{s['session_id']}").json()
    assert m["provenance_status"] == s["provenance_status"] and m["metadata"] == s["metadata"]

    hit = client.get("/api/historical/csv/session-exists", params={"file_hash": h}).json()
    assert hit == {"exists": True, "session_id": s["session_id"], "matched_by": "file_hash"}
    key = {"event": "Imola test", "session_type": "FP2", "driver": "VER", "year": 2024}
    assert client.get("/api/historical/csv/session-exists", params=key).json()["matched_by"] == "session_key"
    assert client.get("/api/historical/csv/session-exists", params={**key, "year": 2023}).json()["exists"] is False
    assert client.get("/api/historical/csv/session-exists", params={"event": "x"}).status_code == 422


def test_unavailable_markers_are_returned_as_markers_and_bad_import_bodies_are_422(client):
    h = _preview(client).json()["file_hash"]
    meta = {**META, "circuit": None, "timezone": None, "unavailable": ["circuit", "timezone"]}
    job = _wait(client, client.post("/api/historical/import", json={"provider": "csv_upload", "csv": {"file_hash": h, "metadata": meta, "units": {"time": "s", "distance": "m"}}}).json()["import_id"])
    assert job["state"] == "COMPLETED", job
    s = client.get("/api/historical/sessions").json()[0]
    assert s["metadata"]["circuit"] is None and s["metadata"]["unavailable"] == ["circuit", "timezone"]

    assert client.post("/api/historical/import", json={"provider": "csv_upload"}).status_code == 422
    r = client.post("/api/historical/import", json={"provider": "csv_upload", "csv": {"file_hash": "nope"}})
    assert r.status_code == 422 and "csv.file_hash" in {d["field"] for d in r.json()["detail"]}
    assert client.post("/api/historical/import", json={"provider": "openf1"}).status_code == 422
