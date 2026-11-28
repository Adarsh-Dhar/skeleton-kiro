"""Basic checks of the Setup_Link service and endpoint (task 6.3): selection, factor mapping, measurements, exclusion, rollback, links."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from f1lab.api.app import create_app

LAPS = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n" + "".join(f"{i},30.{i},40.{i},20.{i},90.{i}\n" for i in range(1, 6))
META = {"year": 2024, "event": "Imola test", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}


@pytest.fixture()
def env(tmp_path):
    with TestClient(create_app(str(tmp_path / "trialforge.db"), frontend_dir=str(tmp_path / "none"))) as c:
        h = c.post("/api/historical/csv/preview", files={"file": ("laps.csv", LAPS.encode(), "text/csv")}).json()["file_hash"]
        jid = c.post("/api/historical/import", json={"provider": "csv_upload", "csv": {"file_hash": h, "metadata": META,
                     "units": {"time": "s", "distance": "m"}}}).json()["import_id"]
        end = time.time() + 20
        while c.get(f"/api/historical/imports/{jid}").json()["state"] not in ("COMPLETED", "FAILED"):
            assert time.time() < end
            time.sleep(0.05)
        sid = c.get("/api/historical/sessions").json()[0]["session_id"]
        exp = c.post("/api/experiments", json={"name": "e", "experiment_id": "exp-1", "simulator": {"latency_seconds": 0.0},
                                               "design": {"run_budget": 50}}).json()
        yield c, sid, exp


def _factors(exp, verified=True):
    return {f["name"]: {"value": f["low"], "verified": verified} for f in exp["factors"]}


def _body(exp, **over):
    return {"driver": "VER", "laps": [1, 2, 3], "metrics": ["lap_time", "sector_1"], "experiment_id": "exp-1",
            "factors": _factors(exp), **over}


def _measurements(c):
    return [m for r in c.get("/api/experiments/exp-1/telemetry").json() for m in r["laps"]]


def test_verified_link_creates_one_measurement_per_lap_and_metric_with_session_id(env):
    c, sid, exp = env
    r = c.post(f"/api/historical/sessions/{sid}/setup-link", json=_body(exp))
    assert r.status_code == 201, r.text
    assert r.json()["measurements_created"] == 6 and r.json()["source_session_id"] == sid
    run = next(x for x in c.get("/api/experiments/exp-1").json()["runs"] if x["run_id"] == r.json()["run_id"])
    assert run["status"] == "completed", run["error"]
    csv = c.get("/api/experiments/exp-1/export/measurements").text if False else None  # noqa: F841
    lab = c.app.state.lab
    ms = lab.repo.get_run(r.json()["run_id"]).measurements
    assert len(ms) == 6 and all(m.metadata["source_session_id"] == sid and m.metadata["provenance_status"] for m in ms)
    assert lab.linked_experiments(sid) == [{"experiment_id": "exp-1", "name": "e"}]
    d = c.delete(f"/api/historical/sessions/{sid}")
    assert d.status_code == 409 and "exp-1" in d.text


def test_unverified_mapping_flags_measurements_excluded(env):
    c, sid, exp = env
    r = c.post(f"/api/historical/sessions/{sid}/setup-link", json=_body(exp, factors=_factors(exp, verified=False)))
    assert r.status_code == 201 and r.json()["excluded_from_training"] is True
    ms = c.app.state.lab.repo.get_run(r.json()["run_id"]).measurements
    assert len(ms) == 6 and all("unverified_setup" in m.quality_flags for m in ms)


def test_incomplete_configuration_names_every_missing_factor_and_changes_nothing(env):
    c, sid, exp = env
    before = c.get("/api/experiments/exp-1").json()["runs_total"]
    names = [f["name"] for f in exp["factors"]]
    f = _factors(exp)
    del f[names[0]]
    f[names[1]]["verified"] = None
    r = c.post(f"/api/historical/sessions/{sid}/setup-link", json=_body(exp, factors=f))
    assert r.status_code == 422 and names[0] in r.text and names[1] in r.text
    assert c.get("/api/experiments/exp-1").json()["runs_total"] == before


@pytest.mark.parametrize("over", [{"laps": []}, {"laps": list(range(1, 102))}, {"metrics": []}, {"metrics": ["stability"]},
                                  {"laps": [99]}, {"driver": None}, {"experiment_id": None}])
def test_selection_constraints_rejected(env, over):
    c, sid, exp = env
    assert c.post(f"/api/historical/sessions/{sid}/setup-link", json=_body(exp, **over)).status_code == 422


def test_failure_after_validation_rolls_back(env, monkeypatch):
    c, sid, exp = env
    lab = c.app.state.lab
    before = c.get("/api/experiments/exp-1").json()["runs_total"]

    async def boom(*a, **k):
        raise RuntimeError("storage failed")

    monkeypatch.setattr(lab.svc, "ingest_measurements", boom)
    with pytest.raises(RuntimeError):
        c.post(f"/api/historical/sessions/{sid}/setup-link", json=_body(exp))
    assert c.get("/api/experiments/exp-1").json()["runs_total"] == before
    assert lab.linked_experiments(sid) == []

