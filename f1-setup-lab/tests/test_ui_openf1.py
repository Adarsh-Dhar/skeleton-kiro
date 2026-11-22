"""Browser test of the OpenF1 provider in Historical F1, against the fake OpenF1 server (no network)."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from openf1_fakes import FakeOpenF1, fake_client

from f1lab.api.app import create_app

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("openf1-ui")
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


def test_openf1_import_and_coverage_panel(server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server + "/#/hist")
        page.wait_for_selector("#hprov")
        assert page.input_value("#hprov") == "openf1" and page.input_value("#hyear") == "2025"
        assert page.get_attribute("#hyear", "min") == "2023"
        page.wait_for_function("document.querySelector('#hevent').options.length === 2")
        page.select_option("#hevent", label="Abu Dhabi Grand Prix (Yas Island)")
        page.select_option("#hsession", value="R")
        page.click("#hloaddrv")
        page.wait_for_selector(".hdrv")
        assert page.locator(".hdrv").count() == 2
        page.click("#himport")
        page.wait_for_function("document.querySelector('#hprog')?.textContent.includes('COMPLETED')", timeout=30000)
        assert "OpenF1 cache" in page.inner_text("#hprog")
        page.wait_for_selector("#hist-lib a.btn")
        assert "OPENF1" in page.inner_text("#hist-lib")
        page.click("#hist-lib a.btn")
        page.wait_for_selector(".chip.hist")
        assert page.inner_text(".chip.hist") == "HISTORICAL F1 · OPENF1"
        text = page.inner_text("#hist-body").lower()  # headings are upper-cased by CSS
        assert "import coverage" in text and "partial" in text and "location missing for" in text and "ver" in text
        page.click("#hist-body details summary")
        assert "not provided by openf1: steering angle" in page.inner_text("#hist-body details").lower()
        # switching to FastF1 restores its year range
        page.goto(server + "/#/hist")
        page.reload()
        page.wait_for_selector("#hprov")
        page.route("**/api/historical/events?*provider=fastf1*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.select_option("#hprov", "fastf1")
        assert page.get_attribute("#hyear", "min") == "2018"
        assert errors == []
        browser.close()
