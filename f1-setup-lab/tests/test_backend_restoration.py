"""Backend restoration checks (requirements 1.1-1.4, 4.1-4.9, 5.1, 5.2, 5.5, 5.6, 6.5).

Manifest comparison against tests/fixtures/restoration_manifest.json, AST checks, a fresh-interpreter import check,
pyproject parsing, restored routes against a stub lab, Lab_Factory isolation, /api/meta and /api/health shapes, and the
route list (no route converts between historical and setup data).
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from db import create_db_engine, migrate
from f1lab.api.app import create_app

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
BACKEND = APP_DIR / "backend" / "f1lab"
MANIFEST = json.loads((Path(__file__).parent / "fixtures" / "restoration_manifest.json").read_text())


# ----------------------------------------------------------------------------- manifest (4.1-4.4, 4.9)
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_manifest(files: dict[str, str], root: Path) -> list[str]:
    """One problem line per missing or differing file; every file is checked, so each offender is named."""
    problems = []
    for rel, expected in sorted(files.items()):
        p = root / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
        elif sha256(p) != expected:
            problems.append(f"differs: {rel}")
    return problems


def test_manifest_files_match_current_tree():
    problems = check_manifest(MANIFEST["files"], REPO_ROOT)
    assert not problems, "restored files missing or differing from the release:\n" + "\n".join(problems)


def test_manifest_excludes_files_modified_on_restore():
    modified = set(MANIFEST["modified_on_restore"])
    assert modified, "manifest lists no modified_on_restore files"
    assert not modified & set(MANIFEST["files"]), "modified_on_restore files must not be in 'files'"
    for rel in modified:
        assert (REPO_ROOT / rel).is_file(), f"modified_on_restore file is missing: {rel}"


def test_manifest_covers_the_required_restored_modules():
    names = set(MANIFEST["files"])
    base = "f1-setup-lab/backend/f1lab/"
    for rel in ("adapters/vehicle_model.py", "adapters/sim_adapter.py", "adapters/telemetry.py"):
        assert base + rel in names, rel
    for rel in ("services/lab.py", "services/narrative.py", "services/report_html.py"):  # edited when the recorded-stint feature was removed
        assert base + rel in MANIFEST["modified_on_restore"], rel
        assert (REPO_ROOT / (base + rel)).is_file(), rel
    # the recorded-stint feature was removed: only the historical integration and the synthetic setup packages remain
    assert {p.name for p in (BACKEND / "integrations").iterdir() if p.is_dir() and p.name != "__pycache__"} == {"historical"}
    assert {p.name for p in BACKEND.iterdir() if p.is_dir() and p.name != "__pycache__"} == {"adapters", "api", "integrations", "services"}
    assert not [n for n in names if n.endswith("inspector.js")]
def test_checker_names_every_missing_and_differing_file_in_a_tampered_copy(tmp_path):
    for rel in MANIFEST["files"]:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dest)
    assert check_manifest(MANIFEST["files"], tmp_path) == []
    rels = sorted(MANIFEST["files"])
    changed, changed2, removed, removed2 = rels[0], rels[2], rels[4], rels[-1]
    with open(tmp_path / changed, "ab") as f:
        f.write(b"\n# tampered\n")
    (tmp_path / changed2).write_bytes(b"")
    (tmp_path / removed).unlink()
    (tmp_path / removed2).unlink()
    problems = check_manifest(MANIFEST["files"], tmp_path)
    assert sorted(problems) == sorted([f"differs: {changed}", f"differs: {changed2}", f"missing: {removed}", f"missing: {removed2}"])


# ----------------------------------------------------------------------------- AST checks (4.5, 4.6, 4.7)
def class_names(path: Path) -> list[str]:
    return [n.name for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.ClassDef)]


def test_schema_class_names_are_unique_across_api_and_historical_schemas():
    api = class_names(BACKEND / "api" / "schemas.py")
    hist = class_names(BACKEND / "integrations" / "historical" / "schemas.py")
    assert api and hist
    allnames = api + hist
    dupes = sorted({n for n in allnames if allnames.count(n) > 1})
    assert not dupes, f"class names defined more than once: {dupes}"
    for n in ("ExperimentRequest", "TelemetryRequest", "HistoricalImportRequest", "HistoricalCompareRequest"):
        assert n in allnames


def test_no_fastf1_import_anywhere_in_f1lab():
    offenders = []
    files = sorted(BACKEND.rglob("*.py"))
    assert files
    for p in files:
        for node in ast.walk(ast.parse(p.read_text(), filename=str(p))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            if any(m.split(".")[0].lower() == "fastf1" for m in mods):
                offenders.append(f"{p.relative_to(APP_DIR)}:{node.lineno}")
    assert not offenders, offenders


EXPECTED_SYNTHETIC = (
    "SYNTHETIC DATA: the vehicle model is an invented response surface. This is not real Formula 1 telemetry, "
    "and synthetic experiments use no car or timing data."
)
EXPECTED_HISTORICAL = (
    "HISTORICAL F1 DATA: real Formula 1 session data from OpenF1 (unofficial, not affiliated with Formula 1). "
    "Used for analysis only; some values are estimated from it, and the import coverage on each session says which."
)


def _literal_assignments(path: Path) -> dict:
    """Module-level NAME = <expression of string literals / dict of names>, evaluated from the AST only."""
    env: dict = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                env[node.targets[0].id] = eval(compile(ast.Expression(node.value), "<ast>", "eval"), {}, dict(env))  # noqa: S307
            except Exception:  # not a pure literal expression
                pass
    return env


def test_notices_equal_expected_values_from_the_ast():
    env = _literal_assignments(BACKEND / "__init__.py")
    assert env["SYNTHETIC_NOTICE"] == EXPECTED_SYNTHETIC
    assert "SIM_RECORDED_NOTICE" not in env
    assert env["NOTICES"] == {"synthetic": EXPECTED_SYNTHETIC, "historical": EXPECTED_HISTORICAL}
    assert env["__version__"] == "2.1.0"


def test_notices_exported_at_runtime_equal_expected_values():
    import f1lab

    assert f1lab.SYNTHETIC_NOTICE == EXPECTED_SYNTHETIC
    assert f1lab.NOTICES["synthetic"] == EXPECTED_SYNTHETIC
    assert set(f1lab.NOTICES) == {"synthetic", "historical"}
    assert f1lab.NOTICES["historical"] == EXPECTED_HISTORICAL


# ----------------------------------------------------------------------------- import isolation (4.8)
def test_importing_historical_loads_no_adapter_simulator_or_lab_modules():
    code = (
        "import sys, f1lab.integrations.historical\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('f1lab.adapters', 'f1lab.services')))\n"
        "print('BAD:' + ','.join(bad))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=APP_DIR)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "BAD:", r.stdout


# ----------------------------------------------------------------------------- pyproject (1.1-1.4)
PYPROJECT = tomllib.loads((APP_DIR / "pyproject.toml").read_text())


def _all_dependency_strings() -> list[str]:
    proj = PYPROJECT["project"]
    deps = list(proj.get("dependencies", []))
    for group in proj.get("optional-dependencies", {}).values():
        deps += group
    for group in PYPROJECT.get("dependency-groups", {}).values():
        deps += [d for d in group if isinstance(d, str)]
    deps += PYPROJECT.get("build-system", {}).get("requires", [])
    return deps


def test_pyproject_declares_exactly_one_trialforge_core_dependency_with_the_range():
    deps = [d for d in _all_dependency_strings() if re.match(r"\s*trialforge[-_.]core\b", d, re.I)]
    assert len(deps) == 1, deps
    assert re.sub(r"\s", "", deps[0].split("#")[0]) == "trialforge-core>=0.2.0,<0.3"


def test_pyproject_version_and_description():
    proj = PYPROJECT["project"]
    assert proj["version"] == "2.1.0"
    desc = proj["description"]
    assert 1 <= len(desc) < 200
    for ws in ("Setup Engineering", "Historical F1"):
        assert ws in desc


def test_pyproject_does_not_declare_fastf1_anywhere():
    assert not [d for d in _all_dependency_strings() if "fastf1" in d.lower()]
    assert "fastf1" not in json.dumps(PYPROJECT["project"]).lower()


def test_pyproject_ruff_has_no_e702_exemptions_left():
    ruff = PYPROJECT["tool"]["ruff"]
    lint = ruff.get("lint", {})
    for key in ("ignore", "extend-ignore"):
        assert "E702" not in lint.get(key, []) and "E702" not in ruff.get(key, [])
    for table in (lint.get("per-file-ignores", {}), ruff.get("per-file-ignores", {}), lint.get("extend-per-file-ignores", {})):
        assert not {pat for pat, rules in table.items() if "E702" in rules}
# ----------------------------------------------------------------------------- restored routes with a stub lab (5.1, 5.2)
ASYNC_METHODS = {"startup", "shutdown", "cancel", "attach_telemetry", "analyse", "recommendation",
                 "optimise_suggest", "optimise_apply", "report", "report_html"}
TEXT_METHODS = {"csv_export", "csv_notice", "report_html"}


class StubLab:
    """Answers every LabService method the restored routes call; records the calls."""

    def __init__(self, db_engine):
        self.repo = SimpleNamespace(db_engine=db_engine)
        self.calls: list[str] = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        value = [] if name == "startup" else ("text" if name in TEXT_METHODS else {"ok": True, "stub": name})
        if name in ASYNC_METHODS:
            async def acall(*a, **k):
                self.calls.append(name)
                return value
            return acall

        def call(*a, **k):
            self.calls.append(name)
            return value
        return call


RESTORED_PREFIXES = ("/api/experiments", "/api/telemetry/preview", "/api/demo/")
EXP = "/api/experiments/e1"
RUN = f"{EXP}/runs/r1"
REQUESTS = {  # (method, route template) -> (concrete url, json body)
    ("GET", "/api/experiments"): ("/api/experiments", None),
    ("POST", "/api/experiments"): ("/api/experiments", {"name": "stub experiment"}),
    ("GET", "/api/experiments/{exp_id}"): (EXP, None),
    ("GET", "/api/experiments/{exp_id}/runs"): (f"{EXP}/runs", None),
    ("POST", "/api/experiments/{exp_id}/execute"): (f"{EXP}/execute", None),
    ("POST", "/api/experiments/{exp_id}/cancel"): (f"{EXP}/cancel", None),
    ("POST", "/api/experiments/{exp_id}/retry"): (f"{EXP}/retry", None),
    ("POST", "/api/telemetry/preview"): ("/api/telemetry/preview", {"csv": "lap,sector_1\n1,28\n"}),
    ("GET", "/api/experiments/{exp_id}/telemetry"): (f"{EXP}/telemetry", None),
    ("POST", "/api/experiments/{exp_id}/runs/{run_id}/telemetry"): (f"{RUN}/telemetry", {"csv": "lap,sector_1\n1,28\n"}),
    ("POST", "/api/experiments/{exp_id}/analysis"): (f"{EXP}/analysis", None),
    ("GET", "/api/experiments/{exp_id}/recommendation"): (f"{EXP}/recommendation", None),
    ("POST", "/api/experiments/{exp_id}/optimise/suggest"): (f"{EXP}/optimise/suggest", None),
    ("POST", "/api/experiments/{exp_id}/optimise/apply"): (f"{EXP}/optimise/apply", None),
    ("GET", "/api/experiments/{exp_id}/optimise/history"): (f"{EXP}/optimise/history", None),
    ("GET", "/api/experiments/{exp_id}/report"): (f"{EXP}/report", None),
    ("GET", "/api/experiments/{exp_id}/report.json"): (f"{EXP}/report.json", None),
    ("GET", "/api/experiments/{exp_id}/report.html"): (f"{EXP}/report.html", None),
    ("GET", "/api/experiments/{exp_id}/export/{kind}.csv"): (f"{EXP}/export/runs.csv", None),
    ("GET", "/api/demo/{name}.csv"): ("/api/demo/telemetry.csv", None),
}


def restored_routes(app) -> set[tuple[str, str]]:
    out = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        if path.startswith(RESTORED_PREFIXES):
            out |= {(m, path) for m in r.methods if m not in ("HEAD", "OPTIONS")}
    return out


@pytest.fixture
def stub_app(tmp_path, monkeypatch):
    """create_app on a temp database with a StubLab; the default database is redirected to a path that must stay absent."""
    default_db = tmp_path / "default" / "default.db"
    monkeypatch.setenv("TRIALFORGE_DB", str(default_db))
    db = tmp_path / "work" / "trialforge.db"
    db.parent.mkdir()
    engine = create_db_engine(f"sqlite:///{db}")
    migrate(engine)
    stub = StubLab(engine)

    def boom(*a, **k):
        raise AssertionError("LabService was built although a lab_factory was supplied")

    monkeypatch.setattr("f1lab.api.app.LabService", boom)
    app = create_app(str(db), lab_factory=lambda: stub)
    yield app, stub, default_db
    engine.dispose()


def test_request_table_covers_every_restored_route(stub_app):
    app, _, _ = stub_app
    routes = restored_routes(app)
    assert len(routes) >= 20
    assert routes == set(REQUESTS), f"uncovered: {sorted(routes - set(REQUESTS))}; stale: {sorted(set(REQUESTS) - routes)}"


def test_every_restored_route_returns_2xx_within_2_seconds_and_uses_the_stub_lab(stub_app):
    app, stub, default_db = stub_app
    slow, bad = [], []
    with TestClient(app) as client:
        for (method, template), (url, body) in sorted(REQUESTS.items()):
            t0 = time.perf_counter()
            r = client.request(method, url, json=body)
            dt = time.perf_counter() - t0
            if not 200 <= r.status_code < 300:
                bad.append((method, template, r.status_code, r.text[:120]))
            if dt > 2.0:
                slow.append((method, template, round(dt, 2)))
    assert not bad, bad
    assert not slow, slow
    assert len(stub.calls) >= len(REQUESTS) - 5  # routes served by the stub, not by a real LabService
    assert not default_db.exists(), "the default database was created although a lab_factory was supplied"
    assert not default_db.parent.exists()


def test_unknown_demo_name_is_404_with_message(stub_app):
    app, _, _ = stub_app
    with TestClient(app) as client:
        r = client.get("/api/demo/nope.csv")
    assert r.status_code == 404
    assert "demo not found" in json.dumps(r.json()).lower()


# ----------------------------------------------------------------------------- /api/meta and /api/health (5.5, 5.6)
def test_meta_lists_historical_and_setup_modules_by_name(client):
    meta = client.get("/api/meta").json()
    mods = {m["id"]: m for m in meta["modules"]}
    assert set(mods) == {"historical", "setup"}
    assert mods["historical"]["label"] == "Historical F1"
    assert mods["setup"]["label"] == "Setup Engineering"
    # keys of both the setup and the historical halves, with their types
    expected = {"notice": str, "notices": dict, "modes": list, "version": str, "trialforge_version": str,
                "factors": dict, "default_factors": dict, "methods": list, "optimisers": list, "provider": str,
                "first_year": int, "modules": list, "telemetry_columns": list}
    for key, typ in expected.items():
        assert key in meta, key
        assert isinstance(meta[key], typ), (key, type(meta[key]))
    assert meta["provider"] == "openf1"
    assert set(meta) == set(expected) and [m["id"] for m in meta["modes"]] == ["synthetic"]


def test_health_has_sdk_fields_and_keeps_existing_fields_and_types(client):
    h = client.get("/api/health").json()
    existing = {"status": str, "app": str, "version": str, "data": str, "interrupted_imports": int}
    for key, typ in existing.items():
        assert key in h, key
        assert isinstance(h[key], typ), (key, type(h[key]))
    assert (h["status"], h["app"], h["version"], h["data"]) == ("ok", "f1-setup-lab", "2.1.0", "openf1")
    import trialforge

    for key in ("trialforge", "trialforge_path"):
        assert isinstance(h[key], str) and h[key], key
    assert h["trialforge"] == trialforge.__version__
    assert Path(h["trialforge_path"]).exists()


# ----------------------------------------------------------------------------- route list (6.5)
SETUP_TOKENS = ("experiment", "telemetry/preview", "demo", "optimise")
HISTORICAL_TOKENS = ("historical", "openf1", "import")


def test_no_route_converts_between_historical_and_setup_data(tmp_path):
    app = create_app(str(tmp_path / "trialforge.db"))
    paths = sorted({r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")})
    assert "/api/historical/sessions" in paths and "/api/experiments" in paths
    assert not [p for p in paths if p.startswith(("/api/live", "/api/game"))]  # the recorded-stint routes are gone
    hist = [p for p in paths if p.startswith("/api/historical")]
    other = [p for p in paths if not p.startswith("/api/historical")]
    for p in hist:  # historical routes never address experiments
        assert not any(t in p.removeprefix("/api/historical") for t in ("experiment", "optimise")), p
    for p in other:  # setup routes never address historical sessions or imports
        assert not any(t in p for t in HISTORICAL_TOKENS), p
    for p in paths:  # a conversion route would name both domains, or use a conversion verb
        assert not (any(t in p for t in HISTORICAL_TOKENS) and any(t in p for t in SETUP_TOKENS)), p
        assert not re.search(r"convert|promote|to-measurement|to-historical|from-historical|from-experiment|as-experiment", p), p
    # the historical routes keep their exact path prefix for every method, and no historical route takes an experiment id
    for r in app.routes:
        if getattr(r, "path", "").startswith("/api/historical"):
            assert "exp_id" not in r.path and "run_id" not in r.path
# ----------------------------------------------------------------------------- the recorded-stint feature is gone
def test_removed_routes_are_not_served_and_the_old_experiment_mode_is_rejected(client):
    for method, url in (("GET", "/api/live/status"), ("POST", "/api/live/connect"), ("GET", "/api/game/verification"),
                        ("POST", "/api/experiments/e1/runs/r1/stint/start"), ("GET", "/api/experiments/e1/runs/r1/evidence"),
                        ("GET", "/api/experiments/e1/telemetry/series-index")):
        assert client.request(method, url).status_code in (404, 405), (method, url)
    r = client.post("/api/experiments", json={"name": "x", "mode": "live"})
    assert r.status_code == 422 and any(i["field"] == "mode" for i in r.json()["detail"])
