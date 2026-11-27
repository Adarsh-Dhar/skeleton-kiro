"""Restart recovery, persistence, and proof that WATTWISE uses the installed TRIALFORGE package without modifying it."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest
from conftest import wait_done

BACKEND = Path(__file__).resolve().parents[1] / "backend" / "wattwise"
SKELETON = Path(__file__).resolve().parents[2] / "skeleton"


# ------------------------------------------------------------------ persistence
def test_experiments_and_tariffs_survive_restart_and_reanalysis_is_identical(db_path):
    from fastapi.testclient import TestClient
    from inputs import inputs, seed_tariffs, tariff

    from wattwise.api.app import create_app
    t = tariff("Persisted", [{"label": "all", "start": 0, "end": 24, "price": 0.3}])
    with TestClient(create_app(db_path)) as c1:
        seed_tariffs(c1)
        eid = c1.post("/api/experiments", json=inputs(name="persist", simulator={"latency_seconds": 0}, design={"method": "ofat", "replicates": 1})).json()["id"]
        c1.post(f"/api/experiments/{eid}/execute")
        before = wait_done(c1, eid)
        a1 = c1.post(f"/api/experiments/{eid}/analysis", json={}).json()
        assert c1.post("/api/tariffs", json=t).status_code == 201
    with TestClient(create_app(db_path)) as c2:
        after = c2.get(f"/api/experiments/{eid}").json()
        assert after["runs"] == before["runs"] and after["status"] == "completed" and [e["id"] for e in c2.get("/api/experiments").json()] == [eid]
        assert any(x["id"] == "persisted" for x in c2.get("/api/tariffs").json())
        a2 = c2.post(f"/api/experiments/{eid}/analysis", json={}).json()
        assert a2["effects"] == a1["effects"] and a2["best_run_id"] == a1["best_run_id"]
        # the per-experiment simulator was re-registered at start-up, so more runs can still be executed
        c2.post(f"/api/experiments/{eid}/optimise/apply", json={"count": 1})
        assert wait_done(c2, eid)["runs_total"] == before["runs_total"] + 1


async def test_interrupted_runs_are_recovered_after_a_crash(db_path):
    from inputs import TARIFFS, inputs

    from wattwise.api.schemas import ExperimentRequest
    from wattwise.services.lab import LabService
    lab = LabService(db_path)
    lab.tariffs.save(TARIFFS[0])
    d = lab.create(ExperimentRequest(**inputs(name="crash", simulator={"latency_seconds": 5.0}, design={"method": "ofat", "replicates": 1})))
    lab.execute(d["id"])
    await asyncio.sleep(0.5)
    assert lab.detail(d["id"])["counts"]["running"] > 0
    for task in lab.runner._tasks.values():  # simulate process death mid-run
        task.cancel()
    await asyncio.gather(*lab.runner._tasks.values(), return_exceptions=True)
    stuck = lab.detail(d["id"])["counts"]["running"]
    assert stuck > 0
    lab.repo.engine.dispose()
    lab2 = LabService(db_path)
    actions = await lab2.startup()
    assert len(actions) == stuck and {a["action"] for a in actions} == {"retry"}  # the simulator declares itself idempotent
    assert lab2.detail(d["id"])["counts"]["running"] == 0
    lab2.svc.registry.get(f"wattwise-sim/{d['id']}").latency = 0.0
    lab2.execute(d["id"])
    await lab2.runner.wait(d["id"])
    final = lab2.detail(d["id"])
    assert final["counts"]["completed"] == final["runs_total"] and len(final["runs"]) == d["runs_total"]
    await lab2.shutdown()


def test_health_reports_recovery_and_app_state(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["app"] == "wattwise-lab" and h["recovered"] == []


# ------------------------------------------------------------------ Skeleton Crew compliance
def test_application_runs_on_the_installed_sdk_not_a_copy(client):
    import trialforge

    import wattwise

    h = client.get("/api/health").json()
    assert h["trialforge"] == trialforge.__version__ and trialforge.__version__.startswith("0.2.") and "site-packages" in h["trialforge_path"]
    lab = client.app.state.lab
    assert type(lab.svc) is trialforge.ExperimentService and isinstance(lab.repo, trialforge.SQLiteExperimentRepository) and type(lab.repo).__mro__[1] is trialforge.SQLiteExperimentRepository  # app-scoped subclass of the unmodified SDK repository and type(lab.runner) is trialforge.LocalBackgroundRunner
    assert not hasattr(wattwise, "ExperimentService") and "examples" not in sys.modules
    assert not any(Path(p).resolve() == SKELETON / "packages" / "trialforge-core" / "src" for p in sys.path if p)


def test_wattwise_only_uses_the_public_top_level_sdk_api():
    """No deep imports such as ``trialforge.analysis.engine``: only names the SDK exports from the package root."""
    import ast

    import trialforge

    imported: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "trialforge":
                assert node.module == "trialforge", f"deep SDK import in {path.name}: {node.module}"
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                assert all(not a.name.startswith("trialforge.") for a in node.names), f"deep SDK import in {path.name}"
    assert imported, "expected WATTWISE to import from trialforge"
    missing = sorted(n for n in imported if not hasattr(trialforge, n))
    assert not missing, f"not exported by trialforge: {missing}"


def test_sdk_source_has_no_domain_knowledge_of_either_application():
    src = SKELETON / "packages" / "trialforge-core" / "src" / "trialforge"
    if not src.is_dir():
        pytest.skip("skeleton source not present next to the application")
    banned = re.compile(r"wattwise|f1lab|tariff|tyre|wing|charge_start|lap_time|departure_charge", re.I)
    offenders = [p.name for p in src.rglob("*.py") if banned.search(p.read_text())]
    assert offenders == [], f"domain terms found in SDK modules: {offenders}"


def test_installed_sdk_version_matches_the_distributed_wheel():
    import trialforge

    wheels = sorted((SKELETON / "dist").glob("trialforge_core-*.whl")) if (SKELETON / "dist").is_dir() else []
    if not wheels:
        pytest.skip("no wheel found in skeleton/dist")
    assert any(f"-{trialforge.__version__}-" in w.name for w in wheels)
