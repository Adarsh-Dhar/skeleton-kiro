"""Task 3.3: import_session_csv / CSVProvider smoke and behaviour tests."""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text

from db.engine import create_db_engine
from db.migrations import migrate
from f1lab.integrations.historical import csv_importer
from f1lab.integrations.historical.importer import HistoricalImportError, ImportCancelled
from f1lab.integrations.historical.providers import PROVIDERS, CSVProvider
from f1lab.integrations.historical.repository import HistoricalRepository

META = {"year": 2024, "event": "Imola testing", "circuit": "Imola", "session_type": "FP2", "driver": "VER",
        "session_date": "2024-05-18", "timezone": "Europe/Rome"}
UNITS = {"time": "seconds", "distance": "metres", "speed": "kph"}

LAPS = "lap,sector_1_s,sector_2_s,sector_3_s,lap_time_s,driver\n1,20.1,30.2,25.3,75.6,VER\n2,20.0,30.1,25.2,75.3,VER\n3,20.5,30.0,25.0,75.5,HAM\n"
TEL = ("session_time_s,lap,distance_m,speed_kph,throttle_pct,driver\n"
       + "".join(f"{i * 0.1:.1f},{1 + i // 50},{i * 3.0},{100 + i % 40},{(i * 7) % 101},{'VER' if i % 5 else 'HAM'}\n" for i in range(200)))


