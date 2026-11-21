"""Root database package.

One SQLite file (``<repo>/data/trialforge.db`` by default, ``TRIALFORGE_DB`` to override) holds everything both
applications store: the SDK experiment tables (created by the unmodified ``SQLiteExperimentRepository``), the
``f1_*`` tables (F1 SETUP LAB) and the ``ww_*`` tables (WATTWISE LAB). ``db.migrate`` owns the schema of the
``app_*``, ``f1_*`` and ``ww_*`` tables; the SDK owns its own tables.
"""
from db.config import DEFAULT_DB_ENV, default_db_path, resolve_db_url
from db.engine import create_db_engine, immediate_transaction
from db.migrations import MigrationError, current_version, latest_version, migrate

__all__ = [
    "DEFAULT_DB_ENV",
    "MigrationError",
    "create_db_engine",
    "current_version",
    "default_db_path",
    "immediate_transaction",
    "latest_version",
    "migrate",
    "resolve_db_url",
]
