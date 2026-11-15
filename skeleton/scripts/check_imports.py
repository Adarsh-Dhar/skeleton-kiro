"""Verify that the package and every example import cleanly using only the public SDK surface."""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import trialforge  # noqa: E402

for name in trialforge.__all__:
    getattr(trialforge, name)
mods = [".".join(p.relative_to(ROOT).with_suffix("").parts) for p in (ROOT / "examples").rglob("*.py")]
for m in sorted(mods):
    importlib.import_module(m.replace(".__init__", ""))
print(f"ok: {len(trialforge.__all__)} public names, {len(mods)} example modules import cleanly")
