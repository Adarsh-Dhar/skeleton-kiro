"""Notice locations (Requirements 9.10, 7.8, 7.9): every notice reaches the API payloads and the Frontend places that need it.

The Frontend does not hard-code notice text. It fills the fixed-header banner from ``/api/meta`` ``notices`` by mode,
so the checks are: the API carries the exact text, the banner sits inside the fixed header, ``setBanner`` writes
``META.notices[mode]`` into it, and each route asks for the right mode. The checker is run against mutated copies too,
to prove it fails when any location lacks its notice."""
from __future__ import annotations

import re
from pathlib import Path

from f1lab import HISTORICAL_NOTICE, NOTICES, SYNTHETIC_NOTICE

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"

# route head -> mode the banner must be set to (Setup Engineering views the synthetic notice, historical views only the historical notice)
EXPECTED_ROUTES = {
    "dashboard": "'synthetic'", "experiments": "'synthetic'", "new": "'synthetic'", "telemetry": "'synthetic'",
    "hist": "'historical'", "sessions": "'historical'",
}


def check_meta(meta: dict) -> list[str]:
    problems = []
    if meta.get("notice") != SYNTHETIC_NOTICE:
        problems.append("/api/meta notice is not SYNTHETIC_NOTICE")
    notices = meta.get("notices") or {}
    for mode, text in (("synthetic", SYNTHETIC_NOTICE), ("historical", HISTORICAL_NOTICE)):
        if notices.get(mode) != text:
            problems.append(f"/api/meta notices[{mode}] missing or changed")
    return problems


def check_experiment_payload(d: dict, mode: str) -> list[str]:
    want = NOTICES[mode]
    return [f"experiment payload {k} lacks the {mode} notice" for k in ("synthetic_notice", "notice") if d.get(k) != want]


def check_frontend(index_html: str, setup_js: str, app_js: str, css: str) -> list[str]:
    problems = []
    m = re.search(r'<div class="fixed-head">(.*?)</div>\s*<main', index_html, re.S)
    if not m or 'id="banner"' not in m.group(1):
        problems.append("index.html: #banner is not inside the fixed header")
    if not re.search(r"\.fixed-head\{[^}]*position:sticky", css):
        problems.append("style.css: .fixed-head is not sticky, so the notice could scroll away")
    if not re.search(r"function setBanner\(mode\)[^}]*META\.notices\[mode\]", setup_js, re.S) or "$('#banner')" not in setup_js:
        problems.append("setup_lab.js: setBanner does not write META.notices[mode] into #banner")
    for head, mode in EXPECTED_ROUTES.items():
        if not re.search(rf"head === '{head}'\)[^\n]*?showNotice\({re.escape(mode)}\)", app_js):
            problems.append(f"app.js: route '{head}' does not show the {mode} notice")
    if not re.search(r"head === 'e' && parts\[1\][\s\S]*?showNotice\('synthetic'\)", app_js):
        problems.append("app.js: experiment views do not show the synthetic notice")
    if "setBanner(d.mode)" not in setup_js:
        problems.append("setup_lab.js: experiment header does not set the banner from the experiment mode")
    return problems


def read_frontend() -> tuple[str, str, str, str]:
    return tuple((FRONTEND / n).read_text(encoding="utf-8") for n in ("index.html", "setup_lab.js", "app.js", "style.css"))


def test_meta_carries_every_notice(client):
    assert check_meta(client.get("/api/meta").json()) == []


def test_experiment_payloads_carry_their_mode_notice(client, make_experiment):
    assert check_experiment_payload(make_experiment(), "synthetic") == []
    assert set(NOTICES) == {"synthetic", "historical"}


def test_frontend_shows_the_right_notice_in_the_fixed_header():
    assert check_frontend(*read_frontend()) == []


def test_static_frontend_is_served_with_the_banner(client):
    html = client.get("/").text
    assert 'id="banner"' in html and 'class="fixed-head"' in html


def test_checker_detects_a_missing_notice_location(client, make_experiment):
    meta = client.get("/api/meta").json()
    assert check_meta({**meta, "notice": "x"})
    assert check_meta({**meta, "notices": {**meta["notices"], "synthetic": ""}})
    assert check_meta({**meta, "notices": {k: v for k, v in meta["notices"].items() if k != "historical"}})
    d = make_experiment()
    assert check_experiment_payload({**d, "synthetic_notice": HISTORICAL_NOTICE}, "synthetic")
    assert check_experiment_payload({k: v for k, v in d.items() if k != "notice"}, "synthetic")

    index, setup, app, css = read_frontend()
    assert check_frontend(index, setup, app, css) == []
    assert check_frontend(index.replace('<div class="banner" id="banner" role="note"></div>', ""), setup, app, css)
    assert check_frontend(index.replace('id="banner"', 'id="elsewhere"'), setup, app, css)
    assert check_frontend(index, setup.replace("META.notices[mode]", "''"), app, css)
    assert check_frontend(index, setup, css=css.replace("position:sticky", "position:static"), app_js=app)
    assert check_frontend(index, setup, app.replace("showNotice('synthetic'); return await viewTelemetryStandalone", "return await viewTelemetryStandalone"), css)
    assert check_frontend(index, setup, app.replace("head === 'dashboard') { setNav('dashboard'); showNotice('synthetic');", "head === 'dashboard') { setNav('dashboard');"), css)
    assert check_frontend(index, setup, app.replace("showNotice('historical'); return await viewSessions", "return await viewSessions"), css)
