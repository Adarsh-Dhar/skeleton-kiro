"""Browser tests of real historical data in the UI, against the fake OpenF1 server (no network):
the default New experiment screen (shared picker) and the Historical F1 page."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn
from openf1_fakes import FakeOpenF1, fake_client

from f1lab.api.app import create_app

pw = pytest.importorskip("playwright.sync_api")
SID = "openf1-2025-abu-dhabi-grand-prix-r"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
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
    yield f"http://127.0.0.1:{port}", fake
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page(stack):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 1000})
        pg.errors = []
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.base, pg.fake = stack
        yield pg
        browser.close()


def ready(page, hash_):
    page.goto(page.base + "/about:blank")
    page.goto(page.base + "/" + hash_)
    page.reload()
    page.wait_for_selector("#hprov")
    page.wait_for_function("document.querySelector('#hevent').options.length === 2 && !document.querySelector('#hevent').innerText.includes('Loading')")


# ------------------------------------------------------------------------------------------------ New experiment
def test_the_app_has_seven_nav_entries_and_historical_f1_shows_only_its_own_notice(page):
    page.goto(page.base + "/#/hist")
    page.wait_for_selector("#hprov")
    assert "HISTORICAL F1 DATA" in page.inner_text("#banner").upper() and "SYNTHETIC" not in page.inner_text("#banner").upper()
    assert [a.inner_text().lower() for a in page.locator("#nav a").all()] == [
        "dashboard", "experiments", "telemetry import", "import from openf1", "imported sessions", "standings", "season comparison"]  # upper-cased by CSS
    assert [h.inner_text().lower() for h in page.locator("#nav .nav-head").all()] == ["setup engineering", "historical f1"]
    body = page.inner_text("body").lower()
    for gone in ("fastf1", "wind direction"):
        assert gone not in body, gone
    for old in ("#/nope", "#/e/x/nope"):
        page.goto(page.base + "/about:blank")
        page.goto(page.base + "/" + old)
        page.wait_for_selector("#view .empty")
        assert "page not found" in page.inner_text("#view").lower(), old
    page.click("#view a:has-text('Import a session')")
    page.wait_for_selector("#hprov")
    page.click("#nav a[data-nav=hist]")
    page.wait_for_selector("#hprov")
    # the app's own files are not served from a stale browser cache
    assert page.evaluate("fetch('/app.js').then(r => r.headers.get('cache-control'))") == "no-cache"
    assert page.errors == []


def test_a_tab_opened_before_an_update_reloads_itself_on_the_next_click(page):
    """Switching pages only changes the #hash, so a tab opened before an update would keep the old JavaScript until reloaded
    by hand. The tab compares the server's frontend fingerprint on every page change and reloads when it differs."""
    builds = iter(["old-build", "new-build"])
    page.route("**/api/frontend-build", lambda r: r.fulfill(status=200, content_type="application/json",
                                                             body=json.dumps({"build": next(builds, "new-build"), "files": 6})))
    page.goto(page.base + "/#/sessions")
    page.wait_for_selector("text=No sessions imported yet")
    page.evaluate("window.__marker = 'tab-before-update'")
    page.click("#nav a[data-nav=hist]")  # the fingerprint differs now: the tab reloads instead of running old code
    page.wait_for_selector("#hprov")
    assert page.evaluate("window.__marker === undefined")  # a real reload happened
    assert page.url.endswith("#/hist")
    page.unroute("**/api/frontend-build")


