"""Static purity check for historical/analysis.py (Requirement 8.5)."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import f1lab
from f1lab.integrations.historical import analysis

SOURCE = Path(analysis.__file__)
THIRD_PARTY_ALLOWED = {"numpy", "pandas"}
ALLOWED_F1LAB = {"f1lab.errors"}
FORBIDDEN_PARTS = {"adapters", "services", "game_udp", "service", "repository", "fastapi", "starlette", "sqlalchemy"}


def _imported_modules() -> list[str]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):  # ast.walk includes imports nested in functions
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append("." * node.level + (node.module or ""))
            else:
                found.append(node.module or "")
    return found


def test_analysis_imports_only_allowed_modules():
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for mod in _imported_modules():
        if mod.startswith("."):
            bad.append(mod)
            continue
        top = mod.split(".")[0]
        if mod in ALLOWED_F1LAB or top in THIRD_PARTY_ALLOWED or top in stdlib:
            continue
        bad.append(mod)
    assert not bad, f"analysis.py imports disallowed modules: {bad}"


def test_analysis_does_not_import_forbidden_layers():
    bad = [m for m in _imported_modules() if FORBIDDEN_PARTS & set(m.lower().split("."))]
    assert not bad, f"analysis.py imports forbidden modules: {bad}"


def test_local_notice_matches_package_notice():
    assert analysis.HISTORICAL_NOTICE == f1lab.HISTORICAL_NOTICE
