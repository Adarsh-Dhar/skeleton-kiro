"""Generate the Kiro agent and hook configuration for both workspace layouts from one source.

    python scripts/sync_kiro_config.py            # write the files (and remove migrated legacy *.kiro.hook files)
    python scripts/sync_kiro_config.py --check    # exit 1 if any generated file is missing or out of date

Sources (edit these, then run this script):
  skeleton/.kiro/ownership.json          which agent owns which SDK files, its duties and verification commands
  HOOKS below                            the five SDK hooks (migrated from the legacy skeleton/.kiro/hooks/*.kiro.hook files)

Generated, per layout:
  skeleton/.kiro/...   for a Kiro workspace opened at skeleton/      (paths relative to the SDK root)
  .kiro/...            for a Kiro workspace opened at the repo root (paths prefixed with skeleton/)

Kiro only reads `.kiro` at a workspace root, and the SDK's own tests require skeleton/.kiro to exist, so both are kept and this
script stops them from drifting (tests/test_kiro_config.py runs --check).

Formats (Kiro IDE 1.x, https://kiro.dev/docs/hooks.md and https://kiro.dev/docs/custom-agents/configuration-reference.md):
  hooks   .kiro/hooks/<id>.json  {"version": "v1", "hooks": [{name, description, enabled, trigger, matcher, action}]}
  agents  .kiro/agents/<name>.json  {name, description, prompt, tools, resources, permissions}
Custom agents do not receive workspace steering automatically; it is listed in `resources`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "skeleton"
OWNERSHIP = SDK / ".kiro" / "ownership.json"  # not under agents/: Kiro reads every agents/*.json as an agent definition
WRITE_TOOLS = ("fs_write", "fs_append", "str_replace", "delete_file")
WAVE_GATES = ["./scripts/lint.sh", "python -m pytest", "python scripts/check_imports.py"]

LAYOUTS = {
    "sdk": {"kiro": SDK / ".kiro", "prefix": "", "match": "(^|/)", "cd": "", "where": "the repository root (this workspace is the SDK)"},
    "repo": {"kiro": ROOT / ".kiro", "prefix": "skeleton/", "match": "(^|/)skeleton/", "cd": "cd skeleton && ",
             "where": "the `skeleton/` directory (the SDK inside this repository)"},
}

# {M} = path prefix regex for the layout, {CD} = command prefix. Triggers: legacy `fileEdited` -> PostFileSave (fires on agent edits),
# legacy `askAgent` -> action type `agent` (the prompt is given to the agent, which runs the command and fixes what it reports).
HOOKS = [
    {"id": "affected-tests", "name": "Run affected tests", "description": "Run the tests mapped to the edited core layer",
     "matcher": r"{M}(packages/trialforge-core/src|examples)/.*\.py$",
     "prompt": "An SDK source file changed. Run `{CD}python scripts/test_affected.py` and report the failures. Do not weaken a test to make "
               "it pass: fix the code, or explain why the test is wrong."},
    {"id": "architecture-boundaries", "name": "Check architecture boundaries",
     "description": "Layering, domain independence, public API stability, no dynamic code execution",
     "matcher": r"{M}(packages/trialforge-core/src|examples)/.*\.py$",
     "prompt": "An SDK source file changed. Run `{CD}python -m pytest tests/test_architecture.py -q` and fix any violation by changing the "
               "code, not the rules."},
    {"id": "integration-tests", "name": "Run integration and contract tests",
     "description": "Full lifecycle, persistence/recovery and adapter contract tests when execution, persistence or adapters change",
     "matcher": r"{M}(packages/trialforge-core/src/trialforge/(execution|persistence|adapters)/.*|examples/(.*/)?adapter\.py)$",
     "prompt": "Execution, persistence or adapter code changed. Run `{CD}python -m pytest packages/trialforge-core/tests/integration "
               "packages/trialforge-core/tests/contracts -q` and report the counts."},
    {"id": "lint-on-save", "name": "Lint and format check", "description": "Run ruff lint + format check when SDK Python sources change",
     "matcher": r"{M}(packages|examples|tests|scripts)/.*\.py$",
     "prompt": "An SDK Python file changed. Run `{CD}./scripts/lint.sh`. If it fails, fix the reported problems without changing behaviour "
               "and run it again."},
    {"id": "package-imports", "name": "Check package imports",
     "description": "Verify every public name and example module imports after changes to package __init__ files",
     "matcher": r"{M}packages/trialforge-core/src/trialforge/(.*/)?__init__\.py$",
     "prompt": "A package __init__ changed. Run `{CD}python scripts/check_imports.py` and `{CD}python scripts/gen_api_reference.py`; if the "
               "API reference changed, keep the regenerated file."},
]


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def hook_files(layout: dict) -> dict[Path, str]:
    out = {}
    for h in HOOKS:
        spec = {"name": h["name"], "description": h["description"], "enabled": True, "trigger": "PostFileSave",
                "matcher": h["matcher"].replace("{M}", layout["match"]),
                "action": {"type": "agent", "prompt": h["prompt"].replace("{CD}", layout["cd"])}}
        out[layout["kiro"] / "hooks" / f"{h['id']}.json"] = _dump({"version": "v1", "hooks": [spec]})
    return out


def agent_files(layout: dict, ownership: dict) -> dict[Path, str]:
    p, kiro_rel = layout["prefix"], f"{layout['prefix']}.kiro"
    out = {}
    agents = ownership["agents"]
    for name, a in agents.items():
        own = [p + g for g in a["owns"]]
        verify = "; ".join(f"`{c}`" for c in a["verify"] + WAVE_GATES)
        prompt = (
            f"You are {name} for the TRIALFORGE SDK. Run every command from {layout['where']}.\n"
            f"You may edit ONLY these paths (relative to the workspace root): {', '.join(own)}.\n"
            f"{a['duties'].replace('.kiro/', kiro_rel + '/') if p else a['duties']}\n"
            "Rules:\n"
            "- The SDK is frozen for the current release: change code only when the user explicitly asks; report a suspected defect "
            "instead of fixing it unasked.\n"
            "- Never edit a file you do not own. If your task needs a change elsewhere, stop and send a handoff request to the owner listed "
            f"in {kiro_rel}/agents/README.md: the file, the exact change, why it is needed, and what you will do once it lands. Shared types "
            "are changed only by contracts-owner; never redefine one locally.\n"
            "- The steering files in your resources apply to all of your work.\n"
            f"- Before reporting done, run {verify}, and report the real pass/fail/skip counts."
        )
        rules = []
        for cap in WRITE_TOOLS:
            rules.append({"capability": cap, "match": own, "effect": "allow"})
            rules.append({"capability": cap, "match": ["**"], "exclude": own, "effect": "deny"})
        spec = {"name": name, "description": a["description"], "prompt": prompt, "tools": ["read", "write", "shell"],
                "resources": [f"file://{kiro_rel}/steering/*.md", f"file://{kiro_rel}/agents/README.md"],
                "permissions": {"rules": rules}}
        out[layout["kiro"] / "agents" / f"{name}.json"] = _dump(spec)
    return out


ROOT_STEERING = """---
inclusion: fileMatch
fileMatchPattern: "skeleton/**"
---
# TRIALFORGE SDK steering (repository workspace)

