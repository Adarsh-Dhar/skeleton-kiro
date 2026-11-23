"""Property tests for colour and URL cleaning (Requirement 3.3, design D5)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from hypothesis import given, settings, strategies as st

from f1lab.integrations.historical.analysis import clean_colour, clean_url

non_strings = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.binary(max_size=20),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
    st.tuples(st.text(max_size=5)),
)
hexes = st.text(alphabet="0123456789abcdefABCDEF", min_size=6, max_size=6)
urlish = st.builds(
    lambda scheme, host, path: f"{scheme}://{host}{path}",
    st.sampled_from(["https", "HTTPS", "http", "ftp", ""]),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-:@", max_size=20),
    st.text(max_size=600),
)
any_text = st.one_of(st.text(max_size=600), hexes, urlish)
any_value = st.one_of(any_text, non_strings)


def _check_colour(result: object) -> None:
    assert result is None or (
        isinstance(result, str) and re.fullmatch(r"[0-9A-F]{6}", result) is not None
    )


def _check_url(result: object) -> None:
    if result is None:
        return
    assert isinstance(result, str)
    assert len(result) <= 500
    parts = urlsplit(result)
    assert parts.scheme == "https"
    assert parts.hostname


# Feature: f1-historical-phase1, Property 2: Colour and URL cleaning
# **Validates: Requirements 3.3**
@settings(max_examples=300, deadline=None)
@given(any_value)
def test_colour_and_url_cleaning_valid_or_none(value):
    _check_colour(clean_colour(value))
    _check_url(clean_url(value))


# Feature: f1-historical-phase1, Property 2: Colour and URL cleaning
# **Validates: Requirements 3.3**
@settings(max_examples=100, deadline=None)
@given(hexes)
def test_valid_colour_is_uppercased(value):
    assert clean_colour(value) == value.upper()
    assert clean_colour(f"  {value}\n") == value.upper()


# Feature: f1-historical-phase1, Property 2: Colour and URL cleaning
# **Validates: Requirements 3.3**
@settings(max_examples=100, deadline=None)
@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/_-", max_size=50),
)
def test_valid_https_url_accepted_and_non_https_rejected(host, path):
    url = f"https://{host}.example.com/{path}"
    assert clean_url(url) == url
    assert clean_url(f"http://{host}.example.com/{path}") is None
    assert clean_url("https://" + "a" * 500 + ".com") is None
