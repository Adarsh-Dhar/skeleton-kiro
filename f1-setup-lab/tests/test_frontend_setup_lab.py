"""Frontend checks for the restored Setup Lab views (requirements 7.1, 7.2, 7.5 to 7.12).

Static checks (node --check, index.html structure, forbidden content) always run. The browser tests are opt-in in the same
way as test_ui_openf1.py: they run only where Playwright and its Chromium are installed."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from f1lab import NOTICES

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
SCRIPTS = ["charts.js", "hist_race.js", "hist_picker.js", "setup_lab.js", "hist_analysis.js", "app.js"]
NAV = [("Setup Engineering", ["Dashboard", "Experiments", "Telemetry import"]),
       ("Historical F1", ["Import from OpenF1", "Imported sessions", "Standings", "Season comparison"])]


# ------------------------------------------------------------------------------------------------ static checks
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", SCRIPTS)
def test_every_frontend_script_passes_node_check(name):
    r = subprocess.run(["node", "--check", str(FRONTEND / name)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


class _Nav(HTMLParser):
    """Collects (heading, entry label, data-nav) for every link inside <nav id="nav">."""

    def __init__(self):
        super().__init__()
        self.in_nav = False
        self.heading = None
        self.mode = None
        self.entries: list[tuple[str, str, str]] = []
        self.headings: list[str] = []
        self._data_nav = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "nav" and a.get("id") == "nav":
            self.in_nav = True
        elif self.in_nav and tag == "h2":
            self.mode = "h"
            self.headings.append("")
        elif self.in_nav and tag == "a":
            self.mode = "a"
            self._data_nav = a.get("data-nav")
            self.entries.append((self.headings[-1] if self.headings else "", "", self._data_nav or ""))

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_nav = False
        if tag in ("h2", "a"):
            self.mode = None

    def handle_data(self, data):
        if self.mode == "h":
            self.headings[-1] += data
        elif self.mode == "a":
            h, label, d = self.entries[-1]
            self.entries[-1] = (h, label + data, d)


def test_index_html_has_seven_entries_under_two_headings():
    p = _Nav()
    p.feed((FRONTEND / "index.html").read_text())
    assert [h.strip() for h in p.headings] == [h for h, _ in NAV]
    got: dict[str, list[str]] = {}
    for heading, label, _ in p.entries:
        got.setdefault(heading.strip(), []).append(label.strip())
    assert got == dict(NAV)
    assert len(p.entries) == 7 and len({e[1] for e in p.entries}) == 7  # each entry under exactly one heading


def test_index_html_loads_scripts_in_order():
    html = (FRONTEND / "index.html").read_text()
    assert re.findall(r'<script src="([^"]+)"', html) == SCRIPTS


def test_no_extra_router_pages_tabs_or_report_views():
    app = (FRONTEND / "app.js").read_text()
    sl = (FRONTEND / "setup_lab.js").read_text()
    # the router dispatches only the known pages
    heads = set(re.findall(r"head === '([a-z]+)'", app))
    assert heads == {"dashboard", "experiments", "new", "telemetry", "e", "hist", "sessions", "standings", "season"}
    # experiment tabs and historical tabs are exactly the known sets: no extra report tab
    assert [k for k, _ in re.findall(r"\['([a-z]+)', '([^']+)'\]", re.search(r"const TABS = \[(.*?)\];", sl).group(1))] == \
        ["design", "runs", "telemetry", "analysis", "optimise", "report"]
    hist = re.search(r"const HIST_TABS = \[(.*?)\];", app).group(1) + re.search(r"const OPENF1_TABS = \[(.*?)\];", app).group(1)
    assert re.findall(r"\['([a-z]+)'", hist) == ["overview", "laps", "telemetry", "track", "compare", "weather", "minisectors", "tyres", "race", "strategy"]
    view_fns = set(re.findall(r"^async function (view\w+)", app + sl, re.M))
    assert not [f for f in view_fns if re.search(r"tyre|standing|report", f, re.I) and f != "viewReport"]


# ------------------------------------------------------------------------------------------------ browser tests
pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    import uvicorn
    from openf1_fakes import FakeOpenF1, fake_client

    from f1lab.api.app import create_app

    root = tmp_path_factory.mktemp("setup-lab-ui")
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


@pytest.fixture
def page(stack):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 1000})
        pg.errors = []
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.base = stack
        yield pg
        browser.close()


ENTRIES = [("dashboard", "Dashboard"), ("experiments", "Experiments"), ("telemetry", "Telemetry import"),
           ("hist", "Historical F1"), ("sessions", "Imported sessions")]


def _heading_shown(page, text):
    page.wait_for_function("t => document.querySelector('#view-body h1')?.textContent.trim() === t", arg=text, timeout=5000)


def test_each_nav_entry_renders_its_view_within_one_second(page):
    page.goto(page.base + "/#/dashboard")
    _heading_shown(page, "Dashboard")
    for key, title in ENTRIES[1:] + ENTRIES[:1]:
        t0 = time.perf_counter()
        page.click(f"#nav a[data-nav={key}]")
        _heading_shown(page, title)
        assert time.perf_counter() - t0 < 1.0, key
        assert "on" in page.get_attribute(f"#nav a[data-nav={key}]", "class").split()
    assert page.errors == []


def test_historical_views_keep_their_content_and_controls(page):
    page.goto(page.base + "/#/hist")
    page.wait_for_selector("#hprov")
    for sel in ("#hyear", "#hevent", "#hsession", "#hdrivers", "#himport", "#hist-jobs", "#hist-lib"):
        assert page.locator(sel).count() == 1, sel
    assert page.input_value("#hprov") == "openf1"
    assert page.inner_text("#banner") == NOTICES["historical"]
    page.click("#nav a[data-nav=sessions]")
    _heading_shown(page, "Imported sessions")
    assert page.locator("#view a:has-text('Import a session')").count() == 1
    assert page.inner_text("#banner") == NOTICES["historical"]
    assert page.errors == []


def _in_viewport(page, sel):
    box = page.locator(sel).bounding_box()
    vh = page.viewport_size["height"]
    return box is not None and box["height"] > 0 and box["y"] >= 0 and box["y"] + box["height"] <= vh


@pytest.mark.parametrize(("hash_", "mode", "title"), [
    ("#/dashboard", "synthetic", "Dashboard"), ("#/experiments", "synthetic", "Experiments"), ("#/new", "synthetic", "Create experiment"),
    ("#/telemetry", "synthetic", "Telemetry import")])
def test_simulator_notices_are_unchanged_and_in_the_viewport(page, hash_, mode, title):
    page.goto(page.base + "/" + hash_)
    _heading_shown(page, title)
    assert page.text_content("#banner") == NOTICES[mode]
    assert _in_viewport(page, "#banner")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    assert _in_viewport(page, "#banner")  # the notice stays in the fixed header


def test_api_failure_shows_the_backend_message_and_keeps_navigation_usable(page):
    page.goto(page.base + "/#/new")
    _heading_shown(page, "Create experiment")
    page.route("**/api/experiments", lambda r: r.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "backend exploded"})))
    page.click("#nav a[data-nav=dashboard]")
    page.wait_for_function("document.querySelector('#view-error')?.textContent.includes('backend exploded') || document.querySelector('#dash')?.textContent.includes('backend exploded')", timeout=1500)
    page.click("#nav a[data-nav=sessions]")  # navigation still works
    _heading_shown(page, "Imported sessions")
    assert "backend exploded" not in page.inner_text("#view-error")
    page.unroute("**/api/experiments")
    # an unreachable backend (or a response with no message) reads "request failed"
    page.route("**/api/experiments", lambda r: r.abort())
    page.click("#nav a[data-nav=dashboard]")
    page.wait_for_function("(document.querySelector('#view-error')?.textContent + (document.querySelector('#dash')?.textContent || '')).includes('request failed')", timeout=1500)
    page.click("#nav a[data-nav=experiments]")
    _heading_shown(page, "Experiments")
    page.unroute("**/api/experiments")


def test_one_chart_of_each_type_renders(page):
    page.goto(page.base + "/#/sessions")
    _heading_shown(page, "Imported sessions")
    html = page.evaluate("""() => {
      const pts = [0, 1, 2, 3, 4].map((i) => ({ x: i, y: i * i }));
      const track = Array.from({ length: 40 }, (_, i) => ({ x_m: 100 * Math.cos(i / 6), y_m: 60 * Math.sin(i / 6), speed_kph: 100 + i, distance_m: i * 20 }));
      return {
        scatter: Charts.scatter({ points: pts, xLabel: 'x', yLabel: 'y' }),
        bars: Charts.bars({ items: [{ label: 'A', value: -1, lo: -2, hi: 0, sig: true }, { label: 'B', value: 0.5, lo: -0.5, hi: 1.5, sig: false }] }),
        lines: Charts.lines({ series: [{ name: 's', color: '#e10600', pts }], xLabel: 'x', yLabel: 'y' }),
        sectors: Charts.sectors({ laps: [1, 2, 3].map((n) => ({ lap: n, sector_1: 30 + n, sector_2: 40, sector_3: 20, lap_time: 90 + n })) }),
        trackMap: Charts.trackMap({ points: track, corners: [{ number: 1, name: 'T1', distance_m: 100 }] }),
      };
    }""")
    for kind, svg in html.items():
        assert svg.startswith("<svg") and "</svg>" in svg, kind
        page.evaluate("h => { const d = document.createElement('div'); d.className = 'probe'; d.innerHTML = h; document.body.appendChild(d); }", svg)
    assert page.locator(".probe svg.chart").count() == 5
    assert page.locator(".probe svg.chart *").count() > 5 * 3
    assert page.errors == []
# ------------------------------------------------------------------------------------------------ Experiments page
def _delete_all_experiments_route(page):
    page.route("**/api/experiments", lambda r: r.fulfill(status=200, content_type="application/json", body="[]") if r.request.method == "GET" else r.continue_())
def test_experiments_page_shows_empty_state_and_create_button_opens_the_form(page):
    _delete_all_experiments_route(page)
    page.goto(page.base + "/#/experiments")
    _heading_shown(page, "Experiments")
    page.wait_for_selector("#xlist .empty")
    assert "no experiments yet" in page.inner_text("#xlist").lower()
    assert page.locator("#xnew.btn.primary").text_content() == "Create new experiment"
    assert "on" in page.get_attribute("#nav a[data-nav=experiments]", "class").split()
    page.click("#xnew")
    _heading_shown(page, "Create experiment")
    assert page.locator("#f").count() == 1 and page.locator("#view a:has-text('Back to experiments')").count() == 1
    assert "on" in page.get_attribute("#nav a[data-nav=experiments]", "class").split()
    assert page.locator("#nav a.on").count() == 1
    assert page.inner_text("#banner") == NOTICES["synthetic"]
    page.unroute("**/api/experiments")
    page.click("#view a:has-text('Back to experiments')")
    _heading_shown(page, "Experiments")
    assert page.errors == []
def test_experiments_page_lists_api_created_experiments_filters_and_opens_rows(page):
    import urllib.request
    ids = {}
    for name in ("Alpha listing exp", "Bravo listing exp"):
        req = urllib.request.Request(page.base + "/api/experiments", data=json.dumps({"name": name}).encode(), headers={"content-type": "application/json"}, method="POST")
        ids[name] = json.load(urllib.request.urlopen(req))["id"]
    page.goto(page.base + "/#/experiments")
    _heading_shown(page, "Experiments")
    page.wait_for_selector("#xlist tr[data-id]")
    for name, i in ids.items():
        assert page.locator(f"#xlist tr[data-id='{i}']").inner_text().startswith(name)
    assert [h.text_content() for h in page.locator("#xlist th").all()][:3] == ["Experiment", "Status", "Progress"]
    page.fill("#xq", "bravo")
    assert page.locator("#xlist tr[data-id]").count() == 1
    page.fill("#xq", "zzz-no-match")
    assert "no experiments match" in page.inner_text("#xlist").lower()
    page.fill("#xq", "")
    page.click(f"#xlist tr[data-id='{ids['Alpha listing exp']}']")
    page.wait_for_function("l => location.hash === l", arg=f"#/e/{ids['Alpha listing exp']}/design")
    _heading_shown(page, "Alpha listing exp")
    assert "on" in page.get_attribute("#nav a[data-nav=experiments]", "class").split()
    assert page.locator("#nav a.on").count() == 1
    assert page.errors == []
