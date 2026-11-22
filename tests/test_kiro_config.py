"""Kiro workspace configuration: migrated hooks, agent definitions with steering, and conflict-free file ownership.

These check the configuration files against the documented Kiro IDE 1.x formats and against the real file tree. Whether the
Kiro IDE fires a hook or enforces a permission rule can only be observed inside the IDE (see skeleton/.kiro/agents/README.md).
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "skeleton"
OWNERSHIP = json.loads((SDK / ".kiro" / "ownership.json").read_text())
AGENTS = OWNERSHIP["agents"]
LAYOUTS = {"sdk": (SDK, ""), "repo": (ROOT, "skeleton/")}
TRIGGERS = {"SessionStart", "Stop", "UserPromptSubmit", "PreTaskExec", "PostTaskExec", "PreToolUse", "PostToolUse", "PostFileCreate",
            "PostFileSave", "PostFileDelete", "PreCompact", "PostCompact"}
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "dist", "build", ".run", ".kiro", "node_modules"}

# the legacy *.kiro.hook `fileEdited` patterns (glob, relative to the SDK root) that the migrated matchers must reproduce
LEGACY = {
    "affected-tests": ["packages/trialforge-core/src/**/*.py", "examples/**/*.py"],
    "architecture-boundaries": ["packages/trialforge-core/src/**/*.py", "examples/**/*.py"],
    "integration-tests": ["packages/trialforge-core/src/trialforge/execution/**", "packages/trialforge-core/src/trialforge/persistence/**",
                          "packages/trialforge-core/src/trialforge/adapters/**", "examples/**/adapter.py"],
    "lint-on-save": ["packages/**/*.py", "examples/**/*.py", "tests/**/*.py", "scripts/**/*.py"],
    "package-imports": ["packages/trialforge-core/src/trialforge/__init__.py", "packages/trialforge-core/src/trialforge/**/__init__.py"],
}


def glob_re(pattern: str) -> re.Pattern:
    """Glob with ** (any depth, including none) and * (one path segment)."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + "$")


def files_under(base: Path) -> list[str]:
    out = []
    for p in base.rglob("*"):
        rel = p.relative_to(base)
        if p.is_file() and not (set(rel.parts) & SKIP_DIRS) and not any(x.endswith(".egg-info") for x in rel.parts) and p.name != ".DS_Store":
            out.append(rel.as_posix())
    return sorted(out)


SDK_FILES = files_under(SDK)
REPO_FILES = files_under(ROOT)


def test_generated_files_are_in_sync_and_legacy_hooks_are_gone():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_kiro_config.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not list(SDK.rglob("*.kiro.hook")) and not list(ROOT.joinpath(".kiro").rglob("*.kiro.hook"))


# ------------------------------------------------------------------------------------------- hooks
@pytest.mark.parametrize("layout", LAYOUTS)
def test_hook_files_use_the_current_format(layout):
    base, _ = LAYOUTS[layout]
    files = sorted((base / ".kiro" / "hooks").glob("*.json"))
    assert {f.stem for f in files} == set(LEGACY)
    for f in files:
        doc = json.loads(f.read_text())
        assert doc["version"] == "v1" and len(doc["hooks"]) == 1
        h = doc["hooks"][0]
        assert set(h) <= {"name", "description", "enabled", "trigger", "matcher", "action", "timeout"}
        assert h["trigger"] in TRIGGERS and h["trigger"] == "PostFileSave"
        assert h["action"]["type"] == "agent" and h["action"]["prompt"].strip() and h["enabled"] is True
        re.compile(h["matcher"])


@pytest.mark.parametrize("hook", LEGACY)
def test_hook_matchers_reproduce_the_legacy_patterns_on_the_real_tree(hook):
    expected = {f for f in SDK_FILES if any(glob_re(g).match(f) for g in LEGACY[hook])}
    assert expected, f"{hook}: legacy patterns match no file"
    for layout, (base, prefix) in LAYOUTS.items():
        m = re.compile(json.loads((base / ".kiro" / "hooks" / f"{hook}.json").read_text())["hooks"][0]["matcher"])
        universe = SDK_FILES if layout == "sdk" else REPO_FILES
        # Kiro does not document whether the path is workspace-relative or absolute: the matcher must work for both
        for form in ("relative", "absolute"):
            path = (lambda f: f) if form == "relative" else (lambda f: str(base / f))
            got = {f for f in universe if m.search(path(f))}
            assert got == {prefix + f for f in expected}, (hook, layout, form, sorted(got ^ {prefix + f for f in expected})[:10])


