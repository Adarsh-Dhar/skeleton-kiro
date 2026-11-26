"""End-to-end browser test (Chromium via Playwright): a new user completes the whole WATTWISE workflow with the mouse only.

Screenshots of every screen are written to docs/screenshots/ (used by the README). Run ``playwright install chromium`` once first.
"""

from __future__ import annotations

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
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.base = live.url
        yield pg
        browser.close()


def fresh(page, hash_):
    """Hash-only navigation to the current hash does not re-render; force a clean page."""
    page.goto(page.base + "/")
    page.evaluate(f"location.hash = '{hash_}'")
    page.reload()


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def test_whole_workflow_without_touching_the_terminal(page):
    # 1. dashboard: empty state offers a demo experiment
    page.goto(page.base + "/")
    page.wait_for_selector("#kpis .kpi")
    page.wait_for_selector("#day svg.chart")
    assert "No experiments yet" in page.inner_text("#dash")
    shot(page, "1-dashboard")

    # 2. household setup: an invalid tariff is explained as you type, a valid change saves
    page.click("#nav a[data-nav=household]")
    page.wait_for_selector("#periods tr")
    page.fill("#periods tr:first-child [data-f=end]", "6")
    page.wait_for_selector("#tvalid .errors")
    assert "gaps" in page.inner_text("#tvalid")
    page.fill("#periods tr:first-child [data-f=end]", "7")
    page.wait_for_selector("#tvalid .note")
    shot(page, "2-household")
    page.click("#hsave")
    page.wait_for_selector("#toast.ok")

    # 3. schedule simulator: moving a slider re-prices the day
    page.click("#nav a[data-nav=simulator]")
    page.wait_for_selector("#out svg.chart")
    before = page.inner_text("#out .kpi >> nth=0")
    page.fill("#s-charge_start", "24")
    page.dispatch_event("#s-charge_start", "input")
    page.wait_for_function("(b) => document.querySelector('#out .kpi b').textContent !== b", arg=before.split("\n")[0])
    assert float(page.inner_text("#out .kpi >> nth=0").split("\n")[0]) < float(before.split("\n")[0])
    shot(page, "3-simulator")

    # 4. experiment designer: invalid input is rejected with a readable message, then a valid design is generated
    page.click("#nav a[data-nav=new]")
    page.wait_for_selector("#f")
    page.fill("[data-lo=charge_start]", "29")
    page.fill("[data-hi=charge_start]", "20")
    page.click("#f button[type=submit]")
    page.wait_for_selector("#err .errors")
    assert "low must be smaller" in page.inner_text("#err")
    shot(page, "4-designer-error")
    page.fill("[data-lo=charge_start]", "19")
    page.fill("[data-hi=charge_start]", "29")
    page.select_option("#method", "full_factorial")
    page.fill("#lat", "0")
    page.click("#f button[type=submit]")
    page.wait_for_selector("text=Combinations to measure")
    shot(page, "5-design")

    # 5. run it and watch progress
    page.click("#go")
    page.wait_for_selector("#runs-root table")
    page.wait_for_function("document.querySelectorAll('.chip.completed').length >= 27", timeout=60000)
    assert page.query_selector(".chip.failed") is None
    shot(page, "6-runs")
    page.click("[data-prof] >> nth=0")
    page.wait_for_selector(".modal svg.chart")
    page.keyboard.press("Escape")

    # 6. analysis
    page.click("#nav a[data-nav=analysis]")
    page.click("text=Open analysis")
    page.wait_for_selector(".reco h3")
    assert "Cheapest feasible schedule" in page.inner_text(".reco h3")
    page.wait_for_selector("text=What moves the cost")
    shot(page, "7-analysis")

    # 7. optimisation: suggest, then run the suggestion
    page.click("#nav a[data-nav=optimise]")
    page.click("text=Open optimisation")
    page.wait_for_selector("#best svg.chart")
    page.click("#suggest")
    page.wait_for_selector("#sug .panel")
    shot(page, "8-optimise")
    page.fill("#opt-n", "1")
    page.click("#apply")
    page.wait_for_selector("#runs-root table")
    page.wait_for_function("document.querySelectorAll('.chip.opt').length >= 1 && document.querySelectorAll('.chip.queued, .chip.running').length === 0", timeout=60000)

    # 8. reports
    page.click("#nav a[data-nav=reports]")
    page.click("text=Open reports")
    page.wait_for_selector("iframe")
    page.wait_for_selector("a:has-text('Schedules (CSV)')")
    shot(page, "9-report")
    with page.expect_download() as dl:
        page.click("a:has-text('Report (JSON)')")
    assert dl.value.suggested_filename.endswith("-report.json")

    # 9. meter import
    page.click("#nav a[data-nav=meter]")
    page.click("#d2")
    page.click("#prev")
    page.wait_for_selector("#res .kpis")
    assert "readings every 15 min" in page.inner_text("#res")
    shot(page, "10-meter")

    # 10. dashboard now lists the experiment with its saving
    page.click("#nav a[data-nav=dash]")
    page.wait_for_selector("tr.clickable")
    assert "%" in page.inner_text("tr.clickable")
    assert page.errors == [], page.errors


