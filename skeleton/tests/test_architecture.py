"""Architecture boundaries, public API stability and safety constraints (workspace level)."""

import ast
import re
from pathlib import Path

import pytest

import trialforge

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "packages" / "trialforge-core" / "src" / "trialforge"
EXAMPLES = ROOT / "examples"


def py_files(base):
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]


def imports(path):
    mods = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            mods += [(a.name, [], node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.append((node.module, [a.name for a in node.names], node.lineno))
    return mods


# 1 ----------------------------------------------------------------------------------- core is domain-independent
FORBIDDEN_WORDS = re.compile(
    r"\b(examples|f1_simulator|wattwise|coffee|CoffeeRoast|SyntheticF1|SyntheticWattwise)\b", re.IGNORECASE
)


def test_core_never_imports_or_mentions_application_modules():
    offenders = []
    for p in py_files(SRC):
        for mod, _, line in imports(p):
            if mod.split(".")[0] == "examples" or FORBIDDEN_WORDS.search(mod):
                offenders.append(f"{p.name}:{line} imports {mod}")
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.search(r"examples\.|f1_simulator|wattwise_meter|third_domain", node.value)
            ):
                offenders.append(f"{p.name}:{node.lineno} string references an application module")
    assert not offenders, offenders


# 2/3/4 ------------------------------------------------------------------------------- examples use the public SDK only
@pytest.mark.parametrize("example", ["f1_simulator_adapter", "wattwise_meter_adapter", "third_domain_adapter"])
def test_example_adapters_use_only_the_public_sdk(example):
    public = set(trialforge.__all__)
    for p in py_files(EXAMPLES / example):
        for mod, names, line in imports(p):
            top = mod.split(".")[0]
            assert top != "trialforge" or mod == "trialforge", (
                f"{p.name}:{line} deep import {mod}; use `from trialforge import ...`"
            )
            if mod == "trialforge":
                assert set(names) <= public, f"{p.name}:{line} uses non-public names {set(names) - public}"
            assert top != "examples" or mod.startswith(f"examples.{example}"), f"{p.name}:{line} imports another example ({mod})"


def test_examples_have_no_core_dependency_beyond_trialforge_and_stdlib():
    import sys

    stdlib = set(sys.stdlib_module_names)
    for p in py_files(EXAMPLES):
        for mod, _, line in imports(p):
            top = mod.split(".")[0]
            assert top in stdlib | {"trialforge", "examples", "__future__"}, f"{p}:{line} imports third-party {mod}"


# 5 ---------------------------------------------------------------------------------- extensibility without core edits
async def test_brand_new_domain_adapter_runs_without_touching_core_and_alongside_others():
    from examples.third_domain_adapter.adapter import CoffeeRoastAdapter

    from trialforge import (
        AdapterCapabilities,
        AdapterMetadata,
        AdapterRegistry,
        ExecutionResponse,
        ExperimentService,
        ExperimentSpec,
        Factor,
        Measurement,
        MeasurementDefinition,
        MeasurementResponse,
        Objective,
    )

    class GreenhouseAdapter:  # an unrelated fourth domain, defined entirely outside the package
        metadata = AdapterMetadata("greenhouse", "0.1", "tomato yield")
        capabilities = AdapterCapabilities(concurrent=True, max_concurrency=4)

        async def validate(self, config): ...
        async def prepare(self, context): ...
        async def execute(self, config):
            return ExecutionResponse(
                completed=True,
                measurements=[
                    Measurement(run_id="unused", metric="yield_kg", value=5 - (config["humidity"] - 70) ** 2 / 100, unit="kg")
                ],
            )

        async def execute_run(self, ctx):
            return await self.execute(ctx.configuration)

        async def collect_result(self, run_id):
            return MeasurementResponse(completed=False)

        async def cancel(self, run_id): ...

    reg = AdapterRegistry()
    reg.register(GreenhouseAdapter())
    reg.register(CoffeeRoastAdapter())
    s = ExperimentService(registry=reg)
    spec = ExperimentSpec(
        name="gh",
        adapter_name="greenhouse",
        factors=[Factor(name="humidity", factor_type="continuous", lower=40, upper=90)],
        objectives=[Objective(metric="yield_kg", direction="maximize")],
        measurement_definitions=[MeasurementDefinition(metric="yield_kg", unit="kg")],
        run_budget=12,
        design_method="random",
        design_options={"n_runs": 6},
        random_seed=1,
    )
    res = await s.run(spec)
    final, stop = await s.run_sequential(spec.experiment_id, iterations=2, batch_size=2, method="bayesian")
    assert res.status.value == "completed" and len(res.runs) == 6
    assert len(final.runs) == 6 + 2 * 2 and stop.reason == "max_iterations"  # sequential runs really were added
    an = s.analyse(final)
    best = next(r for r in final.runs if r.run_id == an.best_run_id)
    assert 55 < best.configuration["humidity"] < 85


# 6 ---------------------------------------------------------------------------------- public import stability
V01_API = [
    "AdapterCapabilities",
    "AdapterError",
    "AdapterMetadata",
    "AdapterRegistry",
    "AnalysisEngine",
    "AnalysisResult",
    "Constraint",
    "DesignError",
    "DesignPlan",
    "EffectEstimate",
    "ExecutionContext",
    "ExecutionResponse",
    "ExperimentAdapter",
    "ExperimentDesigner",
    "ExperimentReport",
    "ExperimentRepository",
    "ExperimentResult",
    "ExperimentService",
    "ExperimentSpec",
    "ExperimentStatus",
    "Factor",
    "Measurement",
    "MeasurementDefinition",
    "MeasurementResponse",
    "MeasurementTransformation",
    "Objective",
    "Optimizer",
    "ProcessedMeasurement",
    "Recommendation",
    "ReportEngine",
    "Run",
    "RunStatus",
    "SQLiteExperimentRepository",
    "MeasurementEngine",
]
DOCUMENTED = [
    "Analyzer",
    "Optimizer",
    "ExperimentDesigner",
    "ExperimentService",
    "ExperimentSpec",
    "PluginRegistry",
    "StoppingCriteria",
    "LocalBackgroundRunner",
]


def test_public_api_is_backward_compatible_and_complete():
    names = set(trialforge.__all__)
    assert set(V01_API) - {"MeasurementEngine"} <= names and "MeasurementEngine" in names
    for n in names | set(DOCUMENTED):
        assert hasattr(trialforge, n), n
    snapshot = (ROOT / "tests" / "public_api_snapshot.txt").read_text().split()
    assert not set(snapshot) - names, f"public names removed since snapshot: {set(snapshot) - names}"
    assert trialforge.API_VERSION == "1" and trialforge.__version__ == "0.2.2"


def test_legacy_deep_import_paths_still_resolve():
    from trialforge.adapters.contracts import AdapterError as A1
    from trialforge.designer.engine import DesignError as D1
    from trialforge.errors import AdapterError, DesignError
    from trialforge.models.core import Factor
    from trialforge.optimization.engine import Optimizer

    assert A1 is AdapterError and D1 is DesignError and Factor is trialforge.Factor and Optimizer is trialforge.Optimizer
    assert issubclass(DesignError, ValueError)  # 0.1 code catching ValueError keeps working


# 7 ---------------------------------------------------------------------------------- layering inside the core
LAYERS = {
    "errors": set(),
    "utilities": {"errors"},
    "definitions": {"errors"},
    "events": set(),
    "models": {"errors", "utilities"},
    "evaluation": {"errors", "models", "definitions", "measurements", "utilities"},
    "measurements": {"errors", "models", "definitions", "utilities"},
    "designer": {"errors", "models", "utilities", "evaluation"},
    "adapters": {"errors", "models"},
    "analysis": {"errors", "models", "measurements", "definitions", "utilities", "evaluation"},
    "optimization": {"errors", "models", "definitions", "utilities", "measurements", "evaluation"},
    "persistence": {"errors", "models"},
    "reporting": {"errors", "models", "definitions", "measurements", "persistence", "utilities", "evaluation"},
    "history": {"models", "utilities", "evaluation"},
    "execution": {
        "errors",
        "models",
        "adapters",
        "analysis",
        "definitions",
        "designer",
        "events",
        "measurements",
        "optimization",
        "persistence",
        "reporting",
        "utilities",
        "history",
        "evaluation",
    },
}


def test_core_layers_depend_only_downward():
    bad = []
    for p in py_files(SRC):
        rel = p.relative_to(SRC)
        if len(rel.parts) == 1:
            continue
        layer = rel.parts[0]
        for mod, _, line in imports(p):
            parts = mod.split(".")
            if parts[0] == "trialforge" and len(parts) > 1 and parts[1] != layer:
                if parts[1] not in LAYERS.get(layer, set()):
                    bad.append(f"{rel}:{line} {layer} -> {parts[1]}")
    assert not bad, bad


# 8 ---------------------------------------------------------------------------------- safety: no dynamic code execution
def test_core_has_no_dynamic_code_execution_or_unsafe_deserialisation():
    banned_calls = {"eval", "exec", "compile", "__import__"}
    banned_modules = {"pickle", "marshal", "subprocess", "shelve"}
    bad = []
    for p in py_files(SRC):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                if name in banned_calls or (name == "import_module"):
                    bad.append(f"{p.name}:{node.lineno} calls {name}")
        for mod, _, line in imports(p):
            if mod.split(".")[0] in banned_modules:
                bad.append(f"{p.name}:{line} imports {mod}")
    assert not bad, bad


# 9 ---------------------------------------------------------------------------------- public API documentation
def test_every_public_class_and_method_has_a_docstring():
    import inspect

    missing = []
    for name in trialforge.__all__:
        obj = getattr(trialforge, name)
        if inspect.isclass(obj) and obj.__module__.startswith("trialforge"):
            if not (obj.__doc__ or "").strip():
                missing.append(name)
            if issubclass(obj, Exception):
                continue
            for mname, m in vars(obj).items():
                if mname.startswith("_") or not callable(m) and not isinstance(m, (property, staticmethod, classmethod)):
                    continue
                target = m.fget if isinstance(m, property) else m.__func__ if isinstance(m, (staticmethod, classmethod)) else m
                if (
                    inspect.isfunction(target)
                    and not (target.__doc__ or "").strip()
                    and not issubclass(obj, __import__("pydantic").BaseModel)
                ):
                    missing.append(f"{name}.{mname}")
        elif inspect.isfunction(obj) and obj.__module__.startswith("trialforge") and not (obj.__doc__ or "").strip():
            missing.append(name)
    assert not missing, missing
