"""Import keeps colour and headshot (Requirements 3.1, 3.2)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from db import create_db_engine, migrate
from f1lab.integrations.historical.openf1_normalizer import driver_list
from f1lab.integrations.historical.service import HistoricalService

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "openf1").glob("*.json"))
HEX = "0123456789abcdefABCDEF"

valid_colour = st.text(alphabet=HEX, min_size=6, max_size=6)
invalid_colour = st.one_of(
    st.none(), st.integers(), st.booleans(), st.lists(st.integers(), max_size=2),
    st.text(alphabet=HEX, min_size=0, max_size=5),
    st.text(alphabet=HEX, min_size=7, max_size=9),
    st.just("GGGGGG"), st.just("#ff0000"), st.just(""),
)
valid_url = st.builds(lambda h, p: f"https://{h}.example.com/{p}", st.text("abcdefgh", min_size=1, max_size=8),
                      st.text("abcdef0123/_-", max_size=30))
invalid_url = st.one_of(
    st.none(), st.integers(), st.just(""), st.just("   "),
    st.builds(lambda h: f"http://{h}.example.com/x.png", st.text("abc", min_size=1, max_size=5)),
    st.just("https://"), st.just("not a url"), st.just("ftp://example.com/a"),
    st.just("https://example.com/" + "a" * 600),
)


def _row(n, colour, url):
    return {"driver_number": n, "name_acronym": f"D{n:02d}", "full_name": f"Driver {n}", "team_name": "T",
            "team_colour": colour, "headshot_url": url}


def _service(tmp_path):
    engine = create_db_engine(tmp_path / "trialforge.db")
    migrate(engine)
    return HistoricalService(engine, tmp_path / "cache")


# Feature: f1-historical-phase1, Property 14: Import keeps colour and headshot
def test_fixture_driver_rows_keep_cleaned_colour_and_headshot():
    assert FIXTURES
    for path in FIXTURES:
        rows = json.loads(path.read_text())["drivers"]
        assert rows, path.name
        by_number = {}
        for r in rows:
            by_number.setdefault(str(r["driver_number"]), r)
        out = driver_list(rows)
        assert {d["number"] for d in out} == set(by_number)
        for d in out:
            src = by_number[d["number"]]
            col, url = src.get("team_colour"), src.get("headshot_url")
            ok_col = isinstance(col, str) and len(col.strip()) == 6 and all(c in HEX for c in col.strip())
            ok_url = isinstance(url, str) and url.strip().lower().startswith("https://") and len(url.strip()) <= 500
            assert d["team_colour"] == (col.strip().upper() if ok_col else None), path.name
            assert d["headshot_url"] == (url.strip() if ok_url else None), path.name


# Feature: f1-historical-phase1, Property 14: Import keeps colour and headshot
@settings(max_examples=150, deadline=None)
@given(st.lists(st.tuples(st.one_of(valid_colour, invalid_colour), st.one_of(valid_url, invalid_url)),
                min_size=1, max_size=20))
def test_generated_driver_rows_clean_or_none(pairs):
    rows = [_row(i + 1, c, u) for i, (c, u) in enumerate(pairs)]
    out = driver_list(rows)
    assert len(out) == len(rows)
    for d, (c, u) in zip(out, pairs):
        want_c = c.upper() if isinstance(c, str) and len(c) == 6 and all(x in HEX for x in c) else None
        want_u = u if isinstance(u, str) and u.startswith("https://") and len(u) > 8 and len(u) <= 500 \
            and u != "https://" and " " not in u else None
        assert d["team_colour"] == want_c
        assert d["headshot_url"] == want_u


def test_old_format_manifest_returns_none_for_both(tmp_path):
    svc = _service(tmp_path)
    manifest = {"session_id": "old", "provider": "openf1", "year": 2024, "event": "Italian Grand Prix", "session_type": "Q",
                "drivers": [{"code": "VER", "number": "1", "name": "Max Verstappen", "team": "Red Bull"}], "channels": {}}
    svc.repo.save_session("old", manifest, pd.DataFrame({"driver": ["VER"], "lap_number": [1], "lap_time_s": [80.0]}),
                          pd.DataFrame(), {}, {})
    drivers = svc.drivers("old")
    assert drivers[0]["team_colour"] is None and drivers[0]["headshot_url"] is None
    assert drivers[0]["code"] == "VER" and drivers[0]["team"] == "Red Bull"
