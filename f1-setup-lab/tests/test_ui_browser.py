"""End-to-end browser test (Chromium via Playwright): a new user completes the whole F1 workflow with the mouse only.

Screenshots of every screen are written to docs/screenshots/ (used by the README).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
SHOTS = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


@pytest.fixture(scope="module")
def page(live):
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.errors = []
        # the browser logs every 4xx response as a console error; those are intentional in the validation tests
        pg.on(
            "console",
            lambda m: pg.errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None,
        )
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.base = live.url
        yield pg
        browser.close()


def fresh(page, hash_):
    """Hash-only navigation to the current hash does not re-render; force a clean page."""
    page.goto(page.base + "/")
    page.evaluate(f"location.hash = '{hash_}'")
    page.reload()


def shot(page, name, full=True):
    page.evaluate("document.querySelector('#toast').hidden = true")
    page.wait_for_timeout(250)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)


def test_full_workflow(page):
    page.goto(page.base)
    assert "SYNTHETIC DATA" in page.inner_text(".synthetic")
    page.wait_for_selector("text=No experiments yet")
    shot(page, "01-dashboard-empty")

    # ---- experiment creator -------------------------------------------------------------------
    page.click("text=Configure my own")
    page.wait_for_selector("#f")
    page.fill("#name", "Wing & suspension screening")
    page.fill("#lat", "0.15")
    shot(page, "02-experiment-creator")
    page.click("text=Generate design")

    # ---- setup design ---------------------------------------------------------------------------
    page.wait_for_selector("text=Generated setups")
    assert page.locator("tbody tr").count() >= 20
    body = page.inner_text("main").lower()
    assert "i=abcd" in body and "res iv" in body and "alias structure" in body
    shot(page, "03-setup-design")

    # ---- run monitor ----------------------------------------------------------------------------
    page.click("#go")
    page.wait_for_selector("text=Run monitor")
    page.wait_for_selector(".kpi:has-text('completed') >> text=/^[1-9]/", timeout=20000)
    shot(page, "04a-run-monitor-in-progress", full=False)
    page.wait_for_function(
        "document.querySelector('.kpis .kpi b') && document.querySelector('.kpis .kpi b').textContent.trim()==='20'", timeout=60000
    )
    assert page.locator("tbody tr").count() == 20
    assert page.locator(".chip.completed").count() >= 20
    shot(page, "04-run-monitor")

    # ---- telemetry ------------------------------------------------------------------------------
    page.click(".tabs >> text=Telemetry")
    page.wait_for_selector("#runsel")
    page.wait_for_selector("svg.chart")
    assert "synthetic" in page.inner_text("#tel").lower()
    shot(page, "05-telemetry")

    # ---- analysis -------------------------------------------------------------------------------
    page.click(".tabs >> text=Analysis")
    page.wait_for_selector(".reco")
    assert "recommended setup" in page.inner_text(".reco").lower()
    assert page.locator('h2:has-text("ANOVA (Type II)")').count() == 1
    assert page.locator("svg.chart").count() >= 3
    assert "never recommended" in page.inner_text(".reco").lower()
    shot(page, "06-analysis")

    # ---- optimisation ---------------------------------------------------------------------------
    page.click(".tabs >> text=Optimisation")
    page.wait_for_selector("#oprev")
    page.click("#oprev")
    page.wait_for_selector("text=Recommended setups (preview)")
    assert "model prediction" in page.inner_text("#prev").lower()
    shot(page, "07-optimisation-preview")
    page.click("#oapply")
    page.wait_for_selector("text=Optimiser runs")
    page.wait_for_function(
        "[...document.querySelectorAll('#opt-root tbody tr')].length>=2 && document.querySelectorAll('#opt-root .chip.completed').length>=2",
        timeout=60000,
    )
    shot(page, "08-optimisation-result")

    # ---- report ---------------------------------------------------------------------------------
    page.click(".tabs >> text=Report")
    page.wait_for_selector("text=Printable report")
    assert "reproducibility" in page.inner_text("main").lower()
    href = page.get_attribute('a:has-text("Printable report")', "href")
    html = page.request.get(page.base + href).text()
    assert "SYNTHETIC DATA" in html and "Recommendation" in html and "ANOVA" in html
    csv = page.request.get(page.base + href.replace("report.html", "export/runs.csv")).text()
    assert csv.splitlines()[0].startswith("run_id,order,status")
    shot(page, "09-report")

    # ---- dashboard shows the experiment ----------------------------------------------------------
    page.click("nav >> text=Dashboard")
    page.wait_for_selector("#dash table")
    assert "Wing & suspension screening" in page.inner_text("#dash")
    assert re.search(r"\d\d\.\d{3} s", page.inner_text("#dash"))
    shot(page, "10-dashboard")
    assert page.errors == []


def test_invalid_configuration_is_explained_in_the_ui(page):
    fresh(page, "#/new")
    page.wait_for_selector("#f")
    page.fill('[data-lo="front_wing"]', "40")
    page.fill('[data-hi="front_wing"]', "10")
    page.click("text=Generate design")
    page.wait_for_selector(".errors")
    assert "low must be smaller than high" in page.inner_text(".errors")
    page.fill('[data-lo="front_wing"]', "10")
    page.fill('[data-hi="front_wing"]', "40")
    page.fill("#budget", "6")
    page.click("text=Generate design")
    page.wait_for_selector(".errors:has-text('budget')")  # the previous error box is still on screen until the new response arrives
    assert "budget" in page.inner_text(".errors").lower()
    shot(page, "11-validation-error", full=False)
    assert page.errors == []


def test_failed_runs_are_visible_and_can_be_retried(page):
    fresh(page, "#/new")
    page.wait_for_selector("#f")
    page.fill("#name", "Fault demo")
    page.fill("#fault", "1")
    page.fill("#retries", "0")
    page.fill("#lat", "0")
    page.select_option("#method", "ofat")
    page.click("text=Generate design")
    page.wait_for_selector("#go")
    page.click("#go")
    page.wait_for_selector(".kpi.bad", timeout=20000)
    page.wait_for_function("document.querySelector('#retry') && !document.querySelector('#retry').disabled", timeout=20000)
    assert page.locator(".chip.failed").count() >= 1
    assert "injected fault" in page.inner_text("main").lower()
    shot(page, "12-failed-runs")
    page.click("#retry")
    page.wait_for_function(
        "document.querySelectorAll('.chip.failed').length===0 && document.querySelectorAll('.chip.completed').length>=3", timeout=30000
    )
    assert page.errors == []


def test_standalone_telemetry_import_flags_outlier_and_converts_units(page):
    fresh(page, "#/telemetry")
    page.wait_for_selector("#s-demo2")
    page.click("#s-demo2")
    page.click("#s-prev")
    page.wait_for_selector("text=flagged laps")
    assert "converted from milliseconds" in page.inner_text("main").lower()
    assert page.locator("tr.outlier").count() == 1
    shot(page, "13-telemetry-import")
    page.fill("#s-csv", "lap,sector_1,sector_2\n1,27,31\n")
    page.click("#s-prev")
    page.wait_for_selector("text=missing required column")
    assert "sector_3" in page.inner_text("main")
    assert page.errors == []


def test_stint_telemetry_inspector(page, live):
    """Saved stint replay/inspection (F1-K2) on a live experiment whose two stints have archived decoded telemetry."""
    import json
    import urllib.request

    from test_live_mode import live_body

    from db import create_db_engine
    from f1lab.integrations.game_udp.evidence import save_telemetry_series

    req = urllib.request.Request(live.url + "/api/experiments", data=json.dumps(live_body(name="inspector test")).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    d = json.loads(urllib.request.urlopen(req).read())
    engine = create_db_engine(live.db_path)

    def stint(speed0, n, with_tyres):
        return [{"session_time": 100 + i * 0.05, "frame_id": i, "packet_format": 2025, "packet_id": 6, "session_uid": 1, "speed_kph": speed0 + (i % 200),
                 "throttle": (i % 40) / 40, "steer": 0.1, "brake": 0.0 if i % 50 else 1.0, "gear": 6, "rpm": 11000.0, "clutch": None, "drs": 0,
                 "rev_lights_percent": None, "engine_temperature_c": None, "brake_temperature_c": [400, 400, 410, 410],
                 "tyre_surface_temperature_c": [90, 91, 92, 93] if with_tyres else [], "tyre_inner_temperature_c": [],
                 "tyre_pressure_psi": [22.0, 22.0, 23.0, 23.0], "surface_type": [0, 0, 0, 0]} for i in range(n)]
    r1, r2 = d["runs"][0]["run_id"], d["runs"][1]["run_id"]
    save_telemetry_series(engine, d["id"], r1, stint(120, 1500, True))
    save_telemetry_series(engine, d["id"], r2, stint(110, 900, False))
    fresh(page, f"#/e/{d['id']}/telemetry")
    page.wait_for_selector("#tel-run")
    page.click("#tel-series")
    page.wait_for_selector("svg.insp")
    stat = page.inner_text("#istat")
    assert "1500 measured samples" in stat and "downsampled" in stat
    assert "Clutch" in page.inner_text(".note") and "never shown as zero" in page.inner_text(".note")
    assert page.is_disabled('[data-ch="clutch"]') and not page.is_disabled('[data-ch="rpm"]')
    page.check('[data-ch="rpm"]')
    page.wait_for_function("document.querySelectorAll('svg.insp').length === 5")
    page.query_selector("svg.insp").scroll_into_view_if_needed()
    box = page.query_selector("svg.insp .insp-hit").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_selector("#iread table")
    assert "measured sample at" in page.inner_text("#iread").lower() and "frame" in page.inner_text("#iread").lower()
    # drag to zoom: the window shrinks
    page.mouse.move(box["x"] + box["width"] * 0.25, box["y"] + 20)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + 20)
    page.mouse.up()
    page.wait_for_function("!document.querySelector('#istat').innerText.includes('1500 measured samples in window')")
    assert float(page.input_value("#ia1")) - float(page.input_value("#ia0")) < 75 * 0.5
    page.click("#iplay")
    page.wait_for_timeout(400)
    page.click("#iplay")
    # comparison against derived distance
    page.select_option("#tel-cmp", r2)
    page.click("#tel-series")
    page.wait_for_selector("text=derived")
    assert "900" in page.inner_text("#istat") and "(B only)" in page.inner_text(".note")
    with page.expect_download() as dl:
        page.click("a:has-text('CSV, run 1')")
    assert dl.value.suggested_filename.endswith("-telemetry.csv")
    shot(page, "14-stint-inspector", full=False)
    assert page.errors == [], page.errors
