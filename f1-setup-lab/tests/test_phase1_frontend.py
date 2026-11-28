"""Phase 1 frontend tests (requirement 9.4): static checks of the scripts and page, the shared compass table run through node,
and browser tests of the new views. The browser tests serve the real frontend from the app and answer ``/api/historical/**``
with fixtures, so each scenario (failed dataset, null cells, qualifying session) is exact. No network is used."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
import uvicorn
from openf1_fakes import FakeOpenF1, fake_client

from f1lab.api.app import create_app

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
CASES = Path(__file__).parent / "fixtures" / "compass_cases.json"
SCRIPTS = sorted(FRONTEND.glob("*.js"))
SID = "openf1-2025-abu-dhabi-grand-prix-r"
SID2 = "openf1-2025-bahrain-grand-prix-r"
HEADSHOT = "https://media.test/ver.png"


# ------------------------------------------------------------------------------------------------ static
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_passes_node_check(script):
    r = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_nav_is_dashboard_experiments_historical_dropdown_and_telemetry_import():
    html = (FRONTEND / "index.html").read_text()
    nav = re.search(r'<nav id="nav"[^>]*>(.*?)</nav>', html, re.S).group(1)
    groups = re.findall(r'<div class="nav-group">(.*?)</div></div>', nav, re.S)
    assert len(groups) == 1 and re.sub(r"<[^>]+>", "", re.search(r"<h2[^>]*>(.*?)</h2>", groups[0], re.S).group(1)) == "Historical F1"
    assert len(re.findall(r"<a ", groups[0])) == 4
    flat = re.sub(r'<div class="nav-group">.*?</div></div>', "<GROUP>", nav, flags=re.S)
    assert re.findall(r'data-nav="(\w+)"|(<GROUP>)', flat) == [("dashboard", ""), ("experiments", ""), ("", "<GROUP>"), ("telemetry", "")]
    assert re.findall(r'data-nav="(\w+)"', nav) == ["dashboard", "experiments", "hist", "sessions", "standings", "season", "telemetry"]
def test_scripts_load_in_dependency_order():
    order = re.findall(r'<script src="([^"]+)"', (FRONTEND / "index.html").read_text())
    assert order.index("charts.js") < order.index("hist_analysis.js") < order.index("app.js")
    assert order.index("hist_race.js") < order.index("app.js") and order[-1] == "app.js"
    assert len(order) == len(set(order))


def test_frontend_names_no_external_host():
    """The only external address the page uses is a driver headshot URL taken from stored data at run time."""
    for f in [*SCRIPTS, FRONTEND / "index.html", FRONTEND / "style.css"]:
        hosts = set(re.findall(r"https?://([\w.-]+)", f.read_text()))
        assert hosts <= {"www.w3.org"}, (f.name, hosts)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_compass_matches_the_shared_case_table():
    cases = json.loads(CASES.read_text())["cases"]
    prog = (f"const H = require({json.dumps(str(FRONTEND / 'hist_analysis.js'))});"
            f"const cases = JSON.parse({json.dumps(CASES.read_text())}).cases;"
            "console.log(JSON.stringify(cases.map(c => H.compass(c.deg))));")
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got == [c["compass"] for c in cases]


# ------------------------------------------------------------------------------------------------ browser
pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase1-ui")
    fake = FakeOpenF1()
    app = create_app(str(root / "trialforge.db"), openf1_client_factory=lambda: fake_client(root / "openf1-cache", fake)[0])
    import pandas as pd
    app.state.historical.repo.save_session(
        "exp1", {"session_id": "exp1", "provider": "openf1", "source_type": "external_provider", "verification": "verified", "year": 2024, "event": "Export GP", "session": "Race", "session_type": "R",
                 "session_date": "2024-03-01", "drivers": [{"code": "VER", "number": "1", "name": "Max VERSTAPPEN", "team": "RB"}], "channels": {}},
        pd.DataFrame({"driver": ["VER", "VER"], "lap_number": [1, 2], "lap_time_s": [90.0, 89.0]}), None, {}, {})
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


@pytest.fixture
def page(stack):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        pg = ctx.new_page()
        pg.errors = []
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.base = stack
        yield pg
        browser.close()


def D(code, team, colour, headshot=None, number="1"):
    return {"code": code, "number": number, "name": f"{code} Driver", "team": team, "team_colour": colour, "headshot_url": headshot}


def mock(page, routes):
    """Answer /api/historical/** from ``routes``: path (after /api/historical) -> JSON or a callable(query) -> JSON."""
    def handler(route):
        u = urlparse(route.request.url)
        key = unquote(u.path)[len("/api/historical"):]
        if route.request.method == "POST":
            key = "POST " + key
        hit = routes.get(key)
        if hit is None:
            return route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": f"unmocked {key}"}))
        body = hit(u.query) if callable(hit) else hit
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    page.route("**/api/historical/**", handler)


def manifest(sid, session="Race", stype="R", drivers=None):
    return {"session_id": sid, "event": "Abu Dhabi Grand Prix", "round": 24, "circuit": "Yas Marina", "session": session, "session_type": stype,
            "session_date": "2025-12-07", "provider": "openf1", "source_label": "HISTORICAL F1 · OPENF1", "drivers": drivers or [],
            "channels": {}, "datasets": {}, "attribution": ""}


def listing(sid, session="Race", stype="R", year=2025, event="Abu Dhabi Grand Prix", date="2025-12-07"):
    return {"session_id": sid, "year": year, "event": event, "session": session, "session_type": stype, "session_date": date, "drivers": []}


def ok(rows):
    return {"rows": rows, "coverage": {"status": "ok", "note": ""}}


def goto(page, hash_, wait):
    page.goto(page.base + "/about:blank")
    page.goto(page.base + "/" + hash_)
    page.reload()
    page.wait_for_selector(wait)


STAND_DRIVERS = [D("VER", "Red Bull Racing", "3671C6", HEADSHOT), D("HAM", "Ferrari", "E80020")]
STAND_ROWS = [{"driver": "HAM", "position_current": 2, "points_current": 40, "points_start": 25},
              {"driver": "VER", "position_current": 1, "points_current": 50, "points_start": 25}]
TEAM_ROWS = [{"team": "Red Bull Racing", "position_current": 1, "points_current": 50}, {"team": "Ferrari", "position_current": 2, "points_current": 40}]


def standings_routes(drivers_ds, teams_ds, sessions=None, sid=SID):
    return {"/sessions": sessions or [listing(sid)], f"/sessions/{sid}/datasets/drivers_championship": drivers_ds,
            f"/sessions/{sid}/datasets/teams_championship": teams_ds, f"/sessions/{sid}/drivers": STAND_DRIVERS}


def test_standings_with_data(page):
    page.route(HEADSHOT, lambda r: r.abort())
    mock(page, standings_routes(ok(STAND_ROWS), ok(TEAM_ROWS)))
    goto(page, "#/standings", "#stand-body table")
    rows = page.locator("#stand-body table").first.locator("tbody tr")
    assert rows.count() == 2
    assert [rows.nth(i).locator("td").nth(2).inner_text() for i in range(2)] == ["■ VER", "■ HAM"]  # ordered by position
    assert "+25" in rows.nth(0).inner_text() and "+15" in rows.nth(1).inner_text()
    assert "Red Bull Racing" in page.inner_text("#stand-body") and page.locator("#stand-body table").count() == 2
    assert page.errors == []


def test_standings_with_a_failed_dataset_shows_its_status_and_note(page):
    failed = {"rows": [], "coverage": {"status": "failed", "note": "OpenF1 answered 500 for drivers_championship"}}
    mock(page, standings_routes(failed, ok(TEAM_ROWS)))
    goto(page, "#/standings", "#stand-body table")
    text = page.inner_text("#stand-body")
    assert "Drivers championship" in text and "failed" in text and "answered 500" in text
    assert page.locator("#stand-body table").count() == 1 and "Ferrari" in text  # the teams table still renders
    assert page.errors == []


def test_standings_for_a_qualifying_session_shows_the_fixed_text_and_no_table(page):
    q = "openf1-2025-abu-dhabi-grand-prix-q"
    mock(page, standings_routes(ok(STAND_ROWS), ok(TEAM_ROWS), sessions=[listing(SID), listing(q, "Qualifying", "Q")]))
    goto(page, f"#/standings/{q}", "#stand-body .empty")
    assert page.inner_text("#stand-body").strip() == "Standings are published for race and sprint sessions only"
    assert page.locator("#stand-body table").count() == 0 and page.errors == []


def test_headshot_load_failure_shows_the_coded_circle(page):
    page.route(HEADSHOT, lambda r: r.abort())
    mock(page, standings_routes(ok(STAND_ROWS), ok(TEAM_ROWS)))
    goto(page, "#/standings", "#stand-body table")
    page.wait_for_selector("#stand-body span.avatar")
    assert page.locator("#stand-body img.avatar").count() == 0
    circle = page.locator("#stand-body span.avatar").first
    assert circle.inner_text() == "VER" and circle.get_attribute("aria-label") == "VER"
    assert "rgb(54, 113, 198)" in circle.evaluate("e => getComputedStyle(e).backgroundColor")  # the team colour 3671C6
    assert page.locator("#stand-body span.avatar").count() == 2  # HAM has no headshot at all


def test_headshot_that_loads_is_an_img_without_referrer(page):
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
    page.route(HEADSHOT, lambda r: r.fulfill(status=200, content_type="image/png", body=png))
    mock(page, standings_routes(ok(STAND_ROWS), ok(TEAM_ROWS)))
    goto(page, "#/standings", "#stand-body table")
    img = page.locator("#stand-body img.avatar")
    assert img.count() == 1 and img.get_attribute("referrerpolicy") == "no-referrer" and img.get_attribute("width") == "32"


# ---- wind direction
def weather_routes(samples):
    return {f"/sessions/{SID}": manifest(SID, drivers=STAND_DRIVERS), f"/sessions/{SID}/weather": {"samples": samples, "note": ""},
            f"/sessions/{SID}/laps": []}


def wind(t, deg):
    return {"time_s": t, "air_temp_c": 25, "wind_direction_deg": deg, "wind_speed_ms": 2}


def test_wind_direction_chart_with_data(page):
    mock(page, weather_routes([wind(0, 0), wind(60, 90), wind(120, 225), wind(180, 359)]))
    goto(page, f"#/hist/{SID}/weather", "#hist-body")
    page.wait_for_selector("text=Wind direction by sample")
    panel = page.locator(".panel:has(h2:text-is('Wind direction'))")
    assert panel.locator("polyline").count() == 1 and panel.locator("circle").count() == 4
    rows = page.locator(".panel:has(h2:text-is('Wind direction by sample')) tbody tr")
    assert [rows.nth(i).locator("td").nth(1).inner_text() for i in range(4)] == ["0° N", "90° E", "225° SW", "359° N"]
    assert page.errors == []


def test_wind_direction_with_missing_samples_leaves_gaps_not_zeros(page):
    mock(page, weather_routes([wind(0, 10), wind(60, None), wind(120, 200)]))
    goto(page, f"#/hist/{SID}/weather", "#hist-body")
    page.wait_for_selector("text=Wind direction by sample")
    panel = page.locator(".panel:has(h2:text-is('Wind direction'))")
    assert panel.locator("circle").count() == 2  # the null sample is not plotted, so it is not drawn as 0
    cells = page.locator(".panel:has(h2:text-is('Wind direction by sample')) tbody tr td:nth-child(2)")
    assert [cells.nth(i).inner_text() for i in range(3)] == ["10° N", "no data", "200° S"]
    assert page.errors == []


def test_wind_direction_all_null_shows_one_note_and_no_chart(page):
    mock(page, weather_routes([wind(0, None), wind(60, None)]))
    goto(page, f"#/hist/{SID}/weather", "#hist-body")
    page.wait_for_selector("text=wind direction not published for this session")
    assert page.locator("h2:text-is('Wind direction')").count() == 0 and page.locator("h2:text-is('Wind direction by sample')").count() == 0
    assert page.errors == []


# ---- season comparison and dashes
def season_routes(cells):
    ids = [(SID2, "Bahrain Grand Prix", "2025-04-13"), (SID, "Abu Dhabi Grand Prix", "2025-12-07")]
    team = [D("VER", "Red Bull Racing", "3671C6"), D("TSU", "Red Bull Racing", "3671C6", number="22")]
    r = {"/sessions": [listing(i, event=e, date=d) for i, e, d in ids]}
    for i, _, _ in ids:
        r[f"/sessions/{i}/drivers"] = team
    r["POST /compare-sessions"] = {"metric": "best_lap", "drivers": ["VER", "TSU"],
                                   "sessions": [{"id": i, "label": f"{e} R", "session_type": "R", "year": 2025, "event": e} for i, e, _ in ids],
                                   "cells": cells}
    return r


def test_season_comparison_shows_a_null_cell_as_a_gap_with_its_reason_and_dashes_same_colour_series(page):
    cells = [{"session_id": SID2, "driver": "VER", "value": 90.1, "reason": None}, {"session_id": SID, "driver": "VER", "value": 89.5, "reason": None},
             {"session_id": SID2, "driver": "TSU", "value": None, "reason": "no valid lap in this session"},
             {"session_id": SID, "driver": "TSU", "value": 91.2, "reason": None}]
    mock(page, season_routes(cells))
    goto(page, "#/season", "#cmp-sessions input.cmp-s >> nth=1")
    for sid in (SID2, SID):
        page.check(f"#cmp-sessions input.cmp-s[value='{sid}']")
    page.wait_for_selector("#cmp-drivers input.cmp-d")
    for code in ("VER", "TSU"):
        page.check(f"#cmp-drivers input.cmp-d[value='{code}']")
    page.click("#cmp-go")
    page.wait_for_selector("#cmp-out svg")
    # one colour for both drivers, so the second series is dashed
    dashes = page.eval_on_selector_all("#cmp-out svg path[stroke]", "ps => ps.map(p => [p.getAttribute('stroke'), p.getAttribute('stroke-dasharray')])")
    assert {c for c, _ in dashes} == {"#3671C6"} and {d for _, d in dashes} == {"0", "6 3"}
    assert "dash 6 3" in page.inner_text("#cmp-out .legend")
    # the missing value is a gap marker with its reason, never a point at 0
    assert "×" in page.text_content("#cmp-out svg")
    reasons = page.inner_text("#cmp-reasons")
    assert "TSU" in reasons and "no valid lap in this session" in reasons and "(R)" in reasons
    assert page.locator("#cmp-out svg circle").count() == 3
    assert page.errors == []


def test_two_drivers_with_one_colour_get_different_dashes_on_the_compare_tab(page):
    drivers = [D("VER", "Red Bull Racing", "3671C6"), D("TSU", "Red Bull Racing", "3671C6", number="22")]
    pts = [{"distance_m": d, "speed_kph": 100 + d / 10, "throttle_pct": 50, "brake_applied": 0} for d in (0, 500, 1000)]
    side = lambda c: {"driver": c, "lap": 1, "points": pts}  # noqa: E731
    mock(page, {f"/sessions/{SID}": manifest(SID, drivers=drivers), f"POST /sessions/{SID}/compare": {
        "a": side("VER"), "b": side("TSU"), "delta": {"note": "n", "distance_pct": [0, 0.5, 1], "delta_s": [0, 0.1, 0.2]}}})
    goto(page, f"#/hist/{SID}/compare", "#hcmpgo")
    page.click("#hcmpgo")
    page.wait_for_selector("#hcmpout svg")
    first = page.locator("#hcmpout .panel").first.locator("polyline")
    assert first.count() == 2
    assert [first.nth(i).get_attribute("stroke-dasharray") for i in range(2)] == [None, "6 3"]
    assert page.errors == []


# ---- mini-sector and tyre tabs
def test_mini_sector_tab_renders_fixture_data(page):
    slices = [{"index": i, "start_frac": i / 10, "end_frac": (i + 1) / 10, "time_s": 9.0 + i / 100, "delta_s": d, "cum_delta_s": 0.0}
              for i, d in enumerate([-0.05, 0.02, 0.0, 0.01, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01])]
    slices[-1]["cum_delta_s"] = 0.03
    track = [{"x_m": 100 * i, "y_m": (i * 37) % 200, "distance_m": 10 * i} for i in range(40)]
    ms = {"driver": "VER", "lap": 2, "lap_source": "fastest", "lap_time_s": 90.0, "sum_slices_s": 90.0, "distance_note": "Distance is an estimate.", "slices": slices}
    mock(page, {f"/sessions/{SID}": manifest(SID, drivers=STAND_DRIVERS), f"/sessions/{SID}/mini-sectors": ms, f"/sessions/{SID}/track-map": {"points": track}})
    goto(page, f"#/hist/{SID}/minisectors", "#msout svg")
    assert page.locator("#msout svg.chart").count() == 2  # bars and track map
    assert page.locator("#msout svg[aria-label='mini-sector bars'] rect").count() == 10
    assert "+0.030" in page.inner_text("#msout .kpis") and "Distance is an estimate." in page.inner_text("#msout")
    assert page.locator("#msout svg[aria-label^='track map'] line[stroke='#2ecc71']").count() > 0
    assert page.errors == []


def test_tyre_tab_renders_fixture_data(page):
    stints = [{"driver": "VER", "stint": 1, "compound": "MEDIUM", "tyre_age_first": 1, "tyre_age_last": 5, "laps_used": 5, "laps_excluded": {"pit_lap": 1},
               "slope_s_per_lap": 0.05, "r_squared": 0.9, "reason": None},
              {"driver": "VER", "stint": 2, "compound": "HARD", "tyre_age_first": 1, "tyre_age_last": 4, "laps_used": 1, "laps_excluded": {},
               "slope_s_per_lap": None, "r_squared": None, "reason": "fewer than 3 usable laps"}]
    laps = [{"driver": "VER", "stint": 1, "lap_number": n, "lap_time_s": 90 + 0.05 * n, "tyre_life": n, "is_valid": True} for n in range(1, 6)]
    r = {"stints": stints, "by_compound": [{"compound": "MEDIUM", "median_slope_s_per_lap": 0.05, "stints": 1}], "caveat": "Fuel burn is not corrected; a negative slope can be caused by falling fuel load."}
    mock(page, {f"/sessions/{SID}": manifest(SID, drivers=STAND_DRIVERS), f"/sessions/{SID}/tyre-degradation": r, f"/sessions/{SID}/laps": laps})
    goto(page, f"#/hist/{SID}/tyres", "#tydrv")
    assert "Fuel burn is not corrected" in page.inner_text("#tyre-caveat")
    assert page.locator("#hist-body tbody tr").count() == 3  # two stints and one compound row
    text = page.inner_text("#hist-body")
    assert "+0.050" in text and "fewer than 3 usable laps" in text and "pit lap: 1" in text
    assert page.locator("#typlot circle").count() == 5 and page.locator("#typlot line[stroke-dasharray]").count() >= 1
    page.select_option("#tydrv", "")
    assert page.locator("#typlot svg").count() == 1 and page.errors == []


# ---- export
def test_export_buttons_download_files(page):
    """Real export routes, on a stored session (no mocking)."""
    goto(page, "#/hist/exp1/overview", ".export-buttons")
    assert page.locator(".export-buttons a.btn:not(.primary)").count() == 6
    with page.expect_download() as d:
        page.click(".export-buttons a:has-text('Laps CSV')")
    assert d.value.suggested_filename == "exp1-laps.csv"
    assert Path(d.value.path()).read_text().splitlines()[0].count(",") > 1
    # the mini-sectors link is off until a driver is chosen
    ms = page.locator("[data-ms-link]")
    assert ms.get_attribute("aria-disabled") == "true" and ms.get_attribute("href") is None
    page.select_option("[data-ms-driver]", "VER")
    assert ms.get_attribute("aria-disabled") is None and ms.get_attribute("href").endswith("table=mini_sectors&driver=VER")
    assert page.locator(".export-buttons a.primary").get_attribute("rel") == "noopener"
