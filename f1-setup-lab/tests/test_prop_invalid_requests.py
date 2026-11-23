"""Property 8: invalid experiment requests are rejected naming every invalid field, and nothing is created."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from f1lab.api.app import create_app

MISSING = object()

VALID = {"name": "valid experiment", "hypothesis": "h", "min_stability": 85.0, "design": {"replicates": 2}, "simulator": {"latency_seconds": 0.0}}

# dotted field path -> candidate invalid values (MISSING removes the key). Each path is an independent field.
INVALID = {
    "name": [MISSING, 123, "", "   ", "a" * 81, None],
    "hypothesis": [5, "h" * 1001, ["x"]],
    "experiment_id": ["Bad ID", "ab", 7, "-start"],
    "mode": ["bogus", 3],
    "min_stability": [-1, 100.5, "abc", None],
    "also_minimise_tyre_wear": ["notabool"],
    "design.replicates": [0, 11, "x", 1.5],
    "design.method": ["bogus", 4],
    "design.seed": [-1, "s"],
    "simulator.latency_seconds": [-0.5, 6, "slow"],
    "simulator.max_retries": [-1, 6, "many"],
    "simulator.fault_rate": [1.5, -0.1],
    "factors.front_wing.low": ["abc", None],
    "factors.rear_wing.enabled": ["maybe"],
}


def _set(body: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = body
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    if value is MISSING:
        cur.pop(parts[-1], None)
    else:
        cur[parts[-1]] = value


@st.composite
def invalid_bodies(draw):
    paths = draw(st.lists(st.sampled_from(sorted(INVALID)), min_size=1, max_size=5, unique=True))
    body = copy.deepcopy(VALID)
    # a factors.* entry needs a complete factor object for the other sub-fields to be valid
    for path in paths:
        if path.startswith("factors."):
            _, fname, _ = path.split(".")
            body.setdefault("factors", {}).setdefault(fname, {"enabled": True, "low": 10, "high": 40})
    for path in paths:
        _set(body, path, draw(st.sampled_from(INVALID[path])))
    return body, paths


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("prop8") / "trialforge.db")
    with TestClient(create_app(db)) as c:
        yield c


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(case=invalid_bodies())
def test_invalid_experiment_requests_name_every_invalid_field(api, case):
    # Feature: f1-setup-lab-restoration, Property 8: Invalid experiment requests are rejected naming every invalid field
    # **Validates: Requirements 5.9**
    body, paths = case
    before = api.get("/api/experiments").json()

    r = api.post("/api/experiments", json=body)

    assert r.status_code == 422, r.text
    fields = {d["field"] for d in r.json()["detail"]}
    for path in paths:
        assert path in fields, f"{path} not named in {sorted(fields)}"
    assert api.get("/api/experiments").json() == before
