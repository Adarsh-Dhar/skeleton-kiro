"""Documentation must agree with the implementation: links resolve, snippets run, examples run, API reference is current."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

import trialforge

ROOT = Path(__file__).resolve().parents[1]
MD_FILES = [
    ROOT / "README.md",
    ROOT / "packages" / "trialforge-core" / "README.md",
    *sorted((ROOT / "docs").rglob("*.md")),
    *sorted((ROOT / ".kiro").rglob("*.md")),
]
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
BLOCK = re.compile(r"```python\n(.*?)```", re.S)


@pytest.mark.parametrize("md", MD_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(md):
    broken = []
    for target in LINK.findall(md.read_text()):
        if re.match(r"^(https?:|mailto:|#)", target):
            continue
        path = target.split("#")[0]
        if path and not (md.parent / path).exists():
            broken.append(target)
    assert not broken, f"{md.relative_to(ROOT)} has broken links: {broken}"


def docs_test_blocks():
    out = []
    for md in MD_FILES:
        for i, code in enumerate(BLOCK.findall(md.read_text())):
            if code.startswith("# docs-test"):
                out.append(pytest.param(code, id=f"{md.relative_to(ROOT)}#{i}"))
    return out


def test_there_are_runnable_documentation_snippets():
    assert len(docs_test_blocks()) >= 6


@pytest.mark.parametrize("code", docs_test_blocks())
def test_documented_snippets_run(code):
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert r.returncode == 0, r.stderr[-1500:]


def test_every_name_imported_from_trialforge_in_docs_exists():
    missing = []
    for md in MD_FILES:
        for code in BLOCK.findall(md.read_text()):
            for m in re.finditer(r"from trialforge import \(?([^)]*?)\)?\n(?!\s+[A-Za-z_]+,)", code, re.S):
                names = [n.strip() for n in re.split(r"[,\n]", m.group(1)) if n.strip() and not n.strip().startswith("#")]
                missing += [f"{md.name}: {n}" for n in names if not hasattr(trialforge, n.split(" as ")[0].split("#")[0].strip())]
    assert not missing, missing


EXAMPLES = {
    "examples.quickstart": ["status=completed", "kind=model_prediction", "report v2.0"],
    "examples.third_domain_adapter.run_example": ["best feasible setup", "largest marginal effect"],
    "examples.f1_simulator_adapter.run_example": ["resolution 4", "synthetic=False", "all simulated values are synthetic"],
    "examples.wattwise_meter_adapter.run_example": ["cheapest REJECTED schedule", "reading_kind=imported"],
    "examples.custom_adapter_tutorial": ["waiting_for_measurements", "after collect: completed"],
}


@pytest.mark.parametrize("module", EXAMPLES)
def test_advertised_examples_run_and_show_what_they_claim(module):
    r = subprocess.run([sys.executable, "-m", module], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert r.returncode == 0, r.stderr[-1500:]
    for needle in EXAMPLES[module]:
        assert needle in r.stdout, f"{needle!r} not in output of {module}:\n{r.stdout}"


def test_example_outputs_are_reproducible_across_processes():
    runs = [
        subprocess.run([sys.executable, "-m", "examples.quickstart"], capture_output=True, text=True, cwd=ROOT).stdout
        for _ in range(2)
    ]
    assert runs[0] == runs[1] and runs[0]


def test_generated_api_reference_is_current():
    r = subprocess.run([sys.executable, "scripts/gen_api_reference.py", "--check"], cwd=ROOT)
    assert r.returncode == 0, "run `python scripts/gen_api_reference.py` and commit docs/api_reference/index.md"


def test_documented_paths_exist():
    for rel in [
        "packages/trialforge-core/pyproject.toml",
        "examples/quickstart.py",
        "scripts/check_imports.py",
        "scripts/lint.sh",
        "scripts/test_affected.py",
        "docs/api_reference/index.md",
        "tests/test_architecture.py",
        ".kiro/specs/completion",
        ".kiro/steering",
        ".kiro/hooks",
        ".kiro/agents",
    ]:
        assert (ROOT / rel).exists(), rel
