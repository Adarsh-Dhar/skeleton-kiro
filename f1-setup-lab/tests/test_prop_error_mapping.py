"""Property test: domain errors map to their HTTP status and carry their message."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

import f1lab.errors as app_errors
import f1lab.services.lab as lab_module
from f1lab.api.app import create_app

_TMP = tempfile.TemporaryDirectory()
_DB = str(Path(_TMP.name) / "trialforge.db")


class _StubLab:
    """Minimal Lab_Service stand-in: ``detail`` raises whatever error is configured."""

    def __init__(self):
        self.exc: Exception | None = None

    async def startup(self):
        return []

    async def shutdown(self):
        return None

    def detail(self, exp_id):
        raise self.exc


_STUB = _StubLab()
_CLIENT_CM = TestClient(create_app(_DB, lab_factory=lambda: _STUB), raise_server_exceptions=False)
_CLIENT_CM.__enter__()

# printable text without surrogates; '' is allowed
_MESSAGES = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=60)


def _body_messages(resp) -> list[str]:
    return [i["message"] for i in resp.json()["detail"]]


# Feature: f1-setup-lab-restoration, Property 6: Domain errors map to their HTTP status and carry their message
# **Validates: Requirements 5.3, 5.4**
@settings(max_examples=100, deadline=None)
@given(msg=_MESSAGES, cls=st.sampled_from([app_errors.NotFound, lab_module.NotFound]))
def test_not_found_maps_to_404_with_message(msg, cls):
    _STUB.exc = cls(msg)
    resp = _CLIENT_CM.get("/api/experiments/x")
    assert resp.status_code == 404
    assert any(msg in m for m in _body_messages(resp))


# Feature: f1-setup-lab-restoration, Property 6: Domain errors map to their HTTP status and carry their message
# **Validates: Requirements 5.3, 5.4**
@settings(max_examples=100, deadline=None)
@given(msg=_MESSAGES, cls=st.sampled_from([app_errors.Conflict, lab_module.Conflict]))
def test_conflict_maps_to_409_with_message(msg, cls):
    _STUB.exc = cls(msg)
    resp = _CLIENT_CM.get("/api/experiments/x")
    assert resp.status_code == 409
    assert any(msg in m for m in _body_messages(resp))
