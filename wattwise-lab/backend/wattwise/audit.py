"""``python -m wattwise.audit [--db PATH] [--json]``: audit the stored WATTWISE inputs against the data policy (read-only).

Exit code 0 when nothing is blocking, 1 otherwise. Default database: ``$TRIALFORGE_DB`` or ``<repo>/data/trialforge.db``.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wattwise.audit", description=__doc__.splitlines()[0])
    p.add_argument("--db", default=None, help="database file or URL (default: the root database)")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")
    a = p.parse_args(argv)
    from wattwise.services.audit import run
    from wattwise.services.lab import LabService
    report = run(LabService(a.db))
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        c = report["counts"]
        print(f"ready for projections: {'yes' if report['ready'] else 'no'}")
        print(f"blocking {c['blocking']} · to confirm {c['confirm']} · warnings {c['warning']} · clean inputs {c['clean_inputs']}")
        for f in report["findings"]:
            print(f"  [{f['severity']:8}] {f['input']}: {f['message']} -> {f['action']}")
        print(f"sign-off: {'PASSED' if report['signoff']['passed'] else 'NOT PASSED'}")
        for crit in report["signoff"]["criteria"]:
            print(f"  [{'x' if crit['passed'] else ' '}] {crit['criterion']}")
        for line in report["limits"]:
            print(f"  note: {line}")
    return 1 if report["counts"]["blocking"] else 0
if __name__ == "__main__":
    sys.exit(main())
