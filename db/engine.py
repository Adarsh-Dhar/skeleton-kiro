"""Engine factory: WAL, busy_timeout, foreign keys, explicit transactions (so SQLite DDL is transactional)."""
from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from db.config import resolve_db_url

BUSY_TIMEOUT_MS = 30000
IMMEDIATE = "sqlite_immediate"


def _enable_wal(cur, attempts: int = 200) -> None:
    """Switch to WAL. The switch needs an exclusive lock and SQLite does not apply busy_timeout to it, so two processes
    opening a brand-new file at once can see 'database is locked'; retry until the other process has finished."""
    for i in range(attempts):
        try:
            mode = cur.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() == "wal":
                return
        except sqlite3.OperationalError as exc:
            if "lock" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
        time.sleep(0.05 + 0.01 * min(i, 20))
    raise sqlite3.OperationalError("could not enable WAL mode: database stayed locked")


def _is_memory(url: str) -> bool:
    return ":memory:" in url or url in ("sqlite://", "sqlite:///")


def create_db_engine(target: str | os.PathLike[str] | None = None) -> Engine:
    """Create an engine for the root database (or any SQLAlchemy URL / file path).

    The parent directory of a file database is created. SQLite connections use WAL, ``busy_timeout=30000`` and
    ``foreign_keys=ON``. Transactions are opened explicitly with ``BEGIN`` so DDL and DML roll back together.
    """
    url = resolve_db_url(target)
    kwargs: dict = {}
    sqlite = url.startswith("sqlite")
    if sqlite:
        if _is_memory(url):
            kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
        else:
            path = url.split("///", 1)[1]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            kwargs.update(connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000})
    engine = create_engine(url, **kwargs)
    if sqlite:
        memory = _is_memory(url)

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.isolation_level = None  # we emit BEGIN ourselves (see _on_begin)
            cur = dbapi_conn.cursor()
            cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cur.execute("PRAGMA foreign_keys=ON")
            if not memory:
                _enable_wal(cur)
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn: Connection) -> None:
            immediate = conn.get_execution_options().get(IMMEDIATE, False)
            conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


@contextmanager
def immediate_transaction(engine: Engine) -> Iterator[Connection]:
    """A write transaction that takes the SQLite write lock up front (``BEGIN IMMEDIATE``).

    Use for read-modify-write sequences shared between the two app processes; it waits (``busy_timeout``)
    instead of failing with a lock-upgrade error halfway through.
    """
    with engine.connect() as conn:
        conn = conn.execution_options(**{IMMEDIATE: True})
        with conn.begin():
            yield conn
