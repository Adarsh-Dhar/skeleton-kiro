"""Property test: unknown demo names give 404 from GET /api/demo/{name}.csv."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from f1lab.api.app import create_app

KNOWN_DEMOS = {"telemetry", "telemetry_ms"}

_SPECIAL = st.sampled_from(
    ["..", ".", "...", "..\\..", "%2e%2e", "%00", "~", " ", "  ", "-", "_", "telemetry.csv", "telemetry_ms.csv",
     "TELEMETRY", "telemetry ", " telemetry", "demo_telemetry", "telemetry%00", "..\\telemetry", "é", "遥测", "🏎️"]
)
_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/"),
    min_size=1,
    max_size=40,
)
# Names that still route to the handler: non-empty, no "/" (an empty segment or "/" would not match the route).
names = st.one_of(_SPECIAL, _TEXT, st.builds(lambda a, b: a + b, _SPECIAL, _TEXT)).filter(
    lambda n: n not in KNOWN_DEMOS and "/" not in n and n != ""
)


@pytest.fixture(scope="module")
def module_client(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("demo-names") / "trialforge.db")
    with TestClient(create_app(db)) as c:
        yield c


# Feature: f1-setup-lab-restoration, Property 9: Unknown demo names give 404
# **Validates: Requirements 5.8**
@settings(max_examples=200, deadline=None)
@given(name=names)
def test_unknown_demo_names_give_404(module_client, name):
    r = module_client.get(f"/api/demo/{quote(name, safe='')}.csv")
    assert r.status_code == 404, (name, r.status_code, r.text[:200])
    assert "demo not found" in r.text.lower(), (name, r.text[:200])
