"""The ordered migration list. Append only; never edit or reorder a released migration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection

from db.base import Base
from db.migrations.game_data import GAME_TABLES, SDK_EXPERIMENT_TABLES, create_game_tables, remove_game_data


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Connection], None]


def create_tables(conn: Connection, *names: str) -> None:
    """Create the named model tables (and their indexes) if missing."""
    import db.models  # noqa: F401  (registers every table on Base.metadata)

    for name in names:
        try:
            Base.metadata.tables[name].create(conn, checkfirst=True)
        except Exception as exc:
            raise RuntimeError(f"creating table {name} failed: {exc}") from exc


def _m001_owner(conn: Connection) -> None:
    create_tables(conn, "app_experiment_owner")


def _m002_f1_historical(conn: Connection) -> None:
    create_tables(conn, "f1_hist_session", "f1_hist_frame", "f1_hist_lap", "f1_hist_weather", "f1_hist_telemetry", "f1_hist_position")


def _m003_f1_import_jobs(conn: Connection) -> None:
    create_tables(conn, "f1_import_job")


def _m004_f1_retired(conn: Connection) -> None:
    """Retired. It created F1 tables for a feature that has since been removed (see migration 8, which drops them). It stays in
    the list, doing nothing, so the version numbers of existing databases keep their meaning. Migration 9 recreates the tables
    that migration 8 removed."""


def _m005_ww_household_tariffs(conn: Connection) -> None:
    create_tables(conn, "ww_household", "ww_tariff")


def _m006_ww_meter_library(conn: Connection) -> None:
    create_tables(conn, "ww_meter_import", "ww_meter_reading")


def _m007_f1_hist_datasets(conn: Connection) -> None:
    create_tables(conn, "f1_hist_dataset")


F1_RETIRED_TABLES = GAME_TABLES  # the tables migration 8 dropped (and migration 9 recreated)


def _m008_f1_openf1_only(conn: Connection) -> None:
    """F1 SETUP LAB keeps only OpenF1 historical data: remove its setup experiments (every SDK row of the experiments the
    ``app_experiment_owner`` table assigns to F1 SETUP LAB), drop its retired tables, and remove historical sessions and import
    jobs of any provider other than OpenF1. WATTWISE LAB experiments are not touched. This was a one-time cleanup."""
    import json

    from sqlalchemy import text

    existing = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
    if "app_experiment_owner" in existing:
        ids = [r[0] for r in conn.execute(text("SELECT experiment_id FROM app_experiment_owner WHERE app = 'f1-setup-lab'"))]
        for table in SDK_EXPERIMENT_TABLES:
            if table in existing:
                for eid in ids:
                    conn.execute(text(f'DELETE FROM "{table}" WHERE experiment_id = :e'), {"e": eid})
        conn.execute(text("DELETE FROM app_experiment_owner WHERE app = 'f1-setup-lab'"))
    for table in F1_RETIRED_TABLES:
        conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
    if "f1_hist_session" in existing:
        conn.execute(text("DELETE FROM f1_hist_session WHERE provider <> 'openf1'"))  # frames, rows and datasets cascade
    if "f1_import_job" in existing:
        for import_id, request in conn.execute(text("SELECT import_id, request_json FROM f1_import_job")).all():
            try:
                provider = json.loads(request).get("provider") or "fastf1"  # requests from before providers existed were FastF1
            except ValueError:
                provider = "unknown"
            if provider != "openf1":
                conn.execute(text("DELETE FROM f1_import_job WHERE import_id = :i"), {"i": import_id})


def _m009_f1_setup_restored(conn: Connection) -> None:
    """Restore the F1 SETUP LAB setup-engineering tables (parents first). Idempotent: existing tables are left alone."""
    create_tables(conn, "app_experiment_owner")
    create_game_tables(conn)  # frozen DDL: these six tables have no models any more (see migration 10)


def _m010_remove_game_data(conn: Connection) -> None:
    """Remove the Live Simulator feature's data: its experiments (every SDK row) and the six game tables. The caller backs the
    database up first (``db.backup.backup_hook``). Historical F1, synthetic experiments and WATTWISE data are not touched."""
    remove_game_data(conn)


MIGRATIONS: list[Migration] = [
    Migration(1, "experiment owner table", _m001_owner),
    Migration(2, "f1 historical sessions", _m002_f1_historical),
    Migration(3, "f1 import jobs", _m003_f1_import_jobs),
    Migration(4, "f1 retired tables (dropped by migration 8)", _m004_f1_retired),
    Migration(5, "wattwise household settings and tariff library", _m005_ww_household_tariffs),
    Migration(6, "wattwise historical meter-data library", _m006_ww_meter_library),
    Migration(7, "f1 historical per-session datasets (race position, results, grid, intervals, ...)", _m007_f1_hist_datasets),
    Migration(8, "f1 keeps only OpenF1 historical data: remove its experiments and retired tables", _m008_f1_openf1_only),
    Migration(9, "f1 setup lab restored: owner table and six setup-engineering tables", _m009_f1_setup_restored),
    Migration(10, "remove live simulator data: its experiments and the six game tables", _m010_remove_game_data),
]
