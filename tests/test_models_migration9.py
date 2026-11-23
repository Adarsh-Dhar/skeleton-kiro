"""Unit tests for the restored F1 SETUP LAB models and migration 9."""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from db import create_db_engine, current_version
from db.migrations import migrate
from db.migrations import versions
from db.migrations.game_data import DDL, create_game_tables
from db.migrations.versions import MIGRATIONS

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "f1-setup-lab" / "tests" / "fixtures"
RESTORED = tuple(DDL)  # the six tables migration 9 creates, parents first
def _golden(name):
    data = json.loads((FIXTURES / name).read_text())
    for t in data.values():  # the seq number of an index is not stable across runs; compare the rest
        t["index_list"] = sorted(r[1:] for r in t["index_list"])
    return data


RESTORED_SCHEMA = _golden("restored_schema.json")
PRE_SCHEMA = _golden("schema_pre_restoration.json")


def _dump(engine, names) -> dict:
    out = {}
    with engine.connect() as c:
        for n in sorted(names):
            out[n] = {
                "table_info": [list(r) for r in c.exec_driver_sql(f'PRAGMA table_info("{n}")')],
                "index_list": sorted([list(r)[1:] for r in c.exec_driver_sql(f'PRAGMA index_list("{n}")')]),
                "foreign_key_list": [list(r) for r in c.exec_driver_sql(f'PRAGMA foreign_key_list("{n}")')],
            }
    return out


def _tables(engine) -> set[str]:
    with engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))}


def test_create_tables_matches_restored_schema(tmp_path):
    e = create_db_engine(tmp_path / "a.db")
    with e.begin() as c:
        create_game_tables(c, *RESTORED)
    assert _dump(e, RESTORED) == RESTORED_SCHEMA


def test_migration_9_matches_restored_and_pre_restoration_schemas(tmp_path):
    e = create_db_engine(tmp_path / "b.db")
    migrate(e, target=9)
    assert current_version(e) == 9
    assert _dump(e, RESTORED) == RESTORED_SCHEMA
    others = _tables(e) - set(RESTORED)
    assert others == set(PRE_SCHEMA)
    assert _dump(e, others) == PRE_SCHEMA


def test_create_tables_keeps_rows_and_creates_only_missing(tmp_path):
    e = create_db_engine(tmp_path / "c.db")
    with e.begin() as c:
        create_game_tables(c, "f1_udp_recording", "f1_probe_report")
        c.execute(text("INSERT INTO f1_udp_recording (recording_id, started_at, source) VALUES ('r1', CURRENT_TIMESTAMP, 'live')"))
        c.execute(text("INSERT INTO f1_probe_report (report_id, created_at, report_json) VALUES ('p1', CURRENT_TIMESTAMP, '{}')"))
    assert _tables(e) == {"f1_udp_recording", "f1_probe_report"}
    with e.begin() as c:
        create_game_tables(c, *RESTORED)
    assert set(RESTORED) <= _tables(e)
    with e.connect() as c:
        assert c.execute(text("SELECT recording_id FROM f1_udp_recording")).all() == [("r1",)]
        assert c.execute(text("SELECT report_id FROM f1_probe_report")).all() == [("p1",)]
        assert c.execute(text("SELECT count(*) FROM f1_udp_packet")).scalar() == 0


def test_duplicate_table_name_raises_at_import_and_names_the_table():
    code = (
        "import db.models\n"
        "from db.base import Base\n"
        "from sqlalchemy import Integer\n"
        "from sqlalchemy.orm import Mapped, mapped_column\n"
        "class Dup(Base):\n"
        "    __tablename__ = 'f1_import_job'\n"
        "    id: Mapped[int] = mapped_column(Integer, primary_key=True)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0
    assert "InvalidRequestError" in r.stderr and "f1_import_job" in r.stderr


def test_migration_list_ends_with_10_and_docstrings_are_updated():
    assert [m.version for m in MIGRATIONS] == list(range(1, 11))
    assert MIGRATIONS[-1].version == 10
    assert "one-time cleanup" in inspect.getdoc(versions._m008_f1_openf1_only)
    assert "migration 9" in inspect.getdoc(versions._m004_f1_retired).lower()


def test_migrations_1_to_8_then_9_keep_historical_data_and_add_restored_tables(tmp_path):
    e = create_db_engine(tmp_path / "d.db")
    migrate(e, target=8)
    assert current_version(e) == 8 and not (set(RESTORED) & _tables(e))
    with e.begin() as c:
        c.execute(text("INSERT INTO f1_hist_session (session_id, status, provider, manifest_json, imported_at) "
                       "VALUES ('openf1-2025-x-r', 'complete', 'openf1', '{}', CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO f1_hist_frame (session_id, kind, driver, columns_json, rows) VALUES ('openf1-2025-x-r', 'laps', '', '[]', 0)"))

    def rows():
        with e.connect() as c:
            return [c.execute(text(f"SELECT * FROM {t} ORDER BY 1, 2")).all() for t in ("f1_hist_session", "f1_hist_frame")]

    before = rows()
    applied = migrate(e, target=9)
    assert [m.version for m in applied] == [9]
    assert rows() == before
    assert set(RESTORED) | {"app_experiment_owner"} <= _tables(e)
    assert migrate(e, target=9) == []


def test_migration_list_is_sorted_unique():
    vs = [m.version for m in MIGRATIONS]
    assert vs == sorted(set(vs))
