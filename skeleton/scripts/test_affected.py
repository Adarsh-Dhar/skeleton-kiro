"""Run the tests most relevant to the changed files (git diff + untracked); full suite if unsure.

Usage: python scripts/test_affected.py [file ...]
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP = {
    "models": ["unit/test_models.py", "unit/test_statistics.py", "unit/test_designs.py"],
    "designer": ["unit/test_designs.py", "unit/test_statistics.py"],
    "measurements": ["unit/test_measurements_engine.py", "regression"],
    "analysis": ["unit/test_statistics.py", "regression"],
    "evaluation": ["unit/test_statistics.py", "unit/test_optimization.py"],
    "optimization": ["unit/test_optimization.py", "integration/test_workflows.py"],
    "execution": ["integration", "contracts"],
    "persistence": ["integration/test_persistence_recovery.py", "integration/test_execution_lifecycle.py"],
    "adapters": ["contracts", "integration/test_execution_lifecycle.py"],
    "reporting": ["integration/test_workflows.py"],
}
TESTS = ROOT / "packages" / "trialforge-core" / "tests"


def changed() -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    return [line[3:] for line in out.stdout.splitlines()] if out.returncode == 0 else []


def main() -> int:
    files = sys.argv[1:] or changed()
    targets: set[str] = set()
    for f in files:
        parts = Path(f).parts
        if "examples" in parts:
            targets |= {"contracts", "integration/test_workflows.py"}
        for layer, tests in MAP.items():
            if layer in parts:
                targets |= {t for t in tests}
    if not targets:
        cmd = [sys.executable, "-m", "pytest", "-q", "-x", str(ROOT)]
    else:
        cmd = [sys.executable, "-m", "pytest", "-q", "-x", str(ROOT / "tests"), *[str(TESTS / t) for t in sorted(targets)]]
    print("running:", " ".join(Path(c).name if c.startswith("/") else c for c in cmd))
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
