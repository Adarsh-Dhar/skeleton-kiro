"""Property tests for the Setup_Link (task 6.5): Properties 32-35.

Drives the real endpoint (``POST /api/historical/sessions/{id}/setup-link``) against a session imported from a small CSV.
"""
from __future__ import annotations

import itertools
import time

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from trialforge.measurements.quality import is_usable

from f1lab.api.app import create_app

LAP_NUMBERS = [1, 2, 3, 4, 5]
LAPS = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s\n" + "".join(f"{i},30.{i},40.{i},20.{i},90.{i}\n" for i in LAP_NUMBERS)
META = {"year": 2024, "event": "Imola test", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}
AVAILABLE = ["lap_time", "sector_1", "sector_2", "sector_3"]  # the CSV has no telemetry, so top_speed has no values
UNAVAILABLE = ["top_speed", "stability", "nonsense"]

SETTINGS = settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))
_counter = itertools.count()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("prop_setup_link")
    with TestClient(create_app(str(tmp / "trialforge.db"), frontend_dir=str(tmp / "none"))) as c:
        h = c.post("/api/historical/csv/preview", files={"file": ("laps.csv", LAPS.encode(), "text/csv")}).json()["file_hash"]
        jid = c.post("/api/historical/import", json={"provider": "csv_upload", "csv": {"file_hash": h, "metadata": META,
                     "units": {"time": "s", "distance": "m"}}}).json()["import_id"]
        end = time.time() + 30
        while c.get(f"/api/historical/imports/{jid}").json()["state"] not in ("COMPLETED", "FAILED"):
            assert time.time() < end
            time.sleep(0.05)
        sess = c.get("/api/historical/sessions").json()[0]
        yield c, sess["session_id"]


def _new_experiment(c) -> dict:
    eid = f"exp-p{next(_counter)}"
    r = c.post("/api/experiments", json={"name": eid, "experiment_id": eid, "simulator": {"latency_seconds": 0.0},
                                         "design": {"run_budget": 50}})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _runs_total(c, eid: str) -> int:
    return c.get(f"/api/experiments/{eid}").json()["runs_total"]


def _stored(c, run_id: str):
    return c.app.state.lab.repo.get_run(run_id).measurements


def _post(c, sid, body):
    return c.post(f"/api/historical/sessions/{sid}/setup-link", json=body)


def _verified_factors(exp, verified=True):
    return {f["name"]: {"value": f["low"], "verified": verified} for f in exp["factors"]}


# ---------------------------------------------------------------- strategies
laps_valid = st.lists(st.sampled_from(LAP_NUMBERS), min_size=1, max_size=5, unique=True)
metrics_valid = st.lists(st.sampled_from(AVAILABLE), min_size=1, max_size=4, unique=True)
# Selections that may or may not be acceptable.
laps_any = st.one_of(st.just([]), st.lists(st.integers(-2, 8), max_size=8), st.lists(st.integers(1, 300), min_size=90, max_size=130, unique=True))
metrics_any = st.lists(st.sampled_from(AVAILABLE + UNAVAILABLE), max_size=7)
drivers_any = st.sampled_from(["VER", "VER", "VER", "", None])
target_any = st.sampled_from(["existing", "existing", "none"])


@given(driver=drivers_any, laps=laps_any, metrics=metrics_any, target=target_any)
@SETTINGS
def test_property_32_selection_constraints_enforced(env, driver, laps, metrics, target):
    """**Validates: Requirements 15.2, 15.3**"""
    c, sid = env
    exp = _new_experiment(c)
    body = {"driver": driver, "laps": laps, "metrics": metrics, "factors": _verified_factors(exp)}
    if target == "existing":
        body["experiment_id"] = exp["id"] if "id" in exp else exp["experiment_id"]
    before = _runs_total(c, exp.get("id") or exp["experiment_id"])
    ok = (driver == "VER" and 1 <= len(set(laps)) <= 100 and set(laps) <= set(LAP_NUMBERS)
          and 1 <= len(set(metrics)) <= 50 and set(metrics) <= set(AVAILABLE) and target == "existing")
    r = _post(c, sid, body)
    if ok:
        assert r.status_code == 201, r.text
        assert r.json()["measurements_created"] == len(set(laps)) * len(set(metrics))
    else:
        assert r.status_code == 422, (r.status_code, r.text)
        assert _runs_total(c, exp.get("id") or exp["experiment_id"]) == before