def test_import_one_driver_and_open_the_analysis(page):
    ready(page, "#/hist")
    # seasons run from the provider's first year to the current one; the newest season without data steps back to the latest with data
    years = page.eval_on_selector_all("#hyear option", "os => os.map(o => +o.value)")
    assert years == sorted(years, reverse=True) and years[-1] == 2023
    assert page.input_value("#hyear") == "2025"
    # Grand Prix, session and drivers are filled from the API; the latest held Grand Prix and the race are preselected
    assert page.eval_on_selector_all("#hevent option", "os => os.map(o => o.textContent)") == ["R1 · Bahrain Grand Prix (Sakhir)", "R2 · Abu Dhabi Grand Prix (Yas Island)"]
    assert "Abu Dhabi" in page.eval_on_selector("#hevent", "e => e.selectedOptions[0].textContent") and page.input_value("#hsession") == "R"
    assert page.eval_on_selector_all("#hsession option", "os => os.map(o => o.value)") == ["Q", "R"]
    page.wait_for_selector(".hdrv")
    labels = page.inner_text("#hdrivers")
    assert "HAM #44" in labels and "Lewis HAMILTON" in labels and "Ferrari" in labels and "VER #1" in labels and "Red Bull Racing" in labels
    assert page.locator(".hdrv:checked").count() == 2 and "All 2 drivers" in page.inner_text("#hdrvsum")
    # searchable multi-select
    page.fill("#hdrvsearch", "verst")
    assert page.locator(".hdrv").count() == 1 and page.get_attribute(".hdrv", "value") == "VER"
    page.fill("#hdrvsearch", "ferrari")
    assert page.get_attribute(".hdrv", "value") == "HAM"
    page.fill("#hdrvsearch", "zzz")
    assert page.locator(".hdrv").count() == 0 and "No driver matches" in page.inner_text("#hdrivers")
    page.fill("#hdrvsearch", "")
    page.click("#hdrvnone")
    assert page.locator(".hdrv:checked").count() == 0 and page.is_disabled("#himport")
    page.check(".hdrv[value=VER]")
    assert "1 of 2 drivers" in page.inner_text("#hdrvsum") and not page.is_disabled("#himport")
    # lap timing is required; individual channels can be switched off
    assert page.is_checked("#hlaps") and page.is_disabled("#hlaps")
    page.uncheck("#dsradio")
    page.click("#himport")
    # "COMPLETED" is shown first; the coverage line and "Opening the session" follow once the session fetch returns
    page.wait_for_function("document.querySelector('#hprog')?.textContent.includes('Opening the session')", timeout=30000)
    assert "COMPLETED" in page.inner_text("#hprog") and "session result: ok" in page.inner_text("#hprog").lower().replace("_", " ")
    page.wait_for_url(f"**/#/hist/{SID}/overview", timeout=10000)
    page.wait_for_selector(".chip.hist")
    assert page.inner_text(".chip.hist") == "HISTORICAL F1 · OPENF1"
    # the stored session carries the real provider, session and driver identifiers
    m = page.evaluate(f"fetch('/api/historical/sessions/{SID}').then(r => r.json())")
    assert m["provider"] == "openf1" and m["session_id"] == SID and m["upstream"]["session_key"] == 99 and m["upstream"]["meeting_key"] == 1001
    assert [(d["code"], d["number"], d["name"], d["team"]) for d in m["drivers"]] == [("VER", "1", "Max VERSTAPPEN", "Red Bull Racing")]
    assert "team_radio" not in m["coverage"]["endpoints"] and m["requested_datasets"] and "session_result" in m["requested_datasets"]
    assert page.errors == []


def test_a_finished_import_does_not_pull_the_user_away_from_another_page(page):
    """Regression: an import kept polling after the user left its page, and when it completed it switched the page to the
    imported session, pulling the user away from wherever they had gone."""
    ready(page, "#/hist")
    page.wait_for_selector(".hdrv")
    page.select_option("#hsession", "Q")
    page.wait_for_selector(".hdrv")
    page.fake.delay_s = 0.15
    try:
        page.click("#himport")
        page.wait_for_function("document.querySelector('#hprog')?.textContent.match(/DOWNLOADING|PROCESSING|QUEUED/)", timeout=15000)
        page.click("#nav a[data-nav=sessions]")  # leave while the import is still running
        page.wait_for_selector("text=Imported sessions")
        jid = page.evaluate("fetch('/api/historical/imports?limit=1').then(r => r.json())")[0]["import_id"]
        end = time.time() + 60
        while page.evaluate(f"fetch('/api/historical/imports/{jid}').then(r => r.json())")["state"] != "COMPLETED":
            assert time.time() < end, "import did not complete"
            page.wait_for_timeout(250)
        page.wait_for_timeout(4000)  # longer than the poll interval plus the auto-open delay
        assert page.url.endswith("#/sessions")
    finally:
        page.fake.delay_s = 0.0
        # leave the shared test server as it was: later tests import this session themselves
        page.evaluate("fetch('/api/historical/sessions/openf1-2025-abu-dhabi-grand-prix-q', {method: 'DELETE'})")
        page.evaluate("fetch('/api/historical/openf1/cache', {method: 'DELETE'})")  # cached laps would hide the failure scenario of a later test


