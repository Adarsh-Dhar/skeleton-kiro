"""CSV-foundation: migrations 14/15 keep pre-feature rows, the provider registry, the OpenF1 stored/browse baseline and the provenance write guard.

Validates: Requirements 1.1, 1.5, 1.6, 1.9, 18.3
"""
from __future__ import annotations

import json

import pandas as pd
import pytest
from sqlalchemy import text

from db import create_db_engine, migrate
from f1lab.integrations.historical import providers as P
from f1lab.integrations.historical.importer import HistoricalImportError
from f1lab.integrations.historical.repository import HistoricalRepository


def _cols(engine, table):
    with engine.connect() as c:
        return {r[1] for r in c.execute(text(f'PRAGMA table_info("{table}")'))}
def _tables(engine):
    with engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
# ------------------------------------------------------------------------------------------- migrations
def test_migration_preserves_pre_feature_rows_and_backfills_openf1(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e, target=13)
    with e.begin() as c:
        for sid, provider in (("openf1-2024-monza-q", "openf1"), ("other-2024-x-q", "other")):
            c.execute(text("INSERT INTO f1_hist_session (session_id, status, provider, year, event, session_type, manifest_json, imported_at) "
                           "VALUES (:s, 'complete', :p, 2024, 'Italian Grand Prix', 'Q', :m, CURRENT_TIMESTAMP)"),
                      {"s": sid, "p": provider, "m": json.dumps({"session_id": sid})})
            c.execute(text("INSERT INTO f1_hist_frame (session_id, kind, driver, columns_json, rows) VALUES (:s, 'laps', '', '[]', 0)"), {"s": sid})
    migrate(e)
    assert {"metadata_json", "source_type", "verification", "file_hash"} <= _cols(e, "f1_hist_session")
    assert {"f1_csv_upload", "f1_hist_column_mapping"} <= _tables(e)
    with e.connect() as c:
        rows = {r[0]: r[1:] for r in c.execute(text("SELECT session_id, provider, year, event, manifest_json, source_type, verification FROM f1_hist_session"))}
        frames = c.execute(text("SELECT COUNT(*) FROM f1_hist_frame")).scalar()
    assert set(rows) == {"openf1-2024-monza-q", "other-2024-x-q"} and frames == 2
    assert rows["openf1-2024-monza-q"] == ("openf1", 2024, "Italian Grand Prix", '{"session_id": "openf1-2024-monza-q"}', "external_provider", "verified")
    assert rows["other-2024-x-q"][4:] == (None, None)  # only OpenF1 rows are backfilled
    migrate(e)  # idempotent
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM f1_hist_session")).scalar() == 2
# ------------------------------------------------------------------------------------------- registry
def test_registry_has_exactly_two_providers_implementing_the_interface():
    assert set(P.PROVIDERS) == {"openf1", "csv_upload"} and len(P.PROVIDERS) == 2
    for key, prov in P.PROVIDERS.items():
        assert prov.name == key and callable(prov.run) and prov.source_type
    assert P.PROVIDERS["openf1"].source_type == "external_provider" and P.PROVIDERS["csv_upload"].source_type == "user_uploaded"
# ------------------------------------------------------------------------------------------- OpenF1 regression baseline
def _manifest(sid):
    return {"session_id": sid, "provider": "openf1", "source_type": "external_provider", "verification": "verified", "year": 2024,
            "event": "Italian Grand Prix", "session_type": "Q", "drivers": [{"code": "VER", "number": "1"}, {"code": "LEC", "number": "16"}], "channels": {}}
BASELINE_SESSIONS = [{"session_id": "openf1-2024-monza-q", "provider": "openf1", "year": 2024, "event": "Italian Grand Prix", "circuit": None, "session": None,
                      "session_type": "Q", "session_date": None, "imported_at": None, "drivers": [{"code": "VER", "number": "1"}, {"code": "LEC", "number": "16"}],
                      "channels": {}}]
BASELINE_LAPS = [{"driver": "VER", "lap_number": 1, "lap_time_s": 80.1}, {"driver": "LEC", "lap_number": 1, "lap_time_s": 81.5}]
def test_openf1_stored_record_and_browsing_match_baseline(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    repo = HistoricalRepository(e)
    sid = "openf1-2024-monza-q"
    repo.save_session(sid, _manifest(sid), pd.DataFrame(BASELINE_LAPS), pd.DataFrame(), {}, {})
    assert repo.list_sessions() == BASELINE_SESSIONS  # session list
    assert repo.manifest(sid)["drivers"] == BASELINE_SESSIONS[0]["drivers"]  # driver list
    assert repo.records(repo.laps(sid)) == BASELINE_LAPS  # lap list
    with e.connect() as c:
        row = c.execute(text("SELECT provider, source_type, verification, status FROM f1_hist_session WHERE session_id = :s"), {"s": sid}).one()
    assert tuple(row) == ("openf1", "external_provider", "verified", "complete")
# ------------------------------------------------------------------------------------------- write guard
@pytest.mark.parametrize("missing", ["provider", "source_type", "verification"])
def test_write_rejected_when_provenance_field_missing(tmp_path, missing):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    repo = HistoricalRepository(e)
    m = _manifest("s1")
    del m[missing]
    with pytest.raises(HistoricalImportError, match=missing):
        repo.save_session("s1", m, pd.DataFrame({"driver": ["VER"], "lap_number": [1]}), pd.DataFrame(), {}, {})
    assert repo.list_sessions() == [] and not repo.exists("s1")
