"""End-to-end browser test (Chromium via Playwright): from a FRESH installation (no example data at all) a user configures their
own household with sources and completes the whole WATTWISE workflow with the mouse only.

The values typed in below are test inputs, not real data. Screenshots of every screen are written to docs/screenshots/ (used by
the README). Run ``playwright install chromium`` once first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from inputs import meter_csv

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
        pg.on("dialog", lambda d: d.accept())
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


def configure_household(page):
    """Enter a tariff, an EV, an appliance and the always-on load through the Household setup form (test inputs)."""
    page.click("#nav a[data-nav=household]")
    page.wait_for_selector("#tname")
    assert "Configuration required" in page.inner_text("#ready")
    page.fill("#tname", "Test bill tariff")
    page.fill("#tcur", "INR")
    page.click("#tsrc > summary")
    page.fill("#tsrc-doc", "bill for test month")
    page.fill("#tsrc-col", "2026-11-01")
    # an incomplete price table is explained as you type, a valid one is confirmed
    page.fill("#periods tr:first-child [data-f=start]", "23")
    page.fill("#periods tr:first-child [data-f=end]", "7")
    page.fill("#periods tr:first-child [data-f=price]", "4")
    page.wait_for_selector("#tvalid .errors")
    assert "gaps" in page.inner_text("#tvalid")
    page.click("#addp")
    page.fill("#periods tr:nth-child(2) [data-f=start]", "7")
    page.fill("#periods tr:nth-child(2) [data-f=end]", "23")
    page.fill("#periods tr:nth-child(2) [data-f=price]", "8")
    page.wait_for_selector("#tvalid .note.info")
    page.click("#tnew")
    page.wait_for_selector("#toast.ok")
    page.wait_for_selector("#tsave")  # the saved tariff is now selected
    # EV, charger, one movable appliance, the always-on load
    page.uncheck("#noev")
    page.fill("#ev-bat", "40")
    page.fill("#ev-eff", "0.9")
    page.fill("#ev-init", "30")
    page.fill("#ev-want", "80")
    page.fill("#ev-plug", "18:30")
    page.fill("#ev-dep", "07:30")
    page.fill("#ev-charger", "7.2")
    page.click("#adda")
    page.fill("#apps tr[data-a='0'] [data-f=name]", "Washing machine")
    page.fill("#apps tr[data-a='0'] [data-f=power_kw]", "0.5")
    page.fill("#apps tr[data-a='0'] [data-f=duration_h]", "1.5")
    page.fill("#apps tr[data-a='0'] [data-f=fixed_start]", "19:00")
    page.fill("#base", "0.3")
    assert not page.is_hidden("#hdirty")  # unsaved edits are announced
    page.click("#hsave")
    page.wait_for_selector("#toast.ok")
    page.wait_for_selector("#ready .note.info")
    assert "Ready" in page.inner_text("#ready")
    page.wait_for_selector("#audit .note.info")  # the data audit has nothing to report
    assert "no findings" in page.inner_text("#audit")
def test_fresh_installation_has_no_example_data(page):
    page.goto(page.base + "/")
    page.wait_for_selector("#day .empty")
    assert "Configuration required" in page.inner_text("#day") and "No experiments yet" in page.inner_text("#dash")
    assert page.query_selector("#demo") is None and page.query_selector("#mine") is None  # no demo experiment
    assert page.evaluate("fetch('/api/tariffs').then(r => r.json())") == []
    hh = page.evaluate("fetch('/api/household').then(r => r.json())")
    assert hh["saved"] is False and hh["settings"]["appliances"] == [] and hh["settings"]["ev"] is None
    for where in ("#/simulator", "#/new"):
        fresh(page, where)
        page.wait_for_selector("text=Configuration required")
    shot(page, "1-dashboard")
    assert page.errors == [], page.errors


def test_whole_workflow_without_touching_the_terminal(page):
    # 1. household setup from nothing: every value typed in, with its source
    page.goto(page.base + "/")
    page.wait_for_selector("#kpis")
    configure_household(page)
    shot(page, "2-household")
    hh = page.evaluate("fetch('/api/household').then(r => r.json())")
    s = hh["settings"]
    assert s["ev"]["plug_in_hour"] == 18.5 and s["ev"]["source"]["verification"] == "user_supplied" and s["charger_power_kw"] == 7.2
    assert s["appliances"][0]["source"]["source_name"] == "household owner" and s["base_load_source"]["source_type"] == "user_provided"
    t = page.evaluate("fetch('/api/tariffs').then(r => r.json())")[0]
    assert t["source"]["document"] == "bill for test month" and t["currency"] == "INR"

    # 2. dashboard: today's projected day
    page.click("#nav a[data-nav=dash]")
    page.wait_for_selector("#day svg.chart")
    assert "Projected from your inputs" in page.inner_text("#day")

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
    assert page.input_value("#cap") == "7.2" and page.input_value("[data-hi=charge_power_kw]") == "7.2"
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
    page.wait_for_selector("#go")
    assert "Inputs and their sources" in page.inner_text("#view") and "USER SUPPLIED" in page.inner_text("#view")
    shot(page, "5-design")

    # 5. run it and watch progress
    page.click("#go")
    page.wait_for_selector("#runs-root table")
    page.wait_for_function("document.querySelectorAll('.chip.completed').length >= 27", timeout=60000)
    assert page.query_selector(".chip.failed") is None
    shot(page, "6-runs")
    page.click("[data-prof] >> nth=0")
    page.wait_for_selector(".modal svg.chart")
    assert "PROJECTION" in page.inner_text(".modal")
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

    # 8. reports: the inputs and their sources are part of the report
    page.click("#nav a[data-nav=reports]")
    page.click("text=Open reports")
    page.wait_for_selector("iframe")
    page.wait_for_selector("a:has-text('Schedules (CSV)')")
    html = page.evaluate("fetch(document.querySelector('iframe').src).then(r => r.text())")
    assert "Inputs (your data and cited sources)" in html and "bill for test month" in html and "PROJECTED RESULTS" in html
    shot(page, "9-report")
    with page.expect_download() as dl:
        page.click("a:has-text('Report (JSON)')")
    assert dl.value.suggested_filename.endswith("-report.json")

    # 9. meter preview: the user's own export (here a test file), no demo button
    page.click("#nav a[data-nav=meter]")
    assert page.query_selector("#d1") is None and page.query_selector("#d2") is None
    page.fill("#csv", meter_csv())
    page.click("#prev")
    page.wait_for_selector("#res .kpis")
    assert "readings every 15 min" in page.inner_text("#res")
    shot(page, "10-meter")

    # 10. dashboard now lists the experiment with its saving
    page.click("#nav a[data-nav=dash]")
    page.wait_for_selector("tr.clickable")
    assert "%" in page.inner_text("tr.clickable")
    assert page.errors == [], page.errors


def test_clear_and_restore_the_household(page):
    fresh(page, "#/household")
    page.wait_for_selector("#hreset")
    page.click("#hreset")
    page.wait_for_selector("#toast.ok")
    page.wait_for_selector("#ready .errors")
    assert page.input_value("#base") == "" and page.is_checked("#noev")
    page.click("#hrestore")
    page.wait_for_selector("#ready .note.info")
    assert page.input_value("#base") == "0.3" and not page.is_checked("#noev") and page.input_value("#ev-charger") == "7.2"
    assert page.errors == [], page.errors


def test_legacy_values_need_an_explicit_confirmation(page):
    """A value saved before source tracking stays marked "to confirm" until the owner ticks that they checked it."""
    fresh(page, "#/")
    page.wait_for_selector("#kpis")
    page.evaluate("""() => fetch('/api/household').then(r => r.json()).then(s => {
        const a = s.settings.appliances.map((x) => ({...x, source: {...x.source, legacy: true}}));
        return fetch('/api/household', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...s.settings, appliances: a})}); })""")
    fresh(page, "#/household")
    page.wait_for_selector("#audit details")
    assert "TO CONFIRM" in page.inner_text("#audit")
    page.click("#hsave")  # saving without ticking the box keeps the mark
    page.wait_for_selector("#toast.ok")
    hh = page.evaluate("fetch('/api/household').then(r => r.json())")
    assert hh["settings"]["appliances"][0]["source"]["legacy"] is True
    page.check("#asrc-0-ok")
    page.click("#hsave")
    page.wait_for_selector("#toast.ok")
    hh = page.evaluate("fetch('/api/household').then(r => r.json())")
    assert hh["settings"]["appliances"][0]["source"]["legacy"] is False
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
    """Historical meter library, measured baseline and actual-vs-optimised comparison in the browser."""
    _clear_meter_library(page)
    fresh(page, "#/usage")
    page.wait_for_selector("#u-csv")
    assert page.query_selector("#u-demo") is None  # no demo data
    page.fill("#u-csv", meter_csv())
    page.select_option("#u-prov", "real_meter")
    page.fill("#u-src", "test meter export")
    page.click("#u-prev")
    page.wait_for_selector("text=How this file will be read")
    res = page.inner_text("#u-res")
    assert "power_kw" in res and "96 / 96" in res and "Converted readings" in res
    page.click("#u-import")
    page.wait_for_selector("[data-open]")
    page.wait_for_selector("#u-base")
    assert "mean daily energy" in page.inner_text("#u-detail").lower()
    assert "METER EXPORT · DECLARED BY YOU" in page.inner_text("#u-detail h2")  # measured by the meter, supplied by the owner: not "verified"
    page.click("#u-base")  # use as household baseline (stored on the server)
    page.wait_for_selector("text=Stop using as baseline")
    hh = page.evaluate("fetch('/api/household').then(r => r.json())")
    assert hh["saved"] and hh["settings"]["baseline_import_id"]
    page.click("#u-cmp")
    page.wait_for_selector("text=Estimated savings")
    out = page.inner_text("#u-cmpres")
    assert "PROJECTED" in out and "MEASURED" in out
    # bill reconciliation: the test file covers only part of 3 March, so it must NOT reconcile (coverage is reported)
    page.fill("#r-from", "2026-03-03")
    page.fill("#r-to", "2026-03-03")
    page.fill("#r-kwh", "10")
    page.fill("#r-amt", "100")
    page.click("#r-go")
    page.wait_for_selector("#r-res .errors")
    assert "Does not reconcile" in page.inner_text("#r-res") and "cover" in page.inner_text("#r-res")
    shot(page, "11-my-usage")
    page.click("#u-base")  # stop using it again, so later tests use the always-on load
    page.wait_for_selector("text=Use as my household baseline")
    assert page.errors == [], page.errors


def test_tariff_editor_keeps_and_edits_advanced_fields(page):
    """Saving from the Household setup screen must never drop fixed charges, tiers, taxes or seasonal periods."""
    src = {"source_type": "user_provided", "source_name": "automated test fixture", "verification": "user_supplied"}
    body = {"name": "Slab test tariff", "source": src, "demand_charge_per_kw": 0.0, "fixed_monthly_charge": 9.0, "billing_days": 30, "tax_pct": 5,
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
    assert saved["tiers"] == body["tiers"] and saved["billing_days"] == 30 and saved["source"]["source_name"] == "automated test fixture"
    assert page.errors == [], page.errors


def test_tariff_editor_sets_individual_weekdays(page):
    """Ticking weekday boxes in the period table saves ``weekdays`` and reloads them ticked."""
    src = {"source_type": "user_provided", "source_name": "automated test fixture", "verification": "user_supplied"}
    wk = [{"label": "weekdays", "start": 0, "end": 24, "price": 0.3, "weekdays": [0, 1, 2, 3, 4]}, {"label": "weekend", "start": 0, "end": 24, "price": 0.1, "weekdays": [5, 6]}]
    body = {"name": "Weekday boxes test", "source": src, "demand_charge_per_kw": 0.0, "periods": wk, "simulation_day": {"month": 6, "day_type": "wed"}}
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
    page.goto(page.base + "/")
    page.evaluate("""async () => {
        const imports = await fetch('/api/meter/imports').then(r => r.json());
        for (const m of imports) await fetch('/api/meter/imports/' + m.import_id, {method: 'DELETE'});
    }""")


def _import_with_label(page, value):
    """Paste the test meter file, pick a provenance in the select, preview, import; return (preview chips, detail chips)."""
    _clear_meter_library(page)
    fresh(page, "#/usage")
    page.wait_for_selector("#u-csv")
    page.fill("#u-csv", meter_csv())
    page.fill("#u-src", "test meter export")
    page.select_option("#u-prov", value)
    page.click("#u-prev")
    page.wait_for_selector("#u-res h2 .chip")
    preview = page.eval_on_selector_all("#u-res h2 .chip", "els => els.map(e => [e.textContent, e.className])")
    page.click("#u-import")
    page.wait_for_selector("#u-detail h2 .chip")
    detail = page.eval_on_selector_all("#u-detail h2 .chip", "els => els.map(e => [e.textContent, e.className])")
    return preview, detail


@pytest.mark.parametrize("value,text,css", [("real_meter", "METER EXPORT · DECLARED BY YOU", "prov-m"), ("unknown", "UNVERIFIED SOURCE", "prov-u")])
def test_provenance_badge_matches_label_on_preview_and_detail(page, value, text, css):
    """Exactly one badge per screen, with the label's text and class; only a real_meter file reads MEASURED."""
    preview, detail = _import_with_label(page, value)
    for chips in (preview, detail):
        assert len(chips) == 1, chips
        assert chips[0][0] == text and css in chips[0][1].split(), chips
    if not text.startswith("METER EXPORT"):
        assert "MEASURED" not in page.inner_text("#u-detail h2")
    assert page.errors == [], page.errors


def test_synthetic_files_cannot_be_imported_from_the_ui(page):
    fresh(page, "#/usage")
    page.wait_for_selector("#u-prov")
    options = page.eval_on_selector_all("#u-prov option", "os => os.map(o => o.value)")
    assert "synthetic_demo" not in options and options == ["real_meter", "unknown"]
    assert page.errors == [], page.errors
