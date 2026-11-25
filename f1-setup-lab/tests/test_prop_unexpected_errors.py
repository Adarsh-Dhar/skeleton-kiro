"""Property test: unexpected Lab_Service exceptions give a fixed-shape 500 without a stack trace."""
from __future__ import annotations

import hashlib
import re

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from f1lab.api.app import create_app

FIXED_BODY = {"detail": [{"field": "server", "message": "internal error"}]}

# (method, path, json body) for every restored route that calls the Lab_Service.
ROUTES = [
    ("GET", "/api/experiments", None),
    ("POST", "/api/experiments", {"name": "x"}),
    ("GET", "/api/experiments/e1", None),
    ("GET", "/api/experiments/e1/runs", None),
    ("POST", "/api/experiments/e1/execute", None),
    ("POST", "/api/experiments/e1/cancel", None),
    ("POST", "/api/experiments/e1/retry", None),
    ("POST", "/api/telemetry/preview", {"csv": "a,b\n1,2"}),
    ("GET", "/api/experiments/e1/telemetry", None),
    ("POST", "/api/experiments/e1/runs/r1/telemetry", {"csv": "a,b\n1,2"}),
    ("POST", "/api/experiments/e1/analysis", {}),
    ("GET", "/api/experiments/e1/recommendation", None),
    ("POST", "/api/experiments/e1/optimise/suggest", {}),
    ("POST", "/api/experiments/e1/optimise/apply", {}),
    ("GET", "/api/experiments/e1/optimise/history", None),
    ("GET", "/api/experiments/e1/report", None),
    ("GET", "/api/experiments/e1/report.json", None),
    ("GET", "/api/experiments/e1/report.html", None),
    ("GET", "/api/experiments/e1/export/runs.csv", None),
]


class _Repo:
    def __init__(self, stub):
        self._stub = stub

    @property
    def db_engine(self):
        if self._stub.exc is not None:
            raise self._stub.exc
        return None


class _FailingLab:
    """Lab_Service stand-in: every service call raises ``self.exc`` once armed."""

    def __init__(self):
        self.exc = None
        self.calls = 0
        self.repo = _Repo(self)

    async def startup(self):
        return []

    async def shutdown(self):
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _raise(*_a, **_k):
            self.calls += 1
            raise self.exc

        return _raise


# Exception classes that the app maps to other statuses, or that are not plain unexpected errors.
_BASES = [Exception, RuntimeError, KeyError, OSError, TypeError, ZeroDivisionError, AttributeError,
          LookupError, ArithmeticError, AssertionError, MemoryError, NotImplementedError]  # no ValueError family (mapped to 422)
_names = st.from_regex(r"[A-Z][A-Za-z0-9]{0,20}", fullmatch=True)
_messages = st.one_of(
    st.text(max_size=80),
    st.sampled_from(["Traceback (most recent call last):", 'File "/app/x.py", line 3, in f', "/Users/a/b.py", "C:\\x\\y.py"]),
)
excs = st.builds(
    lambda name, base, msg: type(name, (base,), {})(msg),
    _names, st.sampled_from(_BASES), _messages,
)

_PATH_RE = re.compile(r'(?:/[\w.\-]+){2,}\.py|File "|line \d+, in |\.py\b')


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("unexpected") / "trialforge.db")
    lab = _FailingLab()
    with TestClient(create_app(db, lab_factory=lambda: lab), raise_server_exceptions=False) as c:
        yield c, lab, db


def _digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# Feature: f1-setup-lab-restoration, Property 7: Unexpected errors give a 500 without a stack trace
# **Validates: Requirements 5.10**
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(exc=excs, route=st.sampled_from(ROUTES))
def test_unexpected_errors_give_500_without_stack_trace(env, exc, route):
    client, lab, db = env
    method, path, body = route
    lab.exc = exc
    before = _digest(db)
    try:
        r = client.request(method, path, json=body)
    finally:
        lab.exc = None
    assert r.status_code == 500, (type(exc).__name__, route, r.status_code, r.text[:200])
    assert r.json() == FIXED_BODY, (route, r.text[:200])
    assert "Traceback" not in r.text and not _PATH_RE.search(r.text), r.text[:200]
    assert _digest(db) == before, "the error handler changed the database"
