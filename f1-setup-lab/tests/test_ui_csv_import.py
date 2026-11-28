"""Browser test: a CSV session import completes end to end while OpenF1 is unreachable.

The app's OpenF1 client factory always fails with a network error, and the browser blocks every request to the
real OpenF1 host, so any dependency of the CSV flow on OpenF1 would break the test. It has its own server, database
and browser. A screenshot is saved to the test results directory (env F1_TEST_RESULTS_DIR, default
f1-setup-lab/test-results)."""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from f1lab.api.app import create_app
from f1lab.integrations.historical.openf1_client import OpenF1Error

pw = pytest.importorskip("playwright.sync_api")
RESULTS = Path(os.environ.get("F1_TEST_RESULTS_DIR") or Path(__file__).resolve().parents[1] / "test-results")


class _UnreachableOpenF1:
    """Stands in for the OpenF1 client: it can be built, but every request fails like a dead network."""

    ttl_s = 0.0

    def __getattr__(self, name):
        def fail(*_a, **_k):
            raise OpenF1Error("OpenF1 is unreachable (test)", "NETWORK_ERROR")
        return fail


def _unreachable():
    return _UnreachableOpenF1()


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("csv-ui")
    app = create_app(str(root / "trialforge.db"), openf1_client_factory=_unreachable)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def test_csv_import_completes_and_opens_with_openf1_unreachable(base_url, tmp_path):
    from playwright.sync_api import sync_playwright

    RESULTS.mkdir(parents=True, exist_ok=True)
    shot = RESULTS / "csv_import_openf1_unreachable.png"
    shot.unlink(missing_ok=True)
    csv = tmp_path / "run.csv"
    csv.write_text("session_time_s,speed_kph,throttle_pct,lap_number\n" + "\n".join(f"{i * 0.5},{200 + i},{50 + i},1" for i in range(20)) + "\n")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**://*openf1.org/**", lambda r: r.abort())  # the real provider is unreachable from the browser too
            page.goto(base_url + "/#/hist")
            page.wait_for_selector("#hprov")
            page.select_option("#hprov", "csv_upload")
            page.wait_for_selector("#hcstepper")
            page.set_input_files("#hcfile", str(csv))
            page.wait_for_function("!document.querySelector('#hcnext').disabled")
            page.click("#hcnext")  # mapping
            page.click("#hcnext")  # units
            page.click("#hcnext")  # metadata
            for k, v in {"year": "2025", "event": "Test GP", "circuit": "Test", "session_type": "R", "driver": "VER", "session_date": "2025-03-01", "timezone": "UTC"}.items():
                page.fill(f"#hcm-{k}", v)
            page.click("#hcnext")  # validation and preview
            page.wait_for_selector("#hcgraphs svg", timeout=5000)
            page.wait_for_function("!document.querySelector('#hcnext').disabled")
            page.click("#hcnext")  # confirm
            page.click("#hcstart")
            page.wait_for_function("location.hash.startsWith('#/hist/') && location.hash.endsWith('/overview')", timeout=30000)
            # the overview of the imported session has rendered (the provider badge is upper-cased by CSS)
            page.wait_for_function("(t => t.includes('csv upload') && t.includes('test gp'))(document.querySelector('#view').innerText.toLowerCase())", timeout=10000)
            assert "csv upload" in page.inner_text("#view").lower() and "Test GP" in page.inner_text("#view")
            assert page.locator(".chip.hist").count() == 0  # the OpenF1 provenance chip is not shown for a CSV session
            sid = page.evaluate("location.hash.split('/')[2]")
            meta = page.evaluate(f"fetch('/api/historical/sessions/{sid}').then(r => r.json())")
            assert meta["provider"] == "csv_upload" and meta["session_id"] == sid
            page.screenshot(path=str(shot), full_page=True)
            assert errors == []
        finally:
            browser.close()
    assert shot.exists() and shot.stat().st_size > 0
