"""Property tests for CSV timestamp parsing (f1-unified-telemetry-import, Properties 10-13)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from f1lab.integrations.historical.csv_normalizer import parse_timestamp, to_session_time

SETTINGS = settings(max_examples=100, deadline=None)
UTC = UTC
TOL = timedelta(milliseconds=1)

# Instants at millisecond resolution, 1975..2100 (epoch ms >= 1e11 and seconds < 1e11 both hold).
instants = st.integers(min_value=int(1.7e11), max_value=int(4.1e12)).map(
    lambda ms: datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)
)
naive_instants = instants.map(lambda d: d.replace(tzinfo=None))
IANA = ["UTC", "America/New_York", "Europe/Berlin", "Asia/Kolkata", "Australia/Sydney", "Pacific/Auckland"]
offsets = st.integers(min_value=-14 * 60, max_value=14 * 60).map(lambda m: timezone(timedelta(minutes=m)))


def _ms(dt: datetime) -> str:
    return f"{dt.microsecond // 1000:03d}"


def _fmt_off(off: timezone) -> str:
    total = int(off.utcoffset(None).total_seconds() // 60)
    sign = "-" if total < 0 else "+"
    return f"{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d}"


def _close(a: datetime | None, b: datetime) -> bool:
    return a is not None and a.tzinfo is not None and abs(a - b) < TOL


# Feature: f1-unified-telemetry-import, Property 10: Timestamp parsing is format-independent
# **Validates: Requirements 5.5**
@SETTINGS
@given(t=instants)
def test_property_10_format_independent(t: datetime):
    base = t.strftime("%Y-%m-%d")
    clock = t.strftime("%H:%M:%S") + "." + _ms(t)
    epoch_s = (t - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
    assert epoch_s < 1e11
    epoch_ms = round(epoch_s * 1000)
    assert epoch_ms >= 1e11

    # ISO 8601 (UTC designator)
    assert _close(parse_timestamp(f"{base}T{clock}Z"), t)
    # YYYY-MM-DD HH:MM:SS.fff, interpreted in UTC
    assert _close(parse_timestamp(f"{base} {clock}", user_tz="UTC"), t)
    # HH:MM:SS.fff with the assumed date 1970-01-01
    day = datetime(1970, 1, 1, t.hour, t.minute, t.second, t.microsecond, tzinfo=UTC)
    assert _close(parse_timestamp(clock, user_tz="UTC"), day)
    # epoch seconds, as number and as string
    assert _close(parse_timestamp(epoch_s), t)
    assert _close(parse_timestamp(repr(epoch_s)), t)
    # epoch milliseconds
    assert _close(parse_timestamp(epoch_ms), t)
    assert _close(parse_timestamp(str(epoch_ms)), t)


# Feature: f1-unified-telemetry-import, Property 11: Offset takes precedence over a user-supplied timezone
# **Validates: Requirements 5.6**
@SETTINGS
@given(t=naive_instants, off=offsets, tz=st.sampled_from(IANA), iso=st.booleans(), colon=st.booleans())
def test_property_11_offset_beats_user_timezone(t, off, tz, iso, colon):
    sep = "T" if iso else " "
    text = f"{t.strftime('%Y-%m-%d')}{sep}{t.strftime('%H:%M:%S')}.{_ms(t)}{_fmt_off(off)}"
    expected = t.replace(tzinfo=off).astimezone(UTC)
    assert _close(parse_timestamp(text, user_tz=tz), expected)
    assert parse_timestamp(text, user_tz=tz) == parse_timestamp(text)
    assert _close(parse_timestamp(text, tz_offset="+00:00", user_tz=tz), expected)


# Feature: f1-unified-telemetry-import, Property 12: No offset and no user timezone yields relative time anchored at zero
# **Validates: Requirements 5.7**
@SETTINGS
@given(ts=st.lists(naive_instants, min_size=1, max_size=20), sep=st.sampled_from(["T", " "]))
def test_property_12_relative_time_anchored_at_zero(ts, sep):
    cells = [f"{t.strftime('%Y-%m-%d')}{sep}{t.strftime('%H:%M:%S')}.{_ms(t)}" for t in ts]
    res = to_session_time(cells)
    assert "timezone" in res.unavailable
    assert res.timezone is None
    assert res.bad_rows == []
    assert min(res.session_time_s) == 0
    assert all(v is not None and v >= 0 for v in res.session_time_s)


# Feature: f1-unified-telemetry-import, Property 13: Unparseable timestamps are reported and excluded, never guessed
# **Validates: Requirements 5.8**
@SETTINGS
@given(
    ts=st.lists(naive_instants, min_size=1, max_size=15),
    bad=st.text(alphabet="abcdghjklmopqrsuvwxyzABCDGHJKLMOPQRSUVWXYZ!#%&", min_size=1, max_size=12),
    data=st.data(),
)
def test_property_13_unparseable_reported_not_guessed(ts, bad, data):
    cells: list[object] = [t.strftime("%Y-%m-%d %H:%M:%S.") + _ms(t) for t in ts]
    idx = data.draw(st.integers(min_value=0, max_value=len(cells)))
    cells.insert(idx, bad)
    res = to_session_time(cells)
    assert res.bad_rows == [(idx + 1, bad)]
    assert res.session_time_s[idx] is None
    good = [v for i, v in enumerate(res.session_time_s) if i != idx]
    assert all(v is not None for v in good)
    assert min(good) == 0
