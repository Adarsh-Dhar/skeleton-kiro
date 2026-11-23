"""Data-separation write guard (requirement 6.8).

Historical (OpenF1) data and setup (simulator/game measurement) data must never be mixed. ``install_guard``
attaches a ``before_cursor_execute`` listener to a SQLAlchemy engine. The listener finds the target table of
every INSERT, UPDATE, DELETE and REPLACE statement and raises :class:`TableNotPermitted` before the statement
reaches the database when the table is outside the allow-list of the engine's domain. SELECTs and other
statements are never restricted, so reads stay unrestricted. ``db.migrate`` runs on an unguarded engine
because migrations legitimately touch every table; do not install the guard on that engine.

SDK findings (``skeleton/packages/trialforge-core/src/trialforge/persistence/repository.py``):

* ``SQLiteExperimentRepository`` creates its own SQLAlchemy engine with ``create_engine`` and exposes it as
  ``repository.engine``. It does not use ``sqlite3`` directly, so the guard works when it is installed on
  that engine (``install_guard(repo.engine, "setup")``). Nothing else in the SDK package opens a connection.
* The SDK tables are: schema_info, experiments, plans, runs, measurements, processed_measurements, analyses,
  recommendations, reports, adapters, jobs, state_log. ``SETUP_SDK_TABLES`` lists them.
* Legacy-database migration in the SDK runs ``ALTER TABLE experiments RENAME TO experiments_legacy_v1``
  and then ``CREATE TABLE``. Those are DDL and are not checked by the guard (only INSERT, UPDATE, DELETE and
  REPLACE are checked).

Known bypasses (the guard cannot see these; the snapshot property tests cover them):

* Writes through a connection that is not on a guarded engine (another engine, or raw ``sqlite3``).
* DDL (CREATE, ALTER, DROP). Only data writes are checked.
* Statements whose target cannot be parsed (for example multi-statement scripts via ``executescript``,
  which bypass SQLAlchemy's cursor events). The guard allows what it cannot parse rather than blocking
  unknown statement shapes; SQLAlchemy itself never emits such statements.
* A write that reaches a table through a trigger or foreign-key cascade is not seen, because the guard
  looks only at the statement text sent by the application.
"""
from __future__ import annotations

import re
from typing import Literal

from sqlalchemy import event
from sqlalchemy.engine import Engine

Domain = Literal["historical", "setup"]

HISTORICAL_TABLES: frozenset[str] = frozenset(
    {
        "f1_hist_session",
        "f1_hist_frame",
        "f1_hist_dataset",
        "f1_hist_lap",
        "f1_hist_weather",
        "f1_hist_telemetry",
        "f1_hist_position",
        "f1_import_job",
    }
)

# Tables created by trialforge.persistence.repository (listed from the SDK source).
SETUP_SDK_TABLES: frozenset[str] = frozenset(
    {
        "schema_info",
        "experiments",
        "plans",
        "runs",
        "measurements",
        "processed_measurements",
        "analyses",
        "recommendations",
        "reports",
        "adapters",
        "jobs",
        "state_log",
    }
)

RESTORED_TABLES: frozenset[str] = frozenset(
    {
        "f1_udp_recording",
        "f1_udp_packet",
        "f1_stint_evidence",
        "f1_stint_telemetry",
        "f1_probe_report",
        "f1_setup_mapping_confirmation",
    }
)

PERMITTED: dict[str, frozenset[str]] = {
    "historical": HISTORICAL_TABLES,
    "setup": SETUP_SDK_TABLES | RESTORED_TABLES | {"app_experiment_owner"},
}


class TableNotPermitted(Exception):
    """A write targeted a table outside the set permitted for the engine's domain."""


_NAME = r"""(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)"""
_TARGET = rf"(?:{_NAME}\s*\.\s*)?({_NAME})"
_WRITE_RE = re.compile(
    rf"""^\s*(?:
        (?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+{_TARGET}
      | UPDATE(?:\s+OR\s+\w+)?\s+{_TARGET}
      | DELETE\s+FROM\s+{_TARGET}
    )""",
    re.IGNORECASE | re.VERBOSE,
)
# A leading WITH (CTE) hides the verb; search for the first write verb after it.
_CTE_WRITE_RE = re.compile(
    rf"""\b(?:
        (?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+{_TARGET}
      | UPDATE(?:\s+OR\s+\w+)?\s+{_TARGET}
      | DELETE\s+FROM\s+{_TARGET}
    )""",
    re.IGNORECASE | re.VERBOSE,
)
# Matches quoted text (kept as is) or a comment (replaced by a space). Quoted alternatives come first so
# comment markers inside string literals or quoted identifiers are not treated as comments.
_STRIP_RE = re.compile(
    r"""(?P<keep>'(?:[^']|'')*'|"[^"]*"|`[^`]*`|\[[^\]]*\])|--[^\n]*|/\*.*?(?:\*/|\Z)""",
    re.DOTALL,
)


def _strip_comments(statement: str) -> str:
    """Remove ``--`` and ``/* */`` comments, leaving string literals and quoted identifiers intact."""
    return _STRIP_RE.sub(lambda m: m.group("keep") or " ", statement).lstrip()


def _unquote(name: str) -> str:
    if len(name) >= 2 and name[0] in '"`[':
        name = name[1:-1]
    return name


def target_table(statement: str) -> str | None:
    """Return the table written by an INSERT/UPDATE/DELETE/REPLACE statement, or ``None`` for anything else."""
    sql = _strip_comments(statement)
    if sql[:4].upper() == "WITH":
        match = _CTE_WRITE_RE.search(sql)
    else:
        match = _WRITE_RE.match(sql)
    if match is None:
        return None
    name = next(g for g in match.groups() if g)
    return _unquote(name)


def check_statement(statement: str, domain: str) -> None:
    """Raise :class:`TableNotPermitted` if ``statement`` writes a table outside ``domain``'s permitted set."""
    table = target_table(statement)
    if table is not None and table.lower() not in PERMITTED[domain]:
        raise TableNotPermitted(f"table '{table}' is not permitted for {domain} writes")


def install_guard(engine: Engine, domain: Domain) -> None:
    """Reject writes to tables outside ``PERMITTED[domain]`` on ``engine``. Reads are not restricted."""
    if domain not in PERMITTED:
        raise ValueError(f"unknown domain {domain!r}; expected one of {sorted(PERMITTED)}")

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        check_statement(statement, domain)