@given(laps=laps_valid)
@SETTINGS
def test_property_32_options_mark_metrics_without_values_unavailable(env, laps):
    """**Validates: Requirements 15.3**"""
    c, sid = env
    lab, hist = c.app.state.lab, c.app.state.historical
    opts = lab.setup_link_options(hist, sid, "VER", laps)
    avail = {m["metric"]: m["available"] for m in opts["metrics"]}
    assert all(avail[m] for m in AVAILABLE) and not avail["top_speed"]


# ---------------------------------------------------------------- Property 33
@st.composite
def configurations(draw):
    """Which factors get a missing value / missing verification statement, as index sets (chosen once the experiment is known)."""
    return draw(st.lists(st.sampled_from(["ok", "no_value", "no_flag", "absent"]), min_size=1, max_size=8))


@given(laps=laps_valid, metrics=metrics_valid, kinds=configurations(), verified=st.booleans())
@SETTINGS
def test_property_33_completeness_gates_measurement_creation(env, laps, metrics, kinds, verified):
    """**Validates: Requirements 15.4, 15.5, 15.6**"""
    c, sid = env
    exp = _new_experiment(c)
    eid = exp.get("id") or exp["experiment_id"]
    names = [f["name"] for f in exp["factors"]]
    factors, missing = {}, []
    for i, name in enumerate(names):
        kind = kinds[i % len(kinds)]
        value = next(f["low"] for f in exp["factors"] if f["name"] == name)
        if kind == "ok":
            factors[name] = {"value": value, "verified": verified}
        elif kind == "no_value":
            factors[name] = {"value": None, "verified": verified}
            missing.append(name)
        elif kind == "no_flag":
            factors[name] = {"value": value, "verified": None}
            missing.append(name)
        else:
            missing.append(name)
    before = _runs_total(c, eid)
    r = _post(c, sid, {"driver": "VER", "laps": laps, "metrics": metrics, "experiment_id": eid, "factors": factors})
    if not missing:
        assert r.status_code == 201, r.text
        out = r.json()
        assert out["measurements_created"] == len(laps) * len(metrics)
        ms = _stored(c, out["run_id"])
        assert len(ms) == len(laps) * len(metrics)
        assert sorted((m.channel, m.metric) for m in ms) == sorted((f"lap_{lap}", m) for lap in laps for m in metrics)
        assert all(m.metadata["provenance_status"] == out["provenance_status"] and out["provenance_status"] for m in ms)
    else:
        assert r.status_code == 422, (r.status_code, r.text)
        assert all(n in r.text for n in missing)
        assert _runs_total(c, eid) == before
        assert c.app.state.lab.linked_experiments(sid) is not None
        runs = c.get(f"/api/experiments/{eid}").json()["runs"]
        assert all(x.get("source") != "setup_link" for x in runs)


# ---------------------------------------------------------------- Property 34
@given(laps=laps_valid, metrics=metrics_valid, verified=st.booleans())
@SETTINGS
def test_property_34_unverified_mappings_excluded_from_training(env, laps, metrics, verified):
    """**Validates: Requirements 15.7**"""
    c, sid = env
    exp = _new_experiment(c)
    eid = exp.get("id") or exp["experiment_id"]
    r = _post(c, sid, {"driver": "VER", "laps": laps, "metrics": metrics, "experiment_id": eid,
                       "factors": _verified_factors(exp, verified)})
    assert r.status_code == 201, r.text
    out = r.json()
    ms = _stored(c, out["run_id"])
    assert len(ms) == len(laps) * len(metrics)
    assert out["excluded_from_training"] is (not verified)
    usable = [m for m in ms if is_usable(m.quality_flags)]
    if verified:
        assert len(usable) == len(ms) and all("unverified_setup" not in m.quality_flags for m in ms)
    else:
        assert usable == [] and all("unverified_setup" in m.quality_flags for m in ms)


# ---------------------------------------------------------------- Property 35
@given(laps=laps_valid, metrics=metrics_valid, verified=st.booleans())
@SETTINGS
def test_property_35_measurements_carry_source_session_id(env, laps, metrics, verified):
    """**Validates: Requirements 15.8**"""
    c, sid = env
    exp = _new_experiment(c)
    eid = exp.get("id") or exp["experiment_id"]
    r = _post(c, sid, {"driver": "VER", "laps": laps, "metrics": metrics, "experiment_id": eid,
                       "factors": _verified_factors(exp, verified)})
    assert r.status_code == 201, r.text
    assert r.json()["source_session_id"] == sid
    ms = _stored(c, r.json()["run_id"])
    assert ms and all(m.metadata["source_session_id"] == sid for m in ms)
    assert c.app.state.lab.linked_experiments(sid) and eid in [x["experiment_id"] for x in c.app.state.lab.linked_experiments(sid)]