@pytest.fixture()
def env(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    up = tmp_path / "csv-uploads"
    up.mkdir()
    return HistoricalRepository(e), up, e


def stage(up, body: str, name="upload.csv", engine=None) -> str:
    h = hashlib.sha256(body.encode()).hexdigest()
    (up / f"{h}.csv").write_text(body)
    if engine is not None:
        with engine.begin() as c:
            c.execute(text("INSERT OR REPLACE INTO f1_csv_upload (file_hash, original_filename, size_bytes, uploaded_at) VALUES (:h,:n,:s,'2024-01-01')"),
                      {"h": h, "n": name, "s": len(body)})
    return h


def payload(h, **over):
    csv = {"file_hash": h, "units": dict(UNITS), "metadata": dict(META), **over}
    return {"provider": "csv_upload", "csv": csv}


def test_registry_and_provider_wrapper(env):
    repo, up, e = env
    assert isinstance(PROVIDERS["csv_upload"], CSVProvider)
    h = stage(up, LAPS, engine=e)
    m = PROVIDERS["csv_upload"].run(repo, payload(h), None, None, upload_dir=up, client=None, force=True)
    assert m["provider"] == "csv_upload" and repo.exists(m["session_id"])


def test_lap_summary_smoke(env):
    repo, up, e = env
    h = stage(up, LAPS, "laps.csv", e)
    events = []
    m = csv_importer.import_session_csv(repo, payload(h), lambda s, d: events.append((s, d)), upload_dir=up)
    sid = m["session_id"]
    laps = repo.laps(sid)
    assert len(laps) == 2 and set(laps["driver"]) == {"VER"}  # HAM row excluded
    assert m["data_type"] == "Lap summaries" and m["import_stats"]["rows_excluded"] == 1
    assert repo.telemetry(sid, "VER").empty
    assert any(d.startswith("0%") for _, d in events) and events[-1][0] == "SAVING"
    # provenance round-trips through the session row and the column mapping table
    with e.connect() as c:
        row = c.execute(text("SELECT file_hash, metadata_json, source_type, verification FROM f1_hist_session WHERE session_id=:s"), {"s": sid}).one()
        mp = c.execute(text("SELECT mapping_json, units_json FROM f1_hist_column_mapping WHERE session_id=:s"), {"s": sid}).one()
    assert row[0] == h and (row[2], row[3]) == ("user_uploaded", "unverified")
    meta = json.loads(row[1])
    assert meta["provider"] == "User CSV" and meta["original_filename"] == "laps.csv" and meta["unavailable"] == []
    assert json.loads(mp[0])["lap_number"] == "lap" and json.loads(mp[1])["time"] == "seconds"


def test_sampled_telemetry_smoke(env):
    repo, up, e = env
    h = stage(up, TEL, "tel.csv", e)
    m = csv_importer.import_session_csv(repo, payload(h, data_type="sampled_telemetry"), upload_dir=up)
    sid = m["session_id"]
    tel = repo.telemetry(sid, "VER")
    assert m["data_type"] == "Full telemetry" and len(tel) == 160 and m["import_stats"]["rows_excluded"] == 40
    assert tel["speed_kph"].notna().all() and tel["brake_pct"].isna().all()
    assert repo.laps(sid).empty


def test_metadata_unavailable_markers_and_errors(env):
    repo, up, e = env
    h = stage(up, LAPS, "laps.csv", e)
    meta = {**META, "circuit": None, "timezone": None, "unavailable": ["circuit", "timezone"]}
    m = csv_importer.import_session_csv(repo, payload(h, metadata=meta), upload_dir=up)
    assert m["metadata"]["circuit"] is None and m["metadata"]["unavailable"] == ["circuit", "timezone"]
    assert len(m["metadata"]) == 12  # 11 items + the marker list

    h2 = stage(up, LAPS + "4,1,2,3,6,VER\n", "b.csv", e)
    bad = {**META, "year": 1900, "session_date": "2024-13-01", "timezone": "Mars/Base", "event": "x" * 201}
    del bad["circuit"]
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(repo, payload(h2, metadata=bad), upload_dir=up)
    assert ei.value.code == "METADATA_INVALID"
    d = ei.value.details
    assert d["missing"] == ["circuit"] and {i["item"] for i in d["invalid"]} == {"year", "session_date", "timezone", "event"}
    assert len(repo.list_sessions()) == 1


def test_errors_leave_repository_unchanged(env):
    repo, up, e = env
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(repo, payload("0" * 64), upload_dir=up)
    assert ei.value.code == "UPLOAD_NOT_FOUND"
    h = stage(up, LAPS, engine=e)
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(repo, payload(h, metadata={**META, "driver": "NOP"}), upload_dir=up)
    assert ei.value.code == "DRIVER_NOT_FOUND"
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(repo, payload(h, units={"time": "fortnights"}), upload_dir=up)
    assert ei.value.code == "UNIT_NOT_SUPPORTED"
    bad = stage(up, LAPS.replace("30.2", "-1"), "bad.csv", e)
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(repo, payload(bad), upload_dir=up)
    assert ei.value.code == "ROW_VALIDATION_FAILED" and "row 1" in str(ei.value)
    with pytest.raises(ImportCancelled):
        csv_importer.import_session_csv(repo, payload(h), should_cancel=lambda: True, upload_dir=up)
    assert repo.list_sessions() == []


def test_duplicate_file_and_key(env):
    repo, up, e = env
    h = stage(up, LAPS, "a.csv", e)
    first = csv_importer.import_session_csv(repo, payload(h), upload_dir=up)["session_id"]
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(repo, payload(h), upload_dir=up)
    assert ei.value.code == "DUPLICATE_SESSION" and ei.value.details["session_id"] == first
    h2 = stage(up, LAPS.replace("75.6", "75.7"), "b.csv", e)
    with pytest.raises(HistoricalImportError) as ei:
        csv_importer.import_session_csv(repo, payload(h2), upload_dir=up)
    assert ei.value.code == "DUPLICATE_KEY_CONFLICT" and ei.value.details["session_id"] == first
    assert repo.manifest(first)["file_hash"] == h  # unchanged
    new = csv_importer.import_session_csv(repo, payload(h2, replace_existing=True), upload_dir=up)
    assert new["session_id"] == first and repo.manifest(first)["file_hash"] == h2 and len(repo.list_sessions()) == 1


def test_progress_reported_every_five_percent(env):
    repo, up, e = env
    rows = 25_000
    body = "session_time_s,speed_kph\n" + "".join(f"{i / 10},{100 + i % 50}\n" for i in range(rows))
    h = stage(up, body, "big.csv", e)
    events = []
    csv_importer.import_session_csv(repo, payload(h, data_type="sampled_telemetry", metadata={**META, "driver": "VER"}),
                                    lambda s, d: events.append(d), upload_dir=up)
    pcts = [int(d.split("%")[0]) for d in events if "%" in d.split(" ")[0]]
    assert pcts[0] == 0 and pcts[-1] == 100 and all(b - a <= 5 for a, b in zip(pcts, pcts[1:]))