def test_changing_a_selection_resets_the_dependent_controls(page):
    ready(page, "#/hist")
    page.wait_for_selector(".hdrv")
    assert page.is_disabled("#hprov") and page.eval_on_selector_all("#hprov option", "os => os.map(o => o.value)") == ["openf1"]
    assert page.is_enabled("#dsgrid") and page.is_enabled("#dsgaps")
    page.select_option("#hsession", "Q")  # qualifying: race-only datasets switch off and the roster reloads
    page.wait_for_function("document.querySelector('#hdrivers').querySelectorAll('.hdrv').length === 2")
    assert page.is_disabled("#dsgrid") and page.is_disabled("#dsgaps") and page.is_enabled("#dsresult")
    assert "race and sprint sessions only" in page.inner_text("#hdshint")
    page.select_option("#hevent", index=0)  # a different Grand Prix: its sessions are rebuilt and the roster reloads
    page.wait_for_function("document.querySelector('#hdrivers').innerText.includes('No drivers') || document.querySelectorAll('.hdrv').length > 0")
    # a season OpenF1 has not published returns no events and the import button stays off
    page.route("**/api/historical/events?year=2023", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
    page.select_option("#hyear", "2023")
    page.wait_for_function("document.querySelector('#hevent').innerText.includes('No events')")
    assert page.is_disabled("#himport")
    assert page.errors == []


def test_import_failure_is_shown_with_its_code_and_can_be_retried(page):
    ready(page, "#/hist")
    page.wait_for_selector(".hdrv")
    page.select_option("#hsession", "Q")
    page.wait_for_selector(".hdrv")
    page.fake.drop_laps = True
    try:
        page.click("#himport")
        page.wait_for_function("document.querySelector('#hprog')?.textContent.includes('FAILED')", timeout=30000)
        prog = page.inner_text("#hprog")
        assert "NO_LAP_DATA" in prog and "lap timing" in prog.lower() and page.locator("#hretry").count() == 1
        assert page.url.endswith("#/hist")  # a failed import does not navigate away
    finally:
        page.fake.drop_laps = False


# ------------------------------------------------------------------------------------------------ Historical F1 page
def test_import_the_whole_field_and_every_analysis_tab(page):
    ready(page, "#/hist")
    assert page.input_value("#hprov") == "openf1" and page.input_value("#hyear") == "2025"
    page.select_option("#hevent", index=1)
    page.select_option("#hsession", "R")
    page.wait_for_selector(".hdrv")
    assert page.locator(".hdrv").count() == 2 and page.locator(".hdrv:checked").count() == 2
    page.click("#himport")
    page.wait_for_function("document.querySelector('#hprog')?.textContent.includes('COMPLETED')", timeout=30000)
    assert "OpenF1 cache" in page.inner_text("#hprog")
    page.wait_for_url(f"**/#/hist/{SID}/overview", timeout=10000)  # the analysis opens when the import finishes
    page.click("#nav a[data-nav=sessions]")
    page.wait_for_selector("#hist-lib a.btn")
    assert SID in page.inner_text("#hist-lib")
    page.click("#hist-lib a.btn")
    page.wait_for_selector(".chip.hist")
    assert page.inner_text(".chip.hist") == "HISTORICAL F1 · OPENF1"
    text = page.inner_text("#hist-body").lower()  # headings are upper-cased by CSS
    assert "import coverage" in text and "partial" in text and "location missing for" in text and "ver" in text
    page.click("#hist-body details summary")
    details = page.inner_text("#hist-body details").lower()
    assert "not provided by openf1" in details and "steering angle" in details and "lap validity" in details and "distance_m (estimate)" in details
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
    page.click(".tabs a:has-text('Pit, flags')")
    page.wait_for_selector("#hrcbody tr")
    pit = page.inner_text("#view").lower()
    assert "22.5" in pit and "green light" in pit and page.locator("#view a:has-text('open recording')").count() == 1
    assert "before start" in pit and "from message" in pit  # pre-session message, and a driver named only in the text
    assert page.locator("#hrcbody tr:has-text('LAP DELETED') td:nth-child(6)").inner_text().lower().startswith("ham")
    page.select_option("#hrcdrv", "VER")
    assert page.locator("#hrcbody tr").count() == 1 and "black and white" in page.inner_text("#hrcbody").lower()
    page.select_option("#hrcdrv", "*track")
    assert page.locator("#hrcbody tr").count() == 2 and "ham" not in page.inner_text("#hrcbody").lower()
    page.select_option("#hrcdrv", "")
    assert page.get_attribute("#view a:has-text('open recording')", "rel") == "noopener noreferrer"
    assert page.locator("#view svg rect").count() >= 2
    assert page.locator(".tabs a:has-text('Championship')").count() == 0 and page.locator("#dschamp").count() == 0
    page.click(".tabs a:has-text('Lap analysis')")
    page.wait_for_selector("#hlapout .kpis")
    kpis = page.inner_text("#hlapout .kpis").lower()
    assert "n/a" in kpis and "not provided" in kpis
    page.click(".tabs a:has-text('Telemetry')")
    page.wait_for_selector("#htrkout svg")
    assert "raw state codes" in page.inner_text("#htrkout").lower() and "estimate" in page.inner_text("#htrkout").lower()
    assert page.errors == []
