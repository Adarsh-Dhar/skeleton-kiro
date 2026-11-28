"""Browser DOM tests of the unified import UI (Requirements 13.9, 16.1, 16.4, 16.5, 16.7, 17.1-17.3, Setup_Link form validation).

The real app serves the real frontend files from an empty database; the CSV and session endpoints under test are answered by
Playwright route handlers so every test controls exactly what the backend "says". Skipped when Playwright is not installed."""
from __future__ import annotations

import json
import socket
import threading
import time
from urllib.parse import urlparse

import pytest
import uvicorn

from f1lab.api.app import create_app

pw = pytest.importorskip("playwright.sync_api")

FILE = {"file_hash": "a" * 64, "original_filename": "run.csv", "row_count": 4, "column_count": 3, "detected_type": "sampled_telemetry",
        "columns": ["t", "spd", "lap"], "sample_rows": [["0", "100", "1"], ["1", "120", "1"], ["2", "140", "1"], ["3", "150", "1"]],
        "suggested_mapping": {"session_time_s": "t", "speed_kph": "spd", "lap_number": "lap"}, "data_type_choices": ["sampled_telemetry"]}
META = {"year": "2025", "event": "Abu Dhabi", "circuit": "Yas Marina", "session_type": "R", "driver": "VER",
        "session_date": "2025-12-07", "timezone": "Asia/Dubai"}
SESSIONS = [
    {"session_id": "openf1-1", "event": "Bahrain GP", "session": "R", "provider": "openf1", "data_type_label": "Sampled telemetry",
     "provenance_status": "OPENF1 \u2014 external provider", "year": 2025, "drivers": [{"code": "VER"}], "imported_at": "2025-03-01T10:00:00"},
    {"session_id": "csv-1", "event": "Monza", "session": "R", "provider": "csv_upload", "data_type_label": "Lap summaries",
     "provenance_status": "USER-IMPORTED \u2014 CSV upload", "year": 2024, "drivers": [{"code": "LEC"}], "imported_at": "2025-04-01T10:00:00"},
]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("csv-ui")
    app = create_app(str(root / "trialforge.db"))
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


class Mock:
    """Per-test answers for the endpoints under test; every request is recorded."""
    def __init__(self):
        self.answers = {}
        self.calls = []

    def handle(self, route):
        req = route.request
        path = urlparse(req.url).path
        self.calls.append((req.method, path, req.post_data))
        if path == "/api/historical/events":
            return route.fulfill(status=200, content_type="application/json", body="[]")
        a = self.answers.get((req.method, path))
        if a is None:
            return route.continue_()
        a = a() if callable(a) else a
        status, body = a if isinstance(a, tuple) else (200, a)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def page(stack):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 1000})
        pg.base = stack
        pg.mock = Mock()
        pg.route("**/api/**", pg.mock.handle)
        yield pg
        browser.close()


def open_csv(page, tmp_path):
    page.mock.answers[("POST", "/api/historical/csv/preview")] = FILE
    page.goto(page.base + "/#/hist")
    page.wait_for_selector("#hprov")
    page.select_option("#hprov", "csv_upload")
    f = tmp_path / "run.csv"
    f.write_text("t,spd,lap\n0,100,1\n")
    return f


def next_disabled(page):
    return page.is_disabled("#hcnext")


def stepper(page):
    return page.inner_text("#hcstepper [aria-current=step]")


def upload(page, f):
    page.set_input_files("#hcfile", str(f))
    page.wait_for_function("!document.querySelector('#hcnext').disabled")


def fill_meta(page):
    for k, v in META.items():
        page.fill(f"#hcm-{k}", v)


# ------------------------------------------------------------------ provider selector (Req 16.1)
def test_provider_selector_offers_openf1_default_and_csv_upload_without_csv_controls_in_openf1_view(page, tmp_path):
    page.goto(page.base + "/#/hist")
    page.wait_for_selector("#hprov")
    assert page.eval_on_selector_all("#hprov option", "os => os.map(o => o.textContent)") == ["OpenF1", "CSV Upload"]
    assert page.input_value("#hprov") == "openf1"
    assert page.is_visible("#hopenf1") and not page.is_visible("#hcsv")
    assert page.locator("#hcfile, #hcnext, #hcstepper").count() == 0  # the OpenF1 view has no CSV controls
    page.select_option("#hprov", "csv_upload")
    assert page.is_visible("#hcfile") and not page.is_visible("#hopenf1")
    assert "Upload" in stepper(page)
    page.select_option("#hprov", "openf1")
    assert page.is_visible("#hopenf1") and not page.is_visible("#hcsv")


