"""Browser test: an OpenF1 import completes against the fake OpenF1 server with no CSV action at all.

Independent of the CSV browser test: it has its own server, database and browser, and never touches the CSV
provider, its stepper or its endpoints. A screenshot is saved to the test results directory
(env F1_TEST_RESULTS_DIR, default f1-setup-lab/test-results)."""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from openf1_fakes import FakeOpenF1, fake_client

from f1lab.api.app import create_app

pw = pytest.importorskip("playwright.sync_api")
SID = "openf1-2025-abu-dhabi-grand-prix-r"
RESULTS = Path(os.environ.get("F1_TEST_RESULTS_DIR") or Path(__file__).resolve().parents[1] / "test-results")


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("openf1-independent-ui")
    fake = FakeOpenF1()
    app = create_app(str(root / "trialforge.db"), openf1_client_factory=lambda: fake_client(root / "openf1-cache", fake)[0])
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


def test_openf1_import_completes_without_any_csv_step(base_url):
    from playwright.sync_api import sync_playwright

    RESULTS.mkdir(parents=True, exist_ok=True)
    shot = RESULTS / "openf1_import_no_csv.png"
    shot.unlink(missing_ok=True)
    csv_requests: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: csv_requests.append(r.url) if "csv" in r.url.lower() else None)
            page.goto(base_url + "/#/hist")
            page.wait_for_selector("#hprov")
            page.wait_for_function("document.querySelector('#hevent').options.length === 2 && !document.querySelector('#hevent').innerText.includes('Loading')")
            assert page.input_value("#hprov") == "openf1" and page.is_visible("#hopenf1")
            page.select_option("#hevent", index=1)
            page.select_option("#hsession", "R")
            page.wait_for_selector(".hdrv")
            assert page.locator(".hdrv:checked").count() == 2
            page.click("#himport")
            page.wait_for_function("document.querySelector('#hprog')?.textContent.includes('COMPLETED')", timeout=30000)
            page.wait_for_url(f"**/#/hist/{SID}/overview", timeout=15000)
            page.wait_for_selector(".chip.hist")
            assert page.inner_text(".chip.hist") == "HISTORICAL F1 · OPENF1"
            meta = page.evaluate(f"fetch('/api/historical/sessions/{SID}').then(r => r.json())")
            assert meta["provider"] == "openf1" and meta["session_id"] == SID
            page.screenshot(path=str(shot), full_page=True)
            assert errors == []
        finally:
            browser.close()
    assert shot.exists() and shot.stat().st_size > 0
    assert csv_requests == [], "the OpenF1 flow must not touch any CSV endpoint"
