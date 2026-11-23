"""``python -m db.migrate [--db PATH_OR_URL] [--status]``: create/upgrade the root database."""
from __future__ import annotations

import argparse
import sys

from db.backup import backup_hook
from db.config import resolve_db_url
from db.engine import create_db_engine
from db.migrations import current_version, latest_version, migrate
from db.migrations.runner import MigrationError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m db.migrate", description="Create or upgrade the TRIALFORGE root database.")
    p.add_argument("--db", default=None, help="file path or SQLAlchemy URL (default: $TRIALFORGE_DB or <repo>/data/trialforge.db)")
    p.add_argument("--status", action="store_true", help="only report the schema version; change nothing")
    a = p.parse_args(argv)
    url = resolve_db_url(a.db)
    engine = create_db_engine(url)
    print(f"database: {url}")
    if not a.status:
        try:
            applied = migrate(engine, before_apply=backup_hook)
        except MigrationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for m in applied:
            print(f"applied migration {m.version}: {m.name}")
        if not applied:
            print("no pending migrations")
    print(f"schema version: {current_version(engine)} (latest {latest_version()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