# ------------------------------------------------------------------ step gating, retained values, error list (Req 16.4, 16.5, 16.7)
def test_steps_are_gated_values_are_retained_and_blocking_errors_are_listed_with_row_numbers(page, tmp_path):
    f = open_csv(page, tmp_path)
    assert [li.inner_text().split(". ", 1)[1] for li in page.locator("#hcstepper li").all()] == [
        "Upload", "Column mapping", "Units", "Metadata", "Validation & preview", "Import"]
    assert next_disabled(page) and page.is_disabled("#hcback")  # step 1: nothing uploaded yet
    upload(page, f)
    page.click("#hcnext")
    assert stepper(page).endswith("Column mapping") and not next_disabled(page)
    # a duplicate target field is reported and blocks the step; unmapping everything blocks it too
    page.select_option("select.hcmap[data-col=spd]", "session_time_s")
    assert next_disabled(page) and "one column only" in page.inner_text("#hcbody")
    page.select_option("select.hcmap[data-col=spd]", "speed_kph")
    assert not next_disabled(page)
    for c in ("t", "spd", "lap"):
        page.select_option(f"select.hcmap[data-col={c}]", "")
    assert next_disabled(page)
    page.select_option("select.hcmap[data-col=spd]", "speed_kph")
    page.select_option("select.hcmap[data-col=t]", "session_time_s")
    page.select_option("select.hcmap[data-col=lap]", "lap_number")
    page.click("#hcnext")
    assert stepper(page).endswith("Units") and not next_disabled(page)
    page.select_option("#hcu-speed", "mph")
    page.fill("#hctz", "+02:00")
    page.click("#hcnext")
    assert stepper(page).endswith("Metadata") and next_disabled(page)  # every field filled or marked not available
    fill_meta(page)
    page.fill("#hcm-circuit", "")
    assert next_disabled(page)
    page.check("[data-unav=circuit]")
    assert not next_disabled(page) and page.is_disabled("#hcm-circuit")
    page.uncheck("[data-unav=circuit]")
    page.fill("#hcm-circuit", "Yas Marina")
    assert not next_disabled(page)

    errors = [{"row": 7 + i, "driver": "VER", "column": "speed_kph", "reason": "not a number", "value": "abc"} for i in range(250)]
    page.mock.answers[("POST", "/api/historical/csv/validate")] = {"ok": False, "report": {"error_count": 250, "errors": errors}}
    page.click("#hcnext")
    page.wait_for_selector("#hcerrs")
    assert stepper(page).endswith("Validation & preview") and next_disabled(page)  # blocking errors keep Next disabled
    items = page.locator("#hcerrs li")
    assert items.count() == 200 and "250 blocking errors" in page.inner_text("#hcerrs") and "(showing 200)" in page.inner_text("#hcerrs")
    assert "row 7" in items.first.inner_text() and "speed_kph" in items.first.inner_text() and "not a number" in items.first.inner_text()
    sent = json.loads([c for c in page.mock.calls if c[1].endswith("/csv/validate")][-1][2])
    assert sent["mapping"] == {"session_time_s": "t", "speed_kph": "spd", "lap_number": "lap"} and sent["units"]["speed"] == "mph"
    assert sent["tz_offset"] == "+02:00" and sent["metadata"]["driver"] == "VER"

    # going back keeps everything the user entered
    page.click("#hcback")
    assert page.input_value("#hcm-event") == "Abu Dhabi" and page.input_value("#hcm-circuit") == "Yas Marina"
    page.click("#hcback")
    assert page.input_value("#hcu-speed") == "mph" and page.input_value("#hctz") == "+02:00"
    page.click("#hcback")
    assert page.input_value("select.hcmap[data-col=spd]") == "speed_kph" and page.input_value("select.hcmap[data-col=t]") == "session_time_s"
    # a clean validation enables Next, and the last step offers the import start
    page.mock.answers[("POST", "/api/historical/csv/validate")] = {"ok": True, "report": {"error_count": 0, "errors": []}}
    for _ in range(3):
        page.click("#hcnext")
    page.wait_for_selector("text=Validation passed")
    assert not next_disabled(page)
    page.click("#hcnext")
    assert stepper(page).endswith("Import") and page.is_visible("#hcstart")