def test_repo_hooks_never_fire_for_the_applications():
    app_files = [f for f in REPO_FILES if not f.startswith("skeleton/")]
    assert any(f.startswith(("tests/", "scripts/", "f1-setup-lab/", "wattwise-lab/")) for f in app_files)
    for h in LEGACY:
        m = re.compile(json.loads((ROOT / ".kiro" / "hooks" / f"{h}.json").read_text())["hooks"][0]["matcher"])
        assert not [f for f in app_files if m.search(f) or m.search(str(ROOT / f))], h


def test_hook_prompts_run_commands_where_they_exist():
    for layout, (base, prefix) in LAYOUTS.items():
        for f in (base / ".kiro" / "hooks").glob("*.json"):
            prompt = json.loads(f.read_text())["hooks"][0]["action"]["prompt"]
            for script in re.findall(r"scripts/[\w./-]+\.(?:py|sh)", prompt):
                assert (SDK / script).exists(), (f, script)
            assert ("cd skeleton && " in prompt) == (layout == "repo"), f


# ------------------------------------------------------------------------------------------- agents
@pytest.mark.parametrize("layout", LAYOUTS)
def test_agent_definitions_load_steering_and_follow_the_schema(layout):
    base, prefix = LAYOUTS[layout]
    files = {f.stem: json.loads(f.read_text()) for f in (base / ".kiro" / "agents").glob("*.json")}
    assert set(files) == set(AGENTS)  # all six roles, and nothing else is parsed as an agent
    steering = sorted((SDK / ".kiro" / "steering").glob("*.md"))
    assert len(steering) == 6
    for name, a in files.items():
        assert set(a) <= {"name", "description", "prompt", "tools", "resources", "permissions", "model", "hooks", "mcpServers"}
        assert a["name"] == name and a["tools"] == ["read", "write", "shell"]
        loaded: set[Path] = set()
        for res in a["resources"]:
            assert res.startswith("file://")
            hits = [p for p in base.glob(res.removeprefix("file://"))]
            assert hits, (name, res)
            loaded |= {p.resolve() for p in hits}
        assert {p.resolve() for p in steering} <= loaded, f"{name} does not load every steering file"
        assert len(loaded) <= 32 and sum(len(p.read_text()) for p in loaded) <= 100_000  # Kiro's per-agent resource limits
        assert "handoff" in a["prompt"] and "contracts-owner" in a["prompt"]
        for cmd in AGENTS[name]["verify"]:
            assert cmd in a["prompt"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_agent_write_permissions_match_ownership(layout):
    base, prefix = LAYOUTS[layout]
    for name, spec in AGENTS.items():
        rules = json.loads((base / ".kiro" / "agents" / f"{name}.json").read_text())["permissions"]["rules"]
        own = [prefix + g for g in spec["owns"]]
        for cap in ("fs_write", "fs_append", "str_replace", "delete_file"):
            allow = [r for r in rules if r["capability"] == cap and r["effect"] == "allow"]
            deny = [r for r in rules if r["capability"] == cap and r["effect"] == "deny"]
            assert len(allow) == 1 and allow[0]["match"] == own, (name, cap)
            assert len(deny) == 1 and deny[0]["match"] == ["**"] and deny[0]["exclude"] == own, (name, cap)


def test_every_sdk_file_has_exactly_one_owner():
    owners: dict[str, list[str]] = {f: [] for f in SDK_FILES}
    for name, spec in AGENTS.items():
        pats = [glob_re(g) for g in spec["owns"]]
        for f in SDK_FILES:
            if any(p.match(f) for p in pats):
                owners[f].append(name)
    unowned = [f for f, o in owners.items() if not o]
    shared = {f: o for f, o in owners.items() if len(o) > 1}
    assert not unowned, f"files without an owner: {unowned[:20]}"
    assert not shared, f"files with more than one owner: {dict(list(shared.items())[:20])}"


def test_every_ownership_glob_matches_something():
    for name, spec in AGENTS.items():
        for g in spec["owns"]:
            assert any(glob_re(g).match(f) for f in SDK_FILES), (name, g)


def test_readme_documents_every_agent():
    text = (SDK / ".kiro" / "agents" / "README.md").read_text()
    for name in AGENTS:
        assert f"`{name}`" in text


def test_root_steering_pulls_in_the_sdk_steering_only_for_sdk_files():
    text = (ROOT / ".kiro" / "steering" / "trialforge-sdk.md").read_text()
    assert text.startswith("---\ninclusion: fileMatch\nfileMatchPattern: \"skeleton/**\"\n---")
    refs = re.findall(r"#\[\[file:([^\]]+)\]\]", text)
    assert sorted(refs) == sorted(f"skeleton/.kiro/steering/{p.name}" for p in (SDK / ".kiro" / "steering").glob("*.md"))
    assert all((ROOT / r).exists() for r in refs)
    assert fnmatch.fnmatch("skeleton/packages/x.py", "skeleton/**") and not fnmatch.fnmatch("f1-setup-lab/x.py", "skeleton/**")
