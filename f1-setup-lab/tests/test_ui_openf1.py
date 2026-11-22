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
        details = page.inner_text("#hist-body details").lower()
        assert "not provided by openf1" in details and "steering angle" in details and "lap validity" in details and "distance_m (estimate)" in details
        # honest "not provided" state for lap validity (OpenF1 publishes none)
        assert "not provided by this source" in text and "running order" in text
        page.click("#hist-body a:has-text('open')")
        page.wait_for_selector("#hrchart svg")
        assert page.locator("#hrchart polyline").count() >= 1
        race = page.inner_text("#view").lower()
        assert "starting grid vs final classification" in race and "dnf" in race and "+1 lap" in race
        assert page.locator(".hrp").count() == 2 and "overtakes" in race
        page.wait_for_selector("#hgapout svg")
        page.select_option("#hgapdrv", "HAM")
        page.wait_for_function("document.querySelector('#hgapout').innerText.includes('HAM:')")
        assert "while lapped" in page.inner_text("#hgapout")
        assert "ham" in page.inner_text("#view").lower() and page.locator("#view table").count() >= 3
        # pit, flags and radio
        page.click(".tabs a:has-text('Pit, flags')")
        page.wait_for_selector("#hrcbody tr")
        pit = page.inner_text("#view").lower()
        assert "22.5" in pit and "green light" in pit and "open recording" in pit and page.locator("#view a:has-text('open recording')").count() == 1
        assert page.get_attribute("#view a:has-text('open recording')", "rel") == "noopener noreferrer"
        assert page.locator("#view svg rect").count() >= 2  # tyre stints
        # championship: beta endpoints returned nothing, and the page says so
        page.click(".tabs a:has-text('Championship')")
        page.wait_for_selector("#view .note")
        champ = page.inner_text("#view").lower()
        assert "beta" in champ and "no driver standings" in champ and "no constructor standings" in champ
        # lap analysis shows validity as not provided, not as zero
        page.click(".tabs a:has-text('Lap analysis')")
        page.wait_for_selector("#hlapout .kpis")
        assert "n/a" in page.inner_text("#hlapout .kpis").lower() and "not provided" in page.inner_text("#hlapout .kpis").lower()
        # DRS: raw codes on a 0-14 axis
        page.click(".tabs a:has-text('Telemetry')")
        page.wait_for_selector("#htrkout svg")
        assert "raw state codes" in page.inner_text("#htrkout").lower() and "estimate" in page.inner_text("#htrkout").lower()
        # switching to FastF1 restores its year range
        page.goto(server + "/#/hist")
        page.reload()
        page.wait_for_selector("#hprov")
        page.route("**/api/historical/events?*provider=fastf1*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.select_option("#hprov", "fastf1")
        assert page.get_attribute("#hyear", "min") == "2018"
        assert errors == []
        browser.close()
