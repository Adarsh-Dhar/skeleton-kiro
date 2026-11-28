"""Task 5.2: job lifecycle, duplicate-key replacement, session deletion and restart behaviour (HTTP level)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from f1lab.api.app import create_app

LAPS_A = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n1,30.1,40.2,20.3,90.6\n2,30.0,40.0,20.0,90.0\n"
LAPS_B = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n1,31.1,41.2,21.3,93.6\n2,31.0,41.0,21.0,93.0\n3,31.0,41.0,21.0,93.0\n"
META = {"year": 2024, "event": "Imola test", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}
UNITS = {"time": "s", "distance": "m"}
FINAL = ("COMPLETED", "FAILED", "CANCELLED")


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "trialforge.db"), frontend_dir=str(tmp_path / "none"))) as c:
        c.data_dir = tmp_path
        c.hist = c.app.state.historical
        yield c


def preview(c, body):
    r = c.post("/api/historical/csv/preview", files={"file": ("laps.csv", body.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()["file_hash"]


def start(c, h, meta=None):
    r = c.post("/api/historical/import", json={"provider": "csv_upload", "csv": {"file_hash": h, "metadata": meta or META, "units": UNITS}})
    assert r.status_code == 202, r.text
    return r.json()["import_id"]


def wait(c, jid, states=FINAL):
    end = time.time() + 20
    while time.time() < end:
        j = c.get(f"/api/historical/imports/{jid}").json()
        if j["state"] in states or j.get("awaiting_decision"):
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} stuck: {j}")


def imported(c, body=LAPS_A, meta=None):
    h = preview(c, body)
    j = wait(c, start(c, h, meta))
    assert j["state"] == "COMPLETED", j
    return h, j


def hist_rows(c):
    with c.app.state.engine.connect() as conn:
        names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'f1_hist_%'"))]
        return {n: conn.execute(text(f"SELECT COUNT(*) FROM {n}")).scalar() for n in names}


def test_404_and_409_for_jobs_and_sessions(client):
    for verb, path in (("get", ""), ("post", "/cancel"), ("post", "/retry")):
        assert getattr(client, verb)(f"/api/historical/imports/imp-nope{path}").status_code == 404
    assert client.delete("/api/historical/sessions/nope").status_code == 404
    _, job = imported(client)
    before = client.get(f"/api/historical/imports/{job['import_id']}").json()
    assert client.post(f"/api/historical/imports/{job['import_id']}/cancel").status_code == 409
    assert client.post(f"/api/historical/imports/{job['import_id']}/retry").status_code == 409
    assert client.get(f"/api/historical/imports/{job['import_id']}").json() == before
    assert client.post("/api/historical/imports/imp-nope/decision", params={"choice": "replace"}).status_code == 404
    assert client.post(f"/api/historical/imports/{job['import_id']}/decision", params={"choice": "replace"}).status_code == 409


def test_retry_reuses_staged_file_and_rejects_when_it_is_gone(client):
    h = preview(client, LAPS_A)
    bad = {**META, "year": 1800}  # invalid metadata fails the job
    failed = wait(client, start(client, h, bad))
    assert failed["state"] == "FAILED" and 1 <= len(failed["error"]) <= 500
    r = client.post(f"/api/historical/imports/{failed['import_id']}/retry")
    assert r.status_code == 202 and r.json()["import_id"] != failed["import_id"] and r.json()["retry_of"] == failed["import_id"]
    assert client.get(f"/api/historical/imports/{failed['import_id']}").json()["state"] == "FAILED"  # original untouched
    wait(client, r.json()["import_id"])
    (client.data_dir / "csv-uploads" / f"{h}.csv").unlink()
    n = len(client.get("/api/historical/imports").json())
    r = client.post(f"/api/historical/imports/{failed['import_id']}/retry")
    assert r.status_code == 409 and "upload the file again" in r.text
    assert len(client.get("/api/historical/imports").json()) == n


def test_duplicate_key_pauses_then_replace_swaps_the_session(client):
    h1, first = imported(client)
    sid = first["session_id"]
    h2 = preview(client, LAPS_B)
    paused = wait(client, start(client, h2))
    assert paused["state"] == "PROCESSING" and paused["awaiting_decision"]["existing_session_id"] == sid
    assert paused["awaiting_decision"]["options"] == ["replace", "cancel"]
    assert client.get(f"/api/historical/sessions/{sid}").json()["file_hash"] == h1  # unchanged while paused
    assert client.post(f"/api/historical/imports/{paused['import_id']}/decision", params={"choice": "bogus"}).status_code == 422
    assert client.post(f"/api/historical/imports/{paused['import_id']}/decision", params={"choice": "replace"}).status_code == 200
    done = wait(client, paused["import_id"])
    assert done["state"] == "COMPLETED" and done["session_id"] == sid
    assert client.get(f"/api/historical/sessions/{sid}").json()["file_hash"] == h2
    assert len(client.get("/api/historical/sessions").json()) == 1


def test_duplicate_key_cancel_choice_and_cancel_endpoint_keep_the_session(client):
    h1, first = imported(client)
    h2 = preview(client, LAPS_B)
    paused = wait(client, start(client, h2))
    r = client.post(f"/api/historical/imports/{paused['import_id']}/decision", params={"choice": "cancel"})
    assert r.status_code == 200 and r.json()["state"] == "CANCELLED"
    assert client.get(f"/api/historical/sessions/{first['session_id']}").json()["file_hash"] == h1
    again = wait(client, start(client, h2))
    assert client.post(f"/api/historical/imports/{again['import_id']}/cancel").json()["state"] == "CANCELLED"
    assert client.get(f"/api/historical/sessions/{first['session_id']}").json()["file_hash"] == h1


def test_unconfirmed_replacement_expires(client):
    client.hist.replace_timeout_s = 0.4
    h1, first = imported(client)
    paused = wait(client, start(client, preview(client, LAPS_B)))
    assert paused["awaiting_decision"]
    done = wait(client, paused["import_id"], FINAL)
    deadline = time.time() + 5
    while done["state"] not in FINAL and time.time() < deadline:
        time.sleep(0.1)
        done = client.get(f"/api/historical/imports/{paused['import_id']}").json()
    assert done["state"] == "CANCELLED" and "5 minutes" in done["error"]
    assert client.get(f"/api/historical/sessions/{first['session_id']}").json()["file_hash"] == h1
    assert client.post(f"/api/historical/imports/{paused['import_id']}/decision", params={"choice": "replace"}).status_code == 409


def test_failed_replacement_rolls_back_and_keeps_the_original(client, monkeypatch):
    h1, first = imported(client)
    paused = wait(client, start(client, preview(client, LAPS_B)))
    before = hist_rows(client)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(client.hist.repo, "save_session", boom)
    client.post(f"/api/historical/imports/{paused['import_id']}/decision", params={"choice": "replace"})
    done = wait(client, paused["import_id"])
    assert done["state"] == "FAILED" and "replacement failed" in done["error"]
    assert client.get(f"/api/historical/sessions/{first['session_id']}").json()["file_hash"] == h1
    assert hist_rows(client) == before


def test_replacement_is_one_transaction_in_the_repository(client):
    """A failure after the old session is deleted inside save_session rolls the deletion back too."""
    h1, first = imported(client)
    repo = client.hist.repo
    manifest = repo.manifest(first["session_id"])
    before = hist_rows(client)
    import pandas as pd
    bad_laps = pd.DataFrame({"driver": ["VER"], "lap_number": [object()]})  # cannot be written
    with pytest.raises(Exception):
        repo.save_session("csv_upload-new", {**manifest, "session_id": "csv_upload-new"}, bad_laps, pd.DataFrame(), {}, {},
                          replace_sid=first["session_id"])
    assert repo.exists(first["session_id"]) and hist_rows(client) == before


def test_delete_removes_every_dependent_and_is_blocked_while_importing(client):
    h, job = imported(client)
    sid = job["session_id"]
    assert hist_rows(client)["f1_hist_session"] == 1 and hist_rows(client)["f1_hist_lap"] > 0
    running = client.hist.jobs.create({"provider": "csv_upload", "csv": {"file_hash": h, "metadata": META, "units": UNITS}})
    for state in ("PROCESSING", "SAVING"):
        client.hist.jobs.update(running["import_id"], state=state)
        r = client.delete(f"/api/historical/sessions/{sid}")
        assert r.status_code == 409 and "import is in progress" in r.text
        assert client.get(f"/api/historical/sessions/{sid}").status_code == 200
    client.hist.jobs.update(running["import_id"], state="CANCELLED")
    assert client.delete(f"/api/historical/sessions/{sid}").status_code == 200
    assert all(n == 0 for t, n in hist_rows(client).items()), hist_rows(client)
    assert client.get(f"/api/historical/sessions/{sid}").status_code == 404


def test_delete_of_a_linked_session_needs_confirmation(client):
    _, job = imported(client)
    sid = job["session_id"]
    client.hist.set_experiment_link_lookup(lambda s: [{"experiment_id": "exp-7", "name": "Wing test"}] if s == sid else [])
    r = client.delete(f"/api/historical/sessions/{sid}")
    assert r.status_code == 409 and "Wing test" in r.text and "exp-7" in r.text and "keeps" in r.text
    assert client.get(f"/api/historical/sessions/{sid}").status_code == 200
    r = client.delete(f"/api/historical/sessions/{sid}", params={"confirm_linked": "true"})
    assert r.status_code == 200 and r.json()["linked_experiments"] == ["exp-7"]


def test_failed_deletion_reports_an_error_and_keeps_the_session(client, monkeypatch):
    _, job = imported(client)

    def boom(sid):
        raise RuntimeError("locked")

    monkeypatch.setattr(client.hist.repo, "delete_session", boom)
    r = client.delete(f"/api/historical/sessions/{job['session_id']}")
    assert r.status_code == 500 and "deleting session" in r.text
    assert client.get(f"/api/historical/sessions/{job['session_id']}").status_code == 200


def test_restart_fails_active_jobs_as_interrupted_and_hides_partial_sessions(client):
    _, done = imported(client)
    ids = {}
    for i, state in enumerate(("QUEUED", "PROCESSING", "SAVING")):
        j = client.hist.jobs.create({"provider": "csv_upload", "csv": {"file_hash": f"{i:064x}", "metadata": META, "units": UNITS}})
        if state != "QUEUED":
            client.hist.jobs.update(j["import_id"], state=state)
        ids[state] = j["import_id"]
    with client.app.state.engine.begin() as c:
        c.execute(text("INSERT INTO f1_hist_session (session_id, status, provider, source_type, verification, manifest_json, imported_at) "
                       "VALUES ('csv_upload-partial','importing','csv_upload','user_uploaded','unverified','{}','2024-01-01')"))
    assert client.hist.recover() == 3
    for state, jid in ids.items():
        j = client.get(f"/api/historical/imports/{jid}").json()
        assert j["state"] == "FAILED" and j["error_code"] == "INTERRUPTED" and j["error"].startswith("interrupted"), (state, j)
    assert client.get(f"/api/historical/imports/{done['import_id']}").json()["state"] == "COMPLETED"
    assert [s["session_id"] for s in client.get("/api/historical/sessions").json()] == [done["session_id"]]
    assert hist_rows(client)["f1_hist_session"] == 1
    client.hist.jobs.update(ids["QUEUED"], state="COMPLETED")  # a finished job never changes state again
    assert client.get(f"/api/historical/imports/{ids['QUEUED']}").json()["state"] == "FAILED"
