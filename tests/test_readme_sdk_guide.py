"""The "Use the TRIALFORGE SDK in your own app" walkthrough in README.md is executable: every ``# file: my_app/...`` code block
is written to a temporary project and run against the installed SDK, so the guide cannot drift from the real API."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _blocks() -> dict[str, str]:
    text = (ROOT / "README.md").read_text()
    section = text.split("## Use the TRIALFORGE SDK in your own app", 1)[1].split("\n## ", 1)[0]
    files = {}
    for code in re.findall(r"```python\n(.*?)```", section, re.S):
        first = code.splitlines()[0]
        if first.startswith("# file: "):
            files[first.removeprefix("# file: ").strip()] = code
    return files


def test_the_guide_contains_the_three_files():
    assert set(_blocks()) == {"my_app/adapter.py", "my_app/experiment.py", "my_app/web.py"}


@pytest.fixture
def project(tmp_path):
    pytest.importorskip("trialforge")
    (tmp_path / "my_app").mkdir()
    (tmp_path / "my_app" / "__init__.py").write_text("")
    for name, code in _blocks().items():
        (tmp_path / name).write_text(code)
    return tmp_path


def test_the_walkthrough_runs_end_to_end(project):
    r = subprocess.run([sys.executable, "-m", "my_app.experiment"], cwd=project, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout
    assert "runs: 18 completed: 18" in out and "best feasible run:" in out and "after optimisation: 20 runs" in out
    assert out.count("suggest:") == 2 and "infeasible runs (moisture < 30 %):" in out
    report = json.loads((project / "bake-001-report.json").read_text())
    assert report["best_configuration"] and (project / "bake-001-runs.csv").read_text().startswith("run_id")


def test_the_web_example_runs_in_the_background_and_ingests_manual_measurements(project):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    script = """
import time
from fastapi.testclient import TestClient
from my_app.web import app, service
with TestClient(app) as c:
    assert c.post("/experiments/bake-web").json()["planned_runs"] == 18
    first = service.get("bake-web").runs[0].run_id
    vals = {"score": {"value": 7.1, "unit": "points"}, "crust": {"value": 5.0, "unit": "index"}, "moisture": {"value": 34, "unit": "percent"}}
    assert c.post(f"/experiments/bake-web/runs/{first}/measurements", json={"source": "lab sheet", "values": vals}).json()["status"] == "completed"
    assert c.post("/experiments/bake-web/execute").status_code == 202
    for _ in range(400):
        s = c.get("/experiments/bake-web").json()
        if s["status"] == "completed":
            break
        time.sleep(0.05)
    assert s == {"status": "completed", "runs": {"completed": 18}}, s
    print("ok")
"""
    r = subprocess.run([sys.executable, "-c", script], cwd=project, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:]