def test_user_text_is_escaped_in_the_browser(page):
    fresh(page, "#/new")
    page.wait_for_selector("#f")
    page.fill("#name", "<img src=x onerror=window.__xss=1>")
    page.select_option("#method", "ofat")
    page.fill("#lat", "0")
    page.click("#f button[type=submit]")
    page.wait_for_url("**/#/e/*/design")  # the hash changes before the design view has rendered
    page.wait_for_function("document.querySelector('h1') && document.querySelector('h1').innerText !== 'Experiment designer'")
    page.wait_for_selector("text=Combinations to measure")
    assert page.evaluate("window.__xss") is None
    assert "<img src=x" in page.inner_text("h1")  # shown literally, not interpreted
    assert page.errors == [], page.errors


def test_my_usage_library_baseline_and_comparison(page):
    """Historical meter library (K2/K3), measured baseline (K4) and actual-vs-optimised comparison (K6) in the browser."""
    fresh(page, "#/usage")
    page.wait_for_selector("#u-demo")
    page.click("#u-demo")
    page.wait_for_function("document.querySelector('#u-csv').value.length > 100")
    page.click("#u-prev")
    page.wait_for_selector("text=How this file will be read")
    res = page.inner_text("#u-res")
    assert "power_kw" in res and "96 / 96" in res and "Converted readings" in res
    page.click("#u-import")
    page.wait_for_selector("[data-open]")
    page.wait_for_selector("#u-base")
    assert "mean daily energy" in page.inner_text("#u-detail").lower()
    # the bundled demo file is labelled SYNTHETIC DEMO; only a file the user labels "real meter" reads MEASURED
    assert "SYNTHETIC DEMO" in page.inner_text("#u-detail h2") and "MEASURED" not in page.inner_text("#u-detail h2")
    page.click("#u-base")  # use as household baseline (stored on the server)
    page.wait_for_selector("text=Stop using as baseline")
    hh = page.evaluate("fetch('/api/household').then(r => r.json())")
    assert hh["saved"] and hh["settings"]["baseline_import_id"]
    page.click("#u-cmp")
    page.wait_for_selector("text=Estimated savings")
    out = page.inner_text("#u-cmpres")
    assert "PROJECTED" in out and "MEASURED" in out
    shot(page, "11-my-usage")
    assert page.errors == [], page.errors


