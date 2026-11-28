"""Task 6.2: session import is separate from explicit attach; provenance flows into measurements; source facts are required."""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from f1lab.integrations.historical.providers import PROVENANCE_DEMO, PROVENANCE_USER
from f1lab.services.lab import LabService, MissingSourceField, source_facts

CSV = "lap,sector_1,sector_2,sector_3,lap_time\n1,30,30,30,90\n2,31,30,30,91\n"
BODY = {"name": "x", "hypothesis": "h"}


@pytest.fixture
def lab(db_path):
    return LabService(db_path)


def _experiment(client):
    r = client.post("/api/experiments", json={"name": "attach split"})
    assert r.status_code in (200, 201), r.text
    d = r.json()
    return d["id"], d["runs"][0]["run_id"]


def _attach(client, lab_unused=None, **kw):
    exp, run = _experiment(client)
    return exp, run


def test_import_session_does_not_attach(client, db_path):
    lab = LabService(db_path)
    exp, _ = _experiment(client)
    before = lab.detail(exp)["source_counts"]
    s = lab.import_session(CSV, "s", "a.csv")
    assert s["lap_count"] == 2 and s["sector_count"] == 6
    assert s["file_hash"] == hashlib.sha256(CSV.encode()).hexdigest()
    assert s["provenance_status"] == PROVENANCE_USER and s["synthetic"] is False
    assert lab.detail(exp)["source_counts"] == before
    assert all(not r["imported"] and r["lap_count"] == 0 for r in lab.runs(exp))


@pytest.mark.parametrize("status,synthetic", [(PROVENANCE_DEMO, True), (PROVENANCE_USER, False)])
def test_attach_passes_provenance_and_facts(client, db_path, status, synthetic):
    lab = LabService(db_path)
    exp, run = _experiment(client)
    out = asyncio.run(lab.attach_telemetry(exp, run, CSV, "s", "a.csv", status))
    assert out["run"]["lap_count"] == 2 and out["run"]["sector_count"] == 6
    assert out["lap_count"] == 2 and out["sector_count"] == 6
    r = next(r for r in lab._result(exp).runs if r.run_id == run)
    for m in r.measurements:
        assert m.metadata["synthetic"] is synthetic and m.metadata["imported"] is (not synthetic)
        assert m.metadata["original_filename"] == "a.csv"
        assert m.metadata["file_hash"] == hashlib.sha256(CSV.encode()).hexdigest()
        assert m.metadata["column_mapping"]["lap_time"] == "lap_time" and m.metadata["units"] == {"time": "seconds"}


@pytest.mark.parametrize("field,args", [("original_filename", ("s", "  ")), ("file_hash", None)])
def test_missing_source_field_is_named_and_nothing_persisted(client, db_path, field, args):
    lab = LabService(db_path)
    exp, run = _experiment(client)
    if args is None:
        facts = source_facts("", "s", "a.csv")
        with pytest.raises(MissingSourceField) as e:
            from f1lab.services.lab import require_source_facts
            require_source_facts(facts)
        assert field in str(e.value) and e.value.field == field
        return
    with pytest.raises(MissingSourceField, match=field):
        asyncio.run(lab.attach_telemetry(exp, run, CSV, args[0], args[1]))
    assert lab.runs(exp)[0]["lap_count"] == 0 and not lab._result(exp).runs[0].measurements


def test_missing_mapping_and_units_are_named():
    from f1lab.services.lab import require_source_facts
    ok = source_facts(CSV, "s", "a.csv")
    for name in ("column_mapping", "units"):
        with pytest.raises(MissingSourceField, match=name):
            require_source_facts({**ok, name: {}})
