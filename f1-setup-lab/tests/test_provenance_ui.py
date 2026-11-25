"""The experiment header shows the experiment-level source label, counts and notice (Chromium)."""
import time

import httpx
import pytest
from test_frontend_setup_lab import page, stack  # noqa: F401  (fixtures)

from f1lab import IMPORTED_NOTICE, SYNTHETIC_NOTICE

pytest.importorskip("playwright.sync_api")


def _wait_done(base, eid):
    for _ in range(600):
        d = httpx.get(f"{base}/api/experiments/{eid}").json()
        if d["counts"]["queued"] + d["counts"]["running"] == 0:
            return d
        time.sleep(0.05)
    raise AssertionError("experiment did not finish")


def _header(pg, base, eid):
    pg.goto(f"{base}/#/e/{eid}/design")
    pg.reload()
    pg.wait_for_selector("#source-label")
    return pg.inner_text("#source-label"), pg.inner_text("#source-counts")


def test_header_shows_synthetic_then_imported_then_mixed(page, stack):  # noqa: F811
    base = stack
    body = {"name": "provenance ui", "design": {"method": "ofat", "replicates": 1}, "simulator": {"latency_seconds": 0.0}}
    d = httpx.post(f"{base}/api/experiments", json=body).json()
    eid, first = d["id"], d["runs"][0]["run_id"]

    label, counts = _header(page, base, eid)
    assert label == "SYNTHETIC" and counts.startswith("0 synthetic") and "0 imported" in counts
    assert page.locator("#source-notice").count() == 0 and "chip synthetic" in page.get_attribute("#source-label", "class")

    csv_text = httpx.get(f"{base}/api/demo/telemetry_ms.csv").text
    r = httpx.post(f"{base}/api/experiments/{eid}/runs/{first}/telemetry", json={"csv": csv_text, "time_unit": "ms", "filename": "s.csv"})
    assert r.status_code == 200
    label, counts = _header(page, base, eid)
    assert label == "IMPORTED TELEMETRY" and counts.startswith("0 synthetic") and "1 imported" in counts
    assert "imported" in page.get_attribute("#source-label", "class").split()
    assert page.inner_text("#source-notice") == IMPORTED_NOTICE

    httpx.post(f"{base}/api/experiments/{eid}/execute")
    done = _wait_done(base, eid)["counts"]["completed"]
    label, counts = _header(page, base, eid)
    assert label == "MIXED (SYNTHETIC + IMPORTED)" and counts.startswith(f"{done - 1} synthetic") and "1 imported" in counts
    notice = page.inner_text("#source-notice")
    assert SYNTHETIC_NOTICE in notice and IMPORTED_NOTICE in notice
    assert "mixed" in page.get_attribute("#source-label", "class").split()
    assert page.errors == []