def test_tariff_editor_keeps_and_edits_advanced_fields(page):
    """Saving from the Household setup screen must never drop fixed charges, tiers, taxes or seasonal periods."""
    body = {"name": "Generic slab example", "demand_charge_per_kw": 0.0, "fixed_monthly_charge": 9.0, "billing_days": 30, "tax_pct": 5,
            "tiers": [{"up_to_kwh": 10, "adder_per_kwh": 0}, {"up_to_kwh": None, "adder_per_kwh": 0.05}], "tier_basis": "daily",
            "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.2}]}
    fresh(page, "#/")
    page.wait_for_selector("#kpis")
    t = page.evaluate("b => fetch('/api/tariffs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)}).then(r => r.json())", body)
    page.evaluate("id => fetch('/api/household').then(r => r.json()).then(s => fetch('/api/household', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...s.settings, tariff_id: id})}))", t["id"])
    fresh(page, "#/household")
    page.wait_for_selector("#tadv[open]")  # opens by itself because the tariff has advanced charges
    assert page.input_value("#tfixm") == "9" and page.input_value("#ttax") == "5" and len(page.query_selector_all("#tiers tr")) == 2
    page.fill("#ttax", "7.5")
    page.fill("#tfixd", "0.25")
    page.click("#tsave")
    page.wait_for_selector("#toast:not([hidden])")
    saved = page.evaluate("id => fetch('/api/tariffs').then(r => r.json()).then(ts => ts.find(x => x.id === id))", t["id"])
    assert saved["tax_pct"] == 7.5 and saved["fixed_daily_charge"] == 0.25 and saved["fixed_monthly_charge"] == 9.0
    assert saved["tiers"] == body["tiers"] and saved["billing_days"] == 30
    assert page.errors == [], page.errors
def test_tariff_editor_sets_individual_weekdays(page):
    """Ticking weekday boxes in the period table saves ``weekdays`` and reloads them ticked."""
    wk = [{"label": "weekdays", "start": 0, "end": 24, "price": 0.3, "weekdays": [0, 1, 2, 3, 4]}, {"label": "weekend", "start": 0, "end": 24, "price": 0.1, "weekdays": [5, 6]}]
    body = {"name": "Weekday boxes example", "demand_charge_per_kw": 0.0, "periods": wk, "simulation_day": {"month": 6, "day_type": "wed"}}
    fresh(page, "#/")
    page.wait_for_selector("#kpis")
    t = page.evaluate("b => fetch('/api/tariffs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)}).then(r => r.json())", body)
    page.evaluate("id => fetch('/api/household').then(r => r.json()).then(s => fetch('/api/household', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...s.settings, tariff_id: id})}))", t["id"])
    fresh(page, "#/household")
    page.wait_for_selector("#tadv[open]")
    assert len(page.query_selector_all("#periods tr:first-child [data-wd]:checked")) == 5
    assert page.input_value("#tsimd") == "wed"
    page.uncheck("#periods tr:first-child [data-wd='4']")  # Friday moves to ... nowhere: now a gap on Friday
    page.wait_for_selector("#tvalid:has-text('gaps')")
    page.check("#periods tr:first-child [data-wd='4']")
    page.click("#tsave")
    page.wait_for_selector("#toast:not([hidden])")
    saved = page.evaluate("id => fetch('/api/tariffs').then(r => r.json()).then(ts => ts.find(x => x.id === id))", t["id"])
    assert saved["periods"][0]["weekdays"] == [0, 1, 2, 3, 4] and saved["periods"][1]["weekdays"] == [5, 6]
    assert page.errors == [], page.errors
def _clear_meter_library(page):
    """The library rejects a file that was already imported with the same options, so each test starts from an empty library."""
    page.evaluate("""async () => {
        const imports = await fetch('/api/meter/imports').then(r => r.json());
        for (const m of imports) await fetch('/api/meter/imports/' + m.import_id, {method: 'DELETE'});
    }""")
def _import_with_label(page, value):
    """Fill the demo CSV, pick a provenance in the select, preview, import; return (preview chips, detail chips)."""
    _clear_meter_library(page)
    fresh(page, "#/usage")
    page.wait_for_selector("#u-demo")
    page.click("#u-demo")
    page.wait_for_function("document.querySelector('#u-csv').value.length > 100")
    page.select_option("#u-prov", value)  # overrides what the demo button set
    page.click("#u-prev")
    page.wait_for_selector("#u-res h2 .chip")
    preview = page.eval_on_selector_all("#u-res h2 .chip", "els => els.map(e => [e.textContent, e.className])")
    page.click("#u-import")
    page.wait_for_selector("#u-detail h2 .chip")
    detail = page.eval_on_selector_all("#u-detail h2 .chip", "els => els.map(e => [e.textContent, e.className])")
    return preview, detail
@pytest.mark.parametrize("value,text,css", [("real_meter", "MEASURED", "prov-m"), ("synthetic_demo", "SYNTHETIC DEMO", "prov-s"), ("unknown", "UNVERIFIED SOURCE", "prov-u"),
                                            ("", "UNVERIFIED SOURCE", "prov-u")])
def test_provenance_badge_matches_label_on_preview_and_detail(page, value, text, css):
    """Exactly one badge per screen, with the label's text and class; only a real_meter file reads MEASURED (3.4 to 3.6)."""
    preview, detail = _import_with_label(page, value)
    for chips in (preview, detail):
        assert len(chips) == 1, chips
        assert chips[0][0] == text and css in chips[0][1].split(), chips
    if text != "MEASURED":
        assert "MEASURED" not in page.inner_text("#u-detail h2")
    assert page.errors == [], page.errors
def test_demo_button_selects_synthetic_demo_and_imports_with_that_label(page):
    _clear_meter_library(page)
    fresh(page, "#/usage")
    page.wait_for_selector("#u-demo")
    page.click("#u-demo")
    page.wait_for_function("document.querySelector('#u-csv').value.length > 100")
    assert page.input_value("#u-prov") == "synthetic_demo"
    page.click("#u-prev")
    page.wait_for_selector("#u-res h2 .chip.prov-s")
    page.click("#u-import")
    page.wait_for_selector("#u-detail h2 .chip.prov-s")
    labels = page.evaluate("fetch('/api/meter/imports').then(r => r.json()).then(l => Promise.all(l.map(m => fetch('/api/meter/imports/' + m.import_id + '/summary').then(r => r.json()))))")
    assert labels and labels[0]["source_label"] == "synthetic_demo", [x["source_label"] for x in labels]
    assert page.errors == [], page.errors
