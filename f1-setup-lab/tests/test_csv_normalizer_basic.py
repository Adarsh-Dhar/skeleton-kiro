import math
from datetime import UTC, datetime

import pytest

from f1lab.integrations.historical import csv_normalizer as N


def test_unit_constants_and_inverses():
    assert N.convert_time(1500, "milliseconds") == 1.5
    assert N.convert_speed(1, "mph") == 1.609344
    assert N.convert_speed(10, "m/s") == 36.0
    assert N.convert_distance(2, "kilometres") == 2000.0
    assert math.isclose(N.convert_distance(10, "feet"), 3.048)
    for fn, inv, unit in [(N.convert_time, N.invert_time, "milliseconds"),
                          (N.convert_speed, N.invert_speed, "mph"),
                          (N.convert_distance, N.invert_distance, "feet")]:
        assert math.isclose(inv(fn(123.456, unit), unit), 123.456, rel_tol=1e-9)


def test_unsupported_unit_lists_supported():
    with pytest.raises(N.UnsupportedUnitError) as e:
        N.convert_speed(1, "knots")
    assert "kph, mph, m/s" in str(e.value)


def test_parse_formats():
    utc = UTC
    assert N.parse_timestamp("2024-05-18T10:00:00+02:00") == datetime(2024, 5, 18, 8, tzinfo=utc)
    assert N.parse_timestamp("2024-05-18 10:00:00.500", user_tz="Europe/Rome") == datetime(2024, 5, 18, 8, 0, 0, 500000, tzinfo=utc)
    assert N.parse_timestamp("2024-05-18T10:00:00Z", user_tz="Europe/Rome") == datetime(2024, 5, 18, 10, tzinfo=utc)
    assert N.parse_timestamp("00:01:02.250") == datetime(1970, 1, 1, 0, 1, 2, 250000)
    assert N.parse_timestamp("1700000000") == N.parse_timestamp("1700000000000")
    assert N.parse_timestamp(99999999999) != N.parse_timestamp(100000000000)
    for bad in ["", "abc", "2024-13-40 00:00:00", "nan", "2024-05-18", None]:
        assert N.parse_timestamp(bad) is None


def test_relative_time_without_timezone():
    r = N.to_session_time(["10:00:01.5", "10:00:00", "oops"])
    assert r.session_time_s == [1.5, 0.0, None]
    assert r.timezone is None and r.unavailable == ["timezone"]
    assert r.bad_rows == [(3, "oops")]


def test_session_time_with_timezone():
    r = N.to_session_time(["2024-05-18 10:00:00", "2024-05-18 10:00:02"], user_tz="UTC")
    assert r.session_time_s == [0.0, 2.0] and r.timezone == "UTC" and not r.unavailable
