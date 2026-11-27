"""Migration 6 creates the historical version-6 meter tables; migration 11 adds the later columns. Fresh installs and upgrades
from versions 6, 9 and 10 end with the same schema, keep their rows, and re-running is a no-op."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from db import create_db_engine, current_version
from db.migrations import migrate

IMPORT_V11 = {"provenance", "total_import_kwh", "total_export_kwh"}
READING_V11 = {"export_kwh", "export_kw"}
def _cols(engine, table):
    with engine.connect() as c:
        return {r[1] for r in c.exec_driver_sql(f'PRAGMA table_info("{table}")')}
def _schema(engine):
    with engine.connect() as c:
        return {t: [list(r[1:]) for r in c.exec_driver_sql(f'PRAGMA table_info("{t}")')] for t in ("ww_meter_import", "ww_meter_reading")}
def _seed(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO ww_meter_import (import_id, file_name, imported_at, source, timezone, reading_kind, unit, rows_total, "
                       "rows_used, rows_skipped, content_sha256, options_json, warnings_json, row_log_json, gaps_json) "
                       "VALUES ('i1', 'f.csv', CURRENT_TIMESTAMP, 'csv upload', 'UTC', 'power', 'kW', 2, 2, 0, 'h', '{}', '[]', '[]', '[]')"))
        for seq, kwh in ((0, 0.25), (1, 0.5)):
            c.execute(text("INSERT INTO ww_meter_reading (import_id, seq, ts_utc, interval_minutes, power_kw, energy_kwh) "
                           f"VALUES ('i1', {seq}, '2025-01-01T00:0{seq}:00+00:00', 15, 1, {kwh})"))
@pytest.mark.parametrize("version", [6, 9, 10])
def test_database_stopped_at_version_has_no_version_11_columns(tmp_path, version):
    e = create_db_engine(tmp_path / "v.db")
    migrate(e, target=version)
    assert not (_cols(e, "ww_meter_import") & IMPORT_V11)
    assert not (_cols(e, "ww_meter_reading") & READING_V11)
@pytest.fixture
def fresh_schema(tmp_path):
    e = create_db_engine(tmp_path / "fresh.db")
    migrate(e)
    assert current_version(e) == 13
    return _schema(e)
def test_fresh_install_has_version_11_columns(fresh_schema):
    assert IMPORT_V11 <= {r[0] for r in fresh_schema["ww_meter_import"]}
    assert READING_V11 <= {r[0] for r in fresh_schema["ww_meter_reading"]}
@pytest.mark.parametrize("version", [6, 9, 10])
def test_upgrade_adds_columns_keeps_rows_and_matches_fresh_schema(tmp_path, version, fresh_schema):
    e = create_db_engine(tmp_path / "up.db")
    migrate(e, target=version)
    _seed(e)
    applied = migrate(e)
    assert [m.version for m in applied] == list(range(version + 1, 14))  # through 13 (bill reconciliation)
    assert _schema(e) == fresh_schema
    with e.connect() as c:
        imp = c.execute(text("SELECT provenance, total_import_kwh, total_export_kwh FROM ww_meter_import")).one()
        assert tuple(imp) == ("unknown", 0.75, 0.0)
        assert c.execute(text("SELECT energy_kwh, export_kwh, export_kw FROM ww_meter_reading ORDER BY seq")).all() == [(0.25, 0.0, 0.0), (0.5, 0.0, 0.0)]
    assert migrate(e) == []  # idempotent
def test_migration_11_tolerates_columns_that_already_exist(tmp_path):
    """A database that an earlier build created from the current models already has the columns."""
    from db.migrations.versions import _m011_ww_provenance_and_export
    e = create_db_engine(tmp_path / "x.db")
    migrate(e)
    with e.begin() as c:
        _m011_ww_provenance_and_export(c)
        _m011_ww_provenance_and_export(c)
    assert IMPORT_V11 <= _cols(e, "ww_meter_import")
