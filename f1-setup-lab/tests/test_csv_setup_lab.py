"""Task 6.6: integration tests for attachment to a queued run and the Setup_Link, driven through the HTTP API.

Covers attach of imported measurements to a queued run, refusal with no experiment change when the Setup_Configuration is
unknown, rollback after a failure, and the provenance source labels for OpenF1, user CSV and demo CSV sessions.
Validates: Requirements 14.1, 14.5, 15.5, 15.6, 15.10, 18.5, 18.6
"""
from __future__ import annotations

import asyncio
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from f1lab.api.app import create_app
from f1lab.integrations.historical.providers import PROVENANCE_DEMO, PROVENANCE_OPENF1, PROVENANCE_USER
from f1lab.services.lab import LabService

PRE = "/api/historical"
CSV = "lap,sector_1,sector_2,sector_3,lap_time\n1,30,30,30,90\n2,31,30,30,91\n3,29,31,30,90\n"
LAPS = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n" + "".join(f"{i},30.{i},40.{i},20.{i},90.{i}\n" for i in range(1, 6))
META = {"year": 2024, "event": "Imola test", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}
LAP_COLS = ["driver", "lap_number", "lap_time_s", "sector_1_s", "sector_2_s", "sector_3_s"]


@pytest.fixture()
def c(tmp_path):
    with TestClient(create_app(str(tmp_path / "trialforge.db"), frontend_dir=str(tmp_path / "none"))) as client:
        client.db = str(tmp_path / "trialforge.db")
        yield client


def _csv_session(c, body=LAPS):
    h = c.post(f"{PRE}/csv/preview", files={"file": ("laps.csv", body.encode(), "text/csv")}).json()["file_hash"]
    jid = c.post(f"{PRE}/import", json={"provider": "csv_upload", "csv": {"file_hash": h, "metadata": META,
                 "units": {"time": "s", "distance": "m"}}}).json()["import_id"]
    end = time.time() + 20
    while (job := c.get(f"{PRE}/imports/{jid}").json())["state"] not in ("COMPLETED", "FAILED"):
        assert time.time() < end
        time.sleep(0.05)
    assert job["state"] == "COMPLETED", job
    return job["session_id"]


def _seed(c, sid, **manifest_extra):
    """Store a session directly (as the OpenF1 importer or a demo generator would)."""
    laps = pd.DataFrame([["VER", n, 80.0 + n, 25.0 + n, 30.0, 25.0] for n in (1, 2, 3)], columns=LAP_COLS)
    tel = pd.DataFrame({"lap_number": [1, 1, 2, 2, 3, 3], "distance_m": [0.0, 10.0] * 3, "session_time_s": [0.0, 1.0, 80.0, 81.0, 160.0, 161.0],
                        "speed_kph": [200.0, 250.0, 210.0, 260.0, 220.0, 270.0]})
    manifest = {"session_id": sid, "year": 2024, "event": "Imola test", "session_type": "R", "session_date": "2024-05-19",
                "drivers": [{"code": "VER", "number": "1", "team": "RB"}], "channels": {}, "verification": "unverified", **manifest_extra}
    c.app.state.historical.repo.save_session(sid, manifest, laps, pd.DataFrame(), {"VER": tel}, {}, None)
    return sid


def _experiment(c, exp_id="exp-1"):
    r = c.post("/api/experiments", json={"name": "e", "experiment_id": exp_id, "simulator": {"latency_seconds": 0.0}, "design": {"run_budget": 50}})
    assert r.status_code == 201, r.text
    return r.json()


def _factors(exp, verified=True):
    return {f["name"]: {"value": f["low"], "verified": verified} for f in exp["factors"]}


def _body(exp, **over):
    return {"driver": "VER", "laps": [1, 2, 3], "metrics": ["lap_time", "sector_1"], "experiment_id": exp["id"],
            "factors": _factors(exp), **over}


def _link(c, sid, body):
    return c.post(f"{PRE}/sessions/{sid}/setup-link", json=body)


def _snapshot(c, exp_id):
    d = c.get(f"/api/experiments/{exp_id}").json()
    return d["runs_total"], c.get(f"/api/experiments/{exp_id}/runs").json(), c.get(f"/api/experiments/{exp_id}/telemetry").json()


