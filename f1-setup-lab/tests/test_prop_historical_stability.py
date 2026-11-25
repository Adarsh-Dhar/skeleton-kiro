"""Property 10: the /api/historical/* contract and GET response shapes still match tests/fixtures/historical_routes.json."""
from __future__ import annotations

import json
import sys
import tempfile
from functools import cache, lru_cache
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_historical_manifest as gen  # noqa: E402

MANIFEST = json.loads(gen.DEFAULT_OUT.read_text())
ROUTES = MANIFEST["routes"]
SESSIONS = sorted(MANIFEST["observed"])


@lru_cache(maxsize=1)
def current_routes() -> dict:
    with tempfile.TemporaryDirectory(prefix="hist-stable-") as tmp:
        app = gen.create_app(str(Path(tmp) / "trialforge.db"), frontend_dir=str(Path(tmp) / "none"))
        routes = gen.route_contract(app.openapi())
    return {(r["method"], r["path"]): json.loads(json.dumps(r, sort_keys=True)) for r in routes}


@cache
def current_observed(session: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="hist-stable-") as tmp:
        d = json.loads((gen.FIXTURES / f"{session}.json").read_text())
        return json.loads(json.dumps(gen.observe(session, d, Path(tmp))))


# Feature: f1-setup-lab-restoration, Property 10: Historical routes and their responses are stable
# **Validates: Requirements 5.7, 9.9**
@settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))
@given(route=st.sampled_from(ROUTES), session=st.sampled_from(SESSIONS))
def test_historical_routes_and_responses_are_stable(route, session):
    key = (route["method"], route["path"])
    now = current_routes()
    assert key in now, f"route removed: {key}"
    assert now[key] == json.loads(json.dumps(route, sort_keys=True)), f"request/response schema changed: {key}"

    if route["method"] == "GET":
        obs_key = f"GET {route['path']}"
        expected = MANIFEST["observed"][session]
        if obs_key in expected:
            actual = current_observed(session)
            assert actual.get(obs_key) == expected[obs_key], f"{session}: response shape changed for {obs_key}"


def test_every_recorded_observation_is_still_produced():
    for session in SESSIONS:
        assert current_observed(session) == MANIFEST["observed"][session]
