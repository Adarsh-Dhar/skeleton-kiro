"""Fixtures: API client on a temp database, and a live server for browser tests."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from wattwise.api.app import create_app


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "wattwise.db")


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path)) as c:
        yield c


def wait_done(client, exp_id, timeout=60):
    """Poll until no run is queued/running."""
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/experiments/{exp_id}").json()
        if d["counts"]["queued"] + d["counts"]["running"] == 0:
            return d
        time.sleep(0.05)
    raise AssertionError(f"experiment {exp_id} did not finish in {timeout}s")


@pytest.fixture
def make_experiment(client):
    counter = iter(range(1, 1000))

    def _make(**kw):
        body = {"name": "test experiment", "experiment_id": f"test-exp-{next(counter)}", **kw}
        body.setdefault("simulator", {"latency_seconds": 0.0})
        r = client.post("/api/experiments", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    return _make


@pytest.fixture
def run_experiment(client, make_experiment):
    def _run(**kw):
        d = make_experiment(**kw)
        assert client.post(f"/api/experiments/{d['id']}/execute").status_code == 202
        return wait_done(client, d["id"])

    return _run


class LiveServer:
    def __init__(self, db_path):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(create_app(db_path), host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    srv = LiveServer(str(tmp_path_factory.mktemp("live") / "ui.db")).start()
    yield srv
    srv.stop()
