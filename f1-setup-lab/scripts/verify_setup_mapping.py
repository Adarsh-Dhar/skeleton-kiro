"""Record (or review) manual confirmations of the four setup-factor mappings, per game variant.

    python scripts/verify_setup_mapping.py status
    python scripts/verify_setup_mapping.py reports
    python scripts/verify_setup_mapping.py show <report_id>
    python scripts/verify_setup_mapping.py confirm --factor front_wing --variant f1-25 --report <report_id> --by "name" [--reject] [--note ...]

A factor becomes 'verified' ONLY through ``confirm`` (without --reject), which you run after comparing the probe's
values with the numbers displayed in the game garage. Variants: f1-25 (EA SPORTS F1 25), f1-25-2026 (2026 Season Pack).
The catalog file (backend/f1lab/catalogs/ea_f1_25.py) is never edited by this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db import create_db_engine, migrate, resolve_db_url  # noqa: E402
from f1lab.catalogs.ea_f1_25 import GAME_FACTORS  # noqa: E402
from f1lab.catalogs.verification import VARIANTS, confirm, get_probe_report, list_probe_reports, status  # noqa: E402
from f1lab.integrations.game_udp.probe import VARIANT_LABELS  # noqa: E402


def print_status(engine) -> None:
    st = status(engine)
    for v in VARIANTS:
        print(f"\n{VARIANT_LABELS[v]} ({v})")
        for f, s in st[v].items():
            print(f"  {f:22s} {'VERIFIED' if s['verified'] else 'not verified':14s} {s['source']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("reports")
    sh = sub.add_parser("show")
    sh.add_argument("report_id")
    c = sub.add_parser("confirm")
    c.add_argument("--factor", required=True, choices=sorted(GAME_FACTORS))
    c.add_argument("--variant", required=True, choices=list(VARIANTS))
    c.add_argument("--report", required=True, help="probe report id that holds the evidence")
    c.add_argument("--by", required=True, help="who compared the packet values with the game screen")
    c.add_argument("--reject", action="store_true", help="record that the mapping did NOT match")
    c.add_argument("--note", default=None)
    a = ap.parse_args(argv)
    engine = create_db_engine(resolve_db_url(a.db))
    migrate(engine)
    if a.cmd == "status":
        print_status(engine)
    elif a.cmd == "reports":
        for r in list_probe_reports(engine):
            print(f"{r['report_id']}  {r['created_at'][:19]}  {r['game_variant'] or '?'}")
    elif a.cmd == "show":
        rep = get_probe_report(engine, a.report_id)
        if rep is None:
            print(f"no report {a.report_id}", file=sys.stderr)
            return 1
        print(json.dumps(rep, indent=2))
    else:
        rep = get_probe_report(engine, a.report)
        finding = (rep or {}).get("factors", {}).get(a.factor, {})
        if rep and not a.reject and finding.get("status") not in ("matches_expected_field", "isolated_change_observed"):
            print(f"warning: the probe status for {a.factor} is '{finding.get('status')}'. Only confirm if the game screen agrees.", file=sys.stderr)
        try:
            s = confirm(engine, a.factor, a.variant, verified=not a.reject, confirmed_by=a.by, probe_report_id=a.report, note=a.note,
                        low_value_seen=finding.get("low_seen"), high_value_seen=finding.get("high_seen"))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{a.factor} / {a.variant}: {'VERIFIED' if s['verified'] else 'REJECTED'} ({s['source']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