# ------------------------------------------------------------------ session list: badges, empty state, load error (Req 13.9, 17.1-17.3)
def test_session_list_shows_provider_data_type_and_provenance_badge_on_every_row(page):
    page.mock.answers[("GET", "/api/historical/sessions")] = SESSIONS
    page.goto(page.base + "/#/sessions")
    page.wait_for_selector("tr[data-session]")
    rows = {r.get_attribute("data-session"): r for r in page.locator("tr[data-session]").all()}
    assert set(rows) == {"openf1-1", "csv-1"}
    assert rows["openf1-1"].locator(".provider-label").inner_text() == "OpenF1"
    assert rows["csv-1"].locator(".provider-label").inner_text() == "CSV Upload"
    assert rows["csv-1"].locator(".data-type-label").inner_text() == "Lap summaries"
    assert rows["openf1-1"].locator(".data-type-label").inner_text() == "Sampled telemetry"
    for sid, s in zip(("openf1-1", "csv-1"), SESSIONS):
        badge = rows[sid].locator(".provenance-badge")
        assert badge.text_content() == s["provenance_status"] and badge.get_attribute("data-provenance") == s["provenance_status"]
    assert "external" in rows["openf1-1"].locator(".provenance-badge").get_attribute("class")
    assert "imported" in rows["csv-1"].locator(".provenance-badge").get_attribute("class")


def test_session_list_empty_state_has_no_rows(page):
    page.mock.answers[("GET", "/api/historical/sessions")] = []
    page.goto(page.base + "/#/sessions")
    page.wait_for_selector("#hist-lib .empty")
    assert "No sessions imported yet" in page.inner_text("#hist-lib") and page.locator("tr[data-session]").count() == 0


def test_session_list_load_error_shows_message_and_retry_reloads(page):
    state = {"fail": True}
    page.mock.answers[("GET", "/api/historical/sessions")] = lambda: (500, {"detail": "database down"}) if state["fail"] else SESSIONS
    page.goto(page.base + "/#/sessions")
    page.wait_for_selector("#sessions-error")
    assert "failed" in page.inner_text("#sessions-error").lower() and page.locator("tr[data-session]").count() == 0
    state["fail"] = False
    page.click("#sessions-retry")
    page.wait_for_selector("tr[data-session]")
    assert page.locator("tr[data-session]").count() == 2 and page.locator("#sessions-error").count() == 0


# ------------------------------------------------------------------ Setup_Link form validation (Req 13.10, 15.x)
def test_setup_link_form_reports_each_missing_factor_and_sends_nothing_until_complete(page):
    sid = "csv-1"
    base = f"/api/historical/sessions/{sid}"
    page.mock.answers[("GET", base)] = SESSIONS[1]
    page.mock.answers[("GET", base + "/drivers")] = [{"code": "LEC", "name": "Charles Leclerc"}]
    page.mock.answers[("GET", "/api/experiments")] = []
    page.mock.answers[("GET", base + "/setup-link/options")] = {"laps": [1, 2], "metrics": [{"metric": "lap_time", "unit": "s", "available": True},
                                                                                               {"metric": "top_speed", "unit": "kph", "available": False}]}
    page.mock.answers[("POST", base + "/setup-link")] = {"measurements_created": 2, "experiment_id": "exp1", "verified": True}
    page.goto(page.base + f"/#/setup-link/{sid}")
    page.wait_for_selector("tr[data-factor]")
    assert page.locator(".provenance-badge").first.text_content() == SESSIONS[1]["provenance_status"]
    page.select_option("#sl-driver", "LEC")
    page.wait_for_selector("input[name=sl-metric]")
    assert page.is_disabled("input[name=sl-metric][value=top_speed]") and not page.is_disabled("input[name=sl-metric][value=lap_time]")
    n = page.locator("tr[data-factor]").count()
    assert n > 1
    page.click("#sl-send")
    page.wait_for_selector("#sl-failure")
    assert "Your entries are kept" in page.inner_text("#sl-failure")
    assert sum(1 for e in page.locator(".sl-ferr").all() if e.inner_text()) == n  # one error per factor
    assert not [c for c in page.mock.calls if c[0] == "POST" and c[1].endswith("/setup-link")]
    # a value without a verified/unverified statement is still an error for that factor only
    rows = page.locator("tr[data-factor]")
    for i in range(n):
        rows.nth(i).locator(".sl-value").fill("1")
    rows.nth(0).locator(".sl-verified").select_option("true")
    page.click("#sl-send")
    page.wait_for_selector("#sl-failure")
    assert sum(1 for e in page.locator(".sl-ferr").all() if e.inner_text()) == n - 1 and rows.nth(1).locator(".sl-value").input_value() == "1"
    for i in range(1, n):
        rows.nth(i).locator(".sl-verified").select_option("false")
    page.click("#sl-send")
    page.wait_for_selector("#sl-success")
    assert "2 measurements created" in page.inner_text("#sl-success") and page.get_attribute("#sl-analysis-link", "href") == "#/e/exp1/analysis"
