"""The ordered migration list. Append only; never edit or reorder a released migration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection

from db.base import Base


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Connection], None]


def create_tables(conn: Connection, *names: str) -> None:
    """Create the named model tables (and their indexes) if missing."""
    import db.models  # noqa: F401  (registers every table on Base.metadata)

    for name in names:
        Base.metadata.tables[name].create(conn, checkfirst=True)


def _m001_owner(conn: Connection) -> None:
    create_tables(conn, "app_experiment_owner")


def _m002_f1_historical(conn: Connection) -> None:
    create_tables(conn, "f1_hist_session", "f1_hist_frame", "f1_hist_lap", "f1_hist_weather", "f1_hist_telemetry", "f1_hist_position")


def _m003_f1_import_jobs(conn: Connection) -> None:
    create_tables(conn, "f1_import_job")


def _m004_f1_udp_and_probe(conn: Connection) -> None:
    create_tables(conn, "f1_udp_recording", "f1_udp_packet", "f1_stint_evidence", "f1_stint_telemetry", "f1_probe_report",
                  "f1_setup_mapping_confirmation")


def _m005_ww_household_tariffs(conn: Connection) -> None:
    create_tables(conn, "ww_household", "ww_tariff")


def _m006_ww_meter_library(conn: Connection) -> None:
    create_tables(conn, "ww_meter_import", "ww_meter_reading")


MIGRATIONS: list[Migration] = [
    Migration(1, "experiment owner table", _m001_owner),
    Migration(2, "f1 historical sessions", _m002_f1_historical),
    Migration(3, "f1 import jobs", _m003_f1_import_jobs),
    Migration(4, "f1 udp recordings, stint evidence, probe reports", _m004_f1_udp_and_probe),
    Migration(5, "wattwise household settings and tariff library", _m005_ww_household_tariffs),
    Migration(6, "wattwise historical meter-data library", _m006_ww_meter_library),
]
