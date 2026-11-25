"""Layer separation smoke test (Requirements 16.3 to 16.5).

TRIALFORGE (the installed SDK) must not import WATTWISE and must carry no electricity vocabulary.
Complements ``test_persistence_and_sdk.py``, which only scans a skeleton source checkout (skipped when absent)
and does not look at imports.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import trialforge

SDK_DIR = Path(trialforge.__file__).resolve().parent
BACKEND = Path(__file__).resolve().parents[1] / "backend" / "wattwise"

# Words that must not appear anywhere in the SDK text (code, comments, strings).
BANNED_TEXT = re.compile(r"wattwise|household|tariff|appliance|charger|charge_start|departure_charge", re.I)
# Words that must not appear as a word inside any identifier. Text such as the SI unit table
# ("meter": "m", "kWh") is a unit-conversion string that predates this spec, so only identifiers are checked.
BANNED_IDENT_TOKENS = {"household", "tariff", "tariffs", "appliance", "appliances", "charger", "meter", "meters", "kwh", "wattwise"}


def _sdk_files() -> list[Path]:
    files = sorted(SDK_DIR.rglob("*.py"))
    assert files, "no trialforge sources found"
    return files


def _identifiers(tree: ast.AST):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield n.name
        elif isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr
        elif isinstance(n, ast.arg):
            yield n.arg
        elif isinstance(n, ast.keyword) and n.arg:
            yield n.arg
        elif isinstance(n, ast.alias):
            yield n.asname or n.name.split(".")[-1]


def _tokens(identifier: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", identifier)
    return {t.lower() for t in spaced.split("_") if t}


def test_trialforge_never_imports_wattwise():
    offenders = []
    for path in _sdk_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders += [f"{path.relative_to(SDK_DIR)}: {n}" for n in names if n.split(".")[0] == "wattwise"]
    assert offenders == []


def test_trialforge_has_no_electricity_terms():
    text_hits = [p.relative_to(SDK_DIR).as_posix() for p in _sdk_files() if BANNED_TEXT.search(p.read_text())]
    assert text_hits == [], f"electricity terms in trialforge text: {text_hits}"

    ident_hits = sorted(
        {f"{p.relative_to(SDK_DIR).as_posix()}:{i}" for p in _sdk_files() for i in _identifiers(ast.parse(p.read_text())) if _tokens(i) & BANNED_IDENT_TOKENS}
    )
    assert ident_hits == [], f"electricity words in trialforge identifiers: {ident_hits}"


def test_wattwise_does_not_define_or_shadow_trialforge_modules():
    """The WATTWISE package must not contain a trialforge copy (the SDK is used as installed)."""
    assert not any(p.name == "trialforge" for p in BACKEND.rglob("*") if p.is_dir())
    assert SDK_DIR != BACKEND and BACKEND not in SDK_DIR.parents


@pytest.mark.parametrize("name", ["trialforge"])
def test_sdk_import_does_not_pull_in_wattwise(name):
    import subprocess
    import sys

    code = f"import {name}, sys; sys.exit(1 if any(m == 'wattwise' or m.startswith('wattwise.') for m in sys.modules) else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
