"""Generate the schema golden files used by the f1-setup-lab restoration tests.

    python f1-setup-lab/scripts/gen_schema_goldens.py current  <repo_root> <out.json>
    python f1-setup-lab/scripts/gen_schema_goldens.py archive  <archive_root> <out.json>

``current``: migrate an empty database with the repository's own ``db`` package (latest version, 8 before
migration 9 exists) and dump every table.
``archive``: apply the archive's own migration 4 to an empty database and dump the six Restored_Tables.
Output: {table: {"table_info": [...], "index_list": [...], "foreign_key_list": [...]}} sorted by name.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

RESTORED = ["f1_udp_recording", "f1_udp_packet", "f1_stint_evidence", "f1_stint_telemetry", "f1_probe_report",
            "f1_setup_mapping_confirmation"]


def dump(db_file: Path, only: list[str] | None) -> dict:
    con = sqlite3.connect(db_file)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if only is not None:
            missing = [n for n in only if n not in names]
            if missing:
                raise SystemExit(f"missing tables: {missing}")
            names = sorted(only)
        out = {}
        for n in names:
            out[n] = {
                "table_info": [list(r) for r in con.execute(f'PRAGMA table_info("{n}")')],
                "index_list": sorted([list(r) for r in con.execute(f'PRAGMA index_list("{n}")')], key=lambda r: r[1]),
                "foreign_key_list": [list(r) for r in con.execute(f'PRAGMA foreign_key_list("{n}")')],
            }
        return out
    finally:
        con.close()


def main(mode: str, root: str, out: str) -> None:
    sys.path.insert(0, root)
    from db.engine import create_db_engine  # noqa: PLC0415
    from db.migrations.runner import migrate  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        db_file = Path(td) / "t.db"
        engine = create_db_engine(db_file)
        if mode == "current":
            migrate(engine)
            data = dump(db_file, None)
        else:
            migrate(engine, target=4)
            data = dump(db_file, RESTORED)
        engine.dispose()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"{mode}: {len(data)} tables -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
