"""Req 9.4: the job view exposes integer processed_rows / total_rows next to the text detail."""
from db import create_db_engine, migrate
from f1lab.integrations.historical.jobs import ImportJobStore, parse_row_progress


def test_parse_row_progress():
    assert parse_row_progress("35% (1400/4000 rows)") == (1400, 4000)
    assert parse_row_progress("normalizing") is None


def test_job_view_reports_rows_and_defaults_to_zero(tmp_path):
    e = create_db_engine(tmp_path / "j.db")
    migrate(e)
    store = ImportJobStore(e)
    job = store.create({"year": 2024, "event": "x", "session": "R", "drivers": ["VER"]})
    assert (job["processed_rows"], job["total_rows"]) == (0, 0)
    store.progress(job["import_id"], "PROCESSING", "35% (1400/4000 rows)")
    got = store.get(job["import_id"])
    assert (got["processed_rows"], got["total_rows"]) == (1400, 4000) and got["detail"] == "35% (1400/4000 rows)"