Generated by scripts/sync_kiro_config.py. When you work on files under `skeleton/` (the TRIALFORGE SDK), the SDK's own steering applies.
The SDK is frozen for the current release: change it only when explicitly asked. Run SDK commands from the `skeleton/` directory.

#[[file:skeleton/.kiro/steering/architecture-boundaries.md]]
#[[file:skeleton/.kiro/steering/coding-conventions.md]]
#[[file:skeleton/.kiro/steering/domain-independence.md]]
#[[file:skeleton/.kiro/steering/public-api-stability.md]]
#[[file:skeleton/.kiro/steering/statistical-correctness.md]]
#[[file:skeleton/.kiro/steering/testing-requirements.md]]
"""


def generated() -> dict[Path, str]:
    ownership = json.loads(OWNERSHIP.read_text())
    files: dict[Path, str] = {}
    for layout in LAYOUTS.values():
        files |= hook_files(layout) | agent_files(layout, ownership)
    files[ROOT / ".kiro" / "steering" / "trialforge-sdk.md"] = ROOT_STEERING
    return files


def legacy_hooks() -> list[Path]:
    return sorted((SDK / ".kiro" / "hooks").glob("*.kiro.hook"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report out-of-date files and exit 1; write nothing")
    a = ap.parse_args(argv)
    files = generated()
    stale = [p for p, text in files.items() if not p.exists() or p.read_text() != text]
    legacy = legacy_hooks()
    if a.check:
        for p in stale:
            print(f"out of date: {p.relative_to(ROOT)}")
        for p in legacy:
            print(f"legacy hook not migrated: {p.relative_to(ROOT)}")
        if not stale and not legacy:
            print(f"ok: {len(files)} generated Kiro files are up to date")
        return 1 if stale or legacy else 0
    for p in stale:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(files[p])
        print(f"wrote {p.relative_to(ROOT)}")
    for p in legacy:
        p.unlink()
        print(f"removed legacy hook {p.relative_to(ROOT)} (migrated to {p.name.removesuffix('.kiro.hook')}.json)")
    if not stale and not legacy:
        print("nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
