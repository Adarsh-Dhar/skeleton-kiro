"""Browser check: the import detail screen shows exactly one provenance badge matching the file's label (Req 3.3-3.6)."""
from __future__ import annotations

import json
import urllib.request

import pytest

pw = pytest.importorskip("playwright.sync_api")

CASES = [
    ("real_meter", "METER EXPORT · DECLARED BY YOU", "prov-m", "2026-03-03"),
    ("unknown", "UNVERIFIED SOURCE", "prov-u", "2026-03-17"),
]  # synthetic_demo can no longer be imported


def _csv(day: str) -> str:
    rows = [f"{day}T{h:02d}:{m:02d}:00,{0.5 + 0.1 * h:.2f}" for h in range(24) for m in (0, 30)]
    return "timestamp,power_kw\n" + "\n".join(rows) + "\n"


def _post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


@pytest.fixture(scope="module")
def page(live):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary missing or unlaunchable
            pytest.skip(f"Chromium could not launch: {e}")
        pg = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        pg.base = live.url
        yield pg
        browser.close()


def test_import_detail_shows_single_matching_provenance_badge(page):
    ids = {}
    for label, _text, _css, day in CASES:
        m = _post(page.base, "/api/meter/import", {"csv": _csv(day), "file_name": f"{label}.csv", "source": "test", "provenance": label})
        ids[label] = m["import_id"]
    page.goto(page.base + "/#usage")
    page.wait_for_selector("[data-open]")
    for label, text, css, _day in CASES:
        page.click(f'[data-open="{ids[label]}"]')
        page.wait_for_function(
            "(f) => document.querySelector('#u-detail h2') && document.querySelector('#u-detail h2').textContent.includes(f)", arg=f"{label}.csv"
        )
        chips = page.locator("#u-detail .chip.prov-m, #u-detail .chip.prov-s, #u-detail .chip.prov-u")
        assert chips.count() == 1
        assert chips.first.inner_text().strip() == text
        assert css in chips.first.get_attribute("class").split()
