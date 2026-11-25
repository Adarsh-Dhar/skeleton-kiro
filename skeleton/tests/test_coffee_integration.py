"""The coffee example runs end to end through the installed public SDK, selected by ExperimentSpec.adapter_name, with no
domain-specific code in the SDK."""
import asyncio
import importlib.metadata
import re
from pathlib import Path

import pytest
from examples.third_domain_adapter.adapter import CoffeeRoastAdapter
from examples.third_domain_adapter.run_example import build_spec

import trialforge
from trialforge import AdapterRegistry, ExperimentService
from trialforge.adapters.contracts import AdapterUnavailableError

DOMAIN_WORDS = re.compile(r"coffee|roast|f1[_ ]setup|f1lab|wattwise", re.IGNORECASE)


def _service():
    registry = AdapterRegistry()
    registry.register(CoffeeRoastAdapter())
    return ExperimentService(registry=registry)


def test_full_lifecycle_selects_the_registered_adapter_by_spec_adapter_name():
    service = _service()
    spec = build_spec()
    assert spec.adapter_name == "coffee-roast"

    async def go():
        plan = service.design(spec)
        return await service.execute(plan, spec)

    result = asyncio.run(go())
    assert result.runs and {r.adapter_name for r in result.runs} == {"coffee-roast"}
    analysis = service.analyse(result)
    assert analysis.best_run_id in {r.run_id for r in result.runs}
    recs = service.optimise(spec, result, count=1)
    assert len(recs) == 1 and set(recs[0].configuration) == {"temperature", "duration", "airflow"}
    assert service.report(result, analysis) is not None


def test_unknown_adapter_name_is_rejected():
    registry = AdapterRegistry()
    registry.register(CoffeeRoastAdapter())
    with pytest.raises(AdapterUnavailableError):
        registry.select(build_spec().model_copy(update={"adapter_name": "no-such-adapter"}))


def test_adapter_declares_itself_synthetic_and_measurements_are_flagged():
    assert CoffeeRoastAdapter.metadata.synthetic is True
    out = CoffeeRoastAdapter()._measure("r1", {"temperature": 205, "duration": 10, "airflow": 40}, None)
    assert out and all(m.metadata.get("synthetic") is True for m in out)


def test_installed_sdk_has_no_domain_specific_code():
    dist = importlib.metadata.distribution("trialforge-core")
    package_dir = Path(trialforge.__file__).parent
    assert "site-packages" in str(package_dir) or "packages/trialforge-core" in str(package_dir)
    offenders = [str(p) for p in package_dir.rglob("*.py") if DOMAIN_WORDS.search(p.read_text(encoding="utf-8"))]
    assert not offenders, f"{dist.version}: domain words in the SDK: {offenders}"


def test_example_imports_only_the_public_sdk():
    src = (Path(__file__).resolve().parents[1] / "examples" / "third_domain_adapter").glob("*.py")
    for p in src:
        for line in p.read_text().splitlines():
            m = re.match(r"\s*(?:from|import)\s+trialforge\.(\S+)", line)
            assert not m, f"{p.name} imports SDK internals: {line}"