# ------------------------------------------------------------------ attach to a queued run (14.1, 14.5)
def test_attach_imported_laps_and_sectors_to_a_queued_run(c):
    exp = _experiment(c)
    run_id = exp["runs"][0]["run_id"]
    assert exp["runs"][0]["lap_count"] == 0
    r = c.post(f"/api/experiments/{exp['id']}/runs/{run_id}/telemetry", json={"csv": CSV, "filename": "a.csv"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lap_count"] == 3 and body["sector_count"] == 9  # shown to the user for this run
    row = next(x for x in c.get(f"/api/experiments/{exp['id']}/runs").json() if x["run_id"] == run_id)
    assert row["lap_count"] == 3 and row["sector_count"] == 9 and row["imported"]
    laps = next(x for x in c.get(f"/api/experiments/{exp['id']}/telemetry").json() if x["run_id"] == run_id)["laps"]
    assert [x["lap"] for x in laps] == [1, 2, 3]  # one row per lap, on that run
    assert len(c.app.state.lab.repo.get_run(run_id).measurements) == 3 * 4  # every lap time and sector is associated with that run
    other = [x for x in c.get(f"/api/experiments/{exp['id']}/runs").json() if x["run_id"] != run_id]
    assert all(x["lap_count"] == 0 for x in other)


def test_session_import_creates_no_attachment_until_explicit_attach(c):
    exp = _experiment(c)
    before = _snapshot(c, exp["id"])
    _csv_session(c)
    assert _snapshot(c, exp["id"]) == before  # import alone attaches nothing (14.5)
    run_id = exp["runs"][0]["run_id"]
    assert c.post(f"/api/experiments/{exp['id']}/runs/{run_id}/telemetry", json={"csv": CSV}).status_code == 200
    assert _snapshot(c, exp["id"]) != before


def test_attach_of_unparseable_file_changes_nothing(c):
    exp = _experiment(c)
    run_id = exp["runs"][0]["run_id"]
    before = _snapshot(c, exp["id"])
    r = c.post(f"/api/experiments/{exp['id']}/runs/{run_id}/telemetry", json={"csv": "not,a,telemetry\nfile,x,y\n"})
    assert r.status_code >= 400
    assert _snapshot(c, exp["id"]) == before


# ------------------------------------------------------------------ Setup_Link: attach (15.5)
def test_setup_link_creates_one_measurement_per_lap_and_metric_with_provenance(c):
    sid = _csv_session(c)
    exp = _experiment(c)
    r = _link(c, sid, _body(exp))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["measurements_created"] == 6 and out["provenance_status"] == PROVENANCE_USER and out["source_session_id"] == sid
    ms = c.app.state.lab.repo.get_run(out["run_id"]).measurements
    assert len(ms) == 6
    assert {(m.metadata["lap"], m.metric) for m in ms} == {(lap, m) for lap in (1, 2, 3) for m in ("lap_time", "sector_1")}
    assert all(m.metadata["provenance_status"] == PROVENANCE_USER and m.metadata["source_session_id"] == sid for m in ms)


# ------------------------------------------------------------------ Setup_Link: unknown configuration (15.6, 18.6)
@pytest.mark.parametrize("variant", ["no_factors", "missing_one", "no_verification_statement"])
def test_unknown_setup_configuration_is_refused_and_experiment_unchanged(c, variant):
    sid = _csv_session(c)
    exp = _experiment(c)
    names = [f["name"] for f in exp["factors"]]
    factors = {"no_factors": None, "missing_one": {k: v for k, v in _factors(exp).items() if k != names[0]},
               "no_verification_statement": {k: {"value": v["value"]} for k, v in _factors(exp).items()}}[variant]
    before = _snapshot(c, exp["id"])
    status_before = c.get(f"/api/experiments/{exp['id']}").json()["status"]
    r = _link(c, sid, _body(exp, factors=factors))
    assert r.status_code == 422, r.text
    expected = names if variant != "missing_one" else names[:1]
    assert all(n in r.text for n in expected)  # each missing factor is listed by name (15.6)
    assert _snapshot(c, exp["id"]) == before
    assert c.get(f"/api/experiments/{exp['id']}").json()["status"] == status_before
    assert c.app.state.lab.linked_experiments(sid) == []


def test_unknown_configuration_for_a_new_experiment_creates_no_experiment(c):
    sid = _csv_session(c)
    ids = {e["id"] for e in c.get("/api/experiments").json()}
    body = _body(_experiment(c, "exp-other"), experiment_id=None, new_experiment={"name": "fresh", "experiment_id": "exp-new"}, factors={})
    ids.add("exp-other")
    r = _link(c, sid, body)
    assert r.status_code == 422
    assert {e["id"] for e in c.get("/api/experiments").json()} == ids


# ------------------------------------------------------------------ Setup_Link: rollback (15.10)
def test_failure_after_validation_rolls_back_all_measurements(c, monkeypatch):
    sid = _csv_session(c)
    exp = _experiment(c)
    lab = c.app.state.lab
    before = _snapshot(c, exp["id"])

    real = lab.svc.ingest_measurements

    async def partial_then_fail(exp_id, run_id, ms):
        await real(exp_id, run_id, ms)  # the measurements are really written first
        raise RuntimeError("storage failed")

    monkeypatch.setattr(lab.svc, "ingest_measurements", partial_then_fail)
    with pytest.raises(RuntimeError):
        _link(c, sid, _body(exp))
    assert _snapshot(c, exp["id"]) == before
    assert lab.linked_experiments(sid) == []
    with lab.repo.db_engine.connect() as conn:
        from sqlalchemy import text
        assert conn.execute(text("SELECT COUNT(*) FROM measurements WHERE payload_json LIKE :p"), {"p": f"%{sid}%"}).scalar() == 0

    monkeypatch.setattr(lab.svc, "ingest_measurements", real)
    again = _link(c, sid, _body(exp))  # the same selections can be submitted again
    assert again.status_code == 201 and again.json()["measurements_created"] == 6


# ------------------------------------------------------------------ provenance source labels (18.5)
def test_provenance_label_openf1_session(c):
    sid = _seed(c, "of1-session", provider="openf1", source_type="external_provider", verification="verified")
    assert c.get(f"{PRE}/sessions/{sid}").json()["provenance_status"] == PROVENANCE_OPENF1 == "OPENF1 — external provider"
    assert {s["session_id"]: s for s in c.get(f"{PRE}/sessions").json()}[sid]["provenance_status"] == PROVENANCE_OPENF1
    out = _link(c, sid, _body(_experiment(c)))
    assert out.status_code == 201, out.text
    assert out.json()["provenance_status"] == PROVENANCE_OPENF1
    ms = c.app.state.lab.repo.get_run(out.json()["run_id"]).measurements
    assert all(m.metadata["provenance_status"] == PROVENANCE_OPENF1 and m.metadata["imported"] is True for m in ms)


def test_provenance_label_user_csv_session_and_attach(c):
    sid = _csv_session(c)
    detail = c.get(f"{PRE}/sessions/{sid}").json()
    assert detail["provenance_status"] == PROVENANCE_USER == "USER UPLOADED — UNVERIFIED" and detail["provider"] == "csv_upload"
    exp = _experiment(c)
    run_id = exp["runs"][0]["run_id"]
    assert c.post(f"/api/experiments/{exp['id']}/runs/{run_id}/telemetry", json={"csv": CSV, "filename": "a.csv"}).status_code == 200
    ms = c.app.state.lab.repo.get_run(run_id).measurements
    assert ms and all(m.metadata["synthetic"] is False and m.metadata["imported"] is True for m in ms)
    assert c.get(f"/api/experiments/{exp['id']}").json()["source_label"] != ""


def test_provenance_label_demo_csv_session_and_attach(c):
    sid = _seed(c, "demo-session", provider="csv_upload", source_type="user_uploaded", verification="unverified",
                provenance_status=PROVENANCE_DEMO, synthetic=True)
    detail = c.get(f"{PRE}/sessions/{sid}").json()
    assert detail["provenance_status"] == PROVENANCE_DEMO == "SYNTHETIC DEMO — generated test data"
    out = _link(c, sid, _body(_experiment(c, "exp-demo-link")))
    assert out.status_code == 201, out.text
    ms = c.app.state.lab.repo.get_run(out.json()["run_id"]).measurements
    assert out.json()["provenance_status"] == PROVENANCE_DEMO
    assert all(m.metadata["synthetic"] is True and m.metadata["imported"] is False for m in ms)

    # explicit CSV attach with the demo label
    exp = _experiment(c, "exp-demo-attach")
    run_id = exp["runs"][0]["run_id"]
    lab = LabService(c.db)
    asyncio.run(lab.attach_telemetry(exp["id"], run_id, CSV, "s", "demo.csv", PROVENANCE_DEMO))
    ms = lab.repo.get_run(run_id).measurements
    assert ms and all(m.metadata["synthetic"] is True and m.metadata["provenance_status"] == PROVENANCE_DEMO for m in ms)


def test_each_source_has_a_distinct_label(c):
    labels = {PROVENANCE_OPENF1, PROVENANCE_USER, PROVENANCE_DEMO}
    assert len(labels) == 3
    seeded = {
        "of": _seed(c, "of1", provider="openf1", source_type="external_provider"),
        "demo": _seed(c, "demo1", provider="csv_upload", source_type="user_uploaded", provenance_status=PROVENANCE_DEMO, synthetic=True),
        "user": _csv_session(c),
    }
    shown = {k: c.get(f"{PRE}/sessions/{sid}").json()["provenance_status"] for k, sid in seeded.items()}
    assert shown == {"of": PROVENANCE_OPENF1, "demo": PROVENANCE_DEMO, "user": PROVENANCE_USER}
