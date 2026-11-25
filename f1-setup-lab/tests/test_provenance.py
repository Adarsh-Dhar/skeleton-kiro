"""Provenance of Setup Engineering experiments (synthetic, imported, mixed), notices, CSV source columns, and the rule that
historical OpenF1 sessions never appear among experiments."""
import csv
import io

import pytest
from conftest import wait_done
from fastapi.testclient import TestClient
from openf1_fakes import FakeOpenF1, fake_client

import f1lab
from f1lab import IMPORTED_NOTICE, NOTICES, SYNTHETIC_NOTICE, provenance_notice
from f1lab.api.app import create_app

IMPORTED, SYNTH, MIXED = "IMPORTED TELEMETRY", "SYNTHETIC", "MIXED (SYNTHETIC + IMPORTED)"


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_imported_notice_text_and_is_not_a_mode_notice():
    assert IMPORTED_NOTICE == "Imported measurements: lap data from a user-supplied CSV; not produced by this app's model"
    assert set(NOTICES) == {"synthetic", "historical"} and IMPORTED_NOTICE not in NOTICES.values()
    assert provenance_notice(SYNTH) == SYNTHETIC_NOTICE and provenance_notice(IMPORTED) == IMPORTED_NOTICE
    assert SYNTHETIC_NOTICE in provenance_notice(MIXED) and IMPORTED_NOTICE in provenance_notice(MIXED)
    with pytest.raises(ValueError):
        provenance_notice("OTHER")
    assert f1lab.notice_for("synthetic") is SYNTHETIC_NOTICE  # existing notice text unchanged


def test_provenance_follows_only_the_completed_runs(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1})
    eid, first = d["id"], d["runs"][0]["run_id"]
    # nothing completed yet: the mode decides
    d0 = client.get(f"/api/experiments/{eid}").json()
    assert d0["source_label"] == SYNTH and d0["source_counts"] == {"synthetic": 0, "imported": 0} and d0["notice"] == SYNTHETIC_NOTICE
    assert {r["source_label"] for r in d0["runs"]} == {SYNTH}
    # one imported run, the rest still queued: queued runs are not considered
    csv_text = client.get("/api/demo/telemetry_ms.csv").text
    assert client.post(f"/api/experiments/{eid}/runs/{first}/telemetry", json={"csv": csv_text, "time_unit": "ms", "filename": "s.csv"}).status_code == 200
    d1 = client.get(f"/api/experiments/{eid}").json()
    assert d1["source_label"] == IMPORTED and d1["source_counts"] == {"synthetic": 0, "imported": 1} and d1["notice"] == IMPORTED_NOTICE
    assert d1["synthetic_notice"] == SYNTHETIC_NOTICE  # existing field keeps its meaning
    # the rest are simulated: mixed
    client.post(f"/api/experiments/{eid}/execute")
    d2 = wait_done(client, eid)
    d2 = client.get(f"/api/experiments/{eid}").json()
    done = d2["counts"]["completed"]
    assert d2["source_label"] == MIXED and d2["source_counts"] == {"synthetic": done - 1, "imported": 1}
    assert SYNTHETIC_NOTICE in d2["notice"] and IMPORTED_NOTICE in d2["notice"]
    labels = {r["run_id"]: r["source_label"] for r in d2["runs"]}
    assert labels[first] == IMPORTED and list(labels.values()).count(IMPORTED) == 1 and list(labels.values()).count(SYNTH) == done - 1
    # report shows both notices, CSVs carry a source column
    html = client.get(f"/api/experiments/{eid}/report.html").text
    assert "SYNTHETIC DATA" in html and "Imported measurements: lap data from a user-supplied CSV" in html
    runs = client.get(f"/api/experiments/{eid}/export/runs.csv")
    assert runs.headers["x-data-notice"].startswith("SYNTHETIC DATA") and "Imported measurements" in runs.headers["x-data-notice"]
    rows = _rows(runs.text)
    assert {r["run_id"]: r["source"] for r in rows} == labels
    eff = _rows(client.get(f"/api/experiments/{eid}/export/effects.csv").text)
    assert eff and {r["source"] for r in eff} == {MIXED}
    assert "source" in _rows(client.get(f"/api/experiments/{eid}/export/measurements.csv").text)[0]


def test_synthetic_experiment_exports_say_synthetic(run_experiment, client):
    d = run_experiment(design={"method": "ofat", "replicates": 1})
    assert d["source_label"] == SYNTH and d["source_counts"]["imported"] == 0
    assert {r["source"] for r in _rows(client.get(f"/api/experiments/{d['id']}/export/runs.csv").text)} == {SYNTH}
    assert {r["source"] for r in _rows(client.get(f"/api/experiments/{d['id']}/export/effects.csv").text)} == {SYNTH}
    html = client.get(f"/api/experiments/{d['id']}/report.html").text
    assert "SYNTHETIC DATA" in html and "Imported measurements" not in html


def test_historical_sessions_are_labelled_and_never_reachable_as_experiments(tmp_path):
    fake = FakeOpenF1()
    with TestClient(create_app(str(tmp_path / "trialforge.db"), openf1_client_factory=lambda: fake_client(tmp_path / "cache", fake)[0])) as c:
        assert c.get("/api/experiments").json() == []
        job = c.post("/api/historical/import", json={"year": 2025, "event": "Abu Dhabi", "session": "R"})
        assert job.status_code in (200, 202), job.text
        import time

        for _ in range(300):
            j = c.get(f"/api/historical/imports/{job.json()['import_id'] if 'import_id' in job.json() else job.json()['id']}").json()
            if j["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.05)
        assert j["state"] == "COMPLETED", j
        sessions = c.get("/api/historical/sessions").json()
        assert sessions
        sid = sessions[0]["session_id"]
        assert c.get(f"/api/historical/sessions/{sid}").json()["source_label"] == "HISTORICAL F1 \u00b7 OPENF1"
        assert c.get("/api/experiments").json() == []  # the session is not an experiment
        for path in (f"/api/experiments/{sid}", f"/api/experiments/{sid}/runs", f"/api/experiments/{sid}/export/runs.csv"):
            assert c.get(path).status_code == 404, path
        assert c.post(f"/api/experiments/{sid}/analysis", json={}).status_code == 404
