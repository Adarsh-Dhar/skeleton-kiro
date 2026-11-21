"""Migration runner.

Each migration runs in its own ``BEGIN IMMEDIATE`` transaction together with its ``app_schema_version`` row.
SQLite DDL is transactional, so a failing migration leaves no partial schema. ``BEGIN IMMEDIATE`` serialises two
processes starting at once; the loser re-reads the version inside the lock and skips what the winner applied.

The SDK's own tables (experiments, runs, ...) are not managed here; ``SQLiteExperimentRepository`` creates them.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from db.engine import immediate_transaction

VERSION_TABLE = "app_schema_version"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str


def _migrations():
    from db.migrations.versions import MIGRATIONS

    return MIGRATIONS


def latest_version(migrations: Sequence | None = None) -> int:
    ms = _migrations() if migrations is None else migrations
    return max((m.version for m in ms), default=0)


def _has_version_table(conn: Connection) -> bool:
    return conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": VERSION_TABLE}).first() is not None


def _version_in(conn: Connection) -> int:
    if not _has_version_table(conn):
        return 0
    return int(conn.execute(text(f"SELECT COALESCE(MAX(version), 0) FROM {VERSION_TABLE}")).scalar_one())


def current_version(engine: Engine) -> int:
    """Highest applied migration version (0 for a database that was never migrated)."""
    with engine.connect() as conn:
        with conn.begin():
            return _version_in(conn)


def migrate(engine: Engine, target: int | None = None, migrations: Sequence | None = None) -> list[AppliedMigration]:
    """Apply pending migrations up to ``target`` (default: latest). Returns what was applied by this call."""
    ms = sorted(_migrations() if migrations is None else migrations, key=lambda m: m.version)
    versions = [m.version for m in ms]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration versions")
    goal = latest_version(ms) if target is None else target
    applied: list[AppliedMigration] = []
    for m in ms:
        if m.version > goal:
            break
        try:
            with immediate_transaction(engine) as conn:
                conn.exec_driver_sql(
                    f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (version INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, "
                    "applied_at VARCHAR(40) NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                if m.version <= _version_in(conn):
                    continue  # already applied (possibly by another process while we waited for the lock)
                m.apply(conn)
                conn.execute(text(f"INSERT INTO {VERSION_TABLE} (version, name) VALUES (:v, :n)"), {"v": m.version, "n": m.name})
                applied.append(AppliedMigration(m.version, m.name))
        except Exception as exc:
            raise MigrationError(f"migration {m.version} ({m.name}) failed and was rolled back: {exc}") from exc
    return applied
