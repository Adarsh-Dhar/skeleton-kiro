"""Property tests for CSV safety and report escaping (Requirements 7.2, 7.5)."""
from __future__ import annotations
import csv
import io
from html import escape
from hypothesis import given, settings, strategies as st
from f1lab.integrations.historical.analysis import csv_text, report_html

_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=20,
)
_formula = st.builds(lambda p, s: p + s, st.sampled_from("=+-@"), _text)
_cell = st.one_of(
    st.none(),
    _text,
    _formula,
    st.integers(min_value=-10**12, max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False),
)
_rows = st.lists(st.lists(_cell, min_size=1, max_size=5), max_size=8)


def _expected(cell: object) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return "'" + cell if cell.startswith(("=", "+", "-", "@")) else cell
    return str(cell)


# Feature: f1-historical-phase1, Property 9: CSV safety and round trip
# **Validates: Requirements 7.2**
@settings(max_examples=200, deadline=None)
@given(_rows, _rows)
def test_property_9_csv_safety_and_round_trip(header_rows, rows):
    header = header_rows[0] if header_rows else ["h"]
    text = csv_text(header, rows)
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed == [[_expected(c) for c in r] for r in [header, *rows]]


_hostile = st.builds(
    lambda a, b, c: "ZQ" + a + b + c + "ZQ",
    _text,
    st.sampled_from(["<", ">", "&", '"', "'", "<script>alert(1)</script>", "<b>", "&amp;"]),
    _text,
)


# Feature: f1-historical-phase1, Property 11: Report escaping
# **Validates: Requirements 7.5**
@settings(max_examples=200, deadline=None)
@given(_hostile, _hostile, _hostile, _hostile)
def test_property_11_report_escaping(event, driver, team, compound):
    html = report_html(
        {"event": event, "session_type": driver, "date_start": team},
        results=[{"position": 1, "driver": driver, "dnf": team}],
        standings=[{"kind": "driver", "position": 1, "name": event, "team": team}],
        tyre_degradation={
            "stints": [{"driver": driver, "stint": 1, "compound": compound}],
            "caveat": compound,
        },
        weather=None,
        unavailable={"weather": compound},
        notice=event,
    )
    assert "<script" not in html.lower()
    values = (event, driver, team, compound)
    escaped_forms = {escape(v, quote=True) for v in values}
    for value in values:
        # the ZQ markers make the raw value unmistakable amid the report's own markup; a raw value that is
        # itself the escaped form of another value (e.g. 'ZQ&amp;ZQ' vs 'ZQ&ZQ') legitimately appears
        if any(ch in value for ch in "<>&\"'") and value not in escaped_forms:
            assert value not in html
        assert escape(value, quote=True) in html
