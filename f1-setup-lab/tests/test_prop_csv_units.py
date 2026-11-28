"""Property test for CSV unit conversion round trip."""
from hypothesis import given, settings
from hypothesis import strategies as st

from f1lab.integrations.historical.csv_normalizer import (
    DISTANCE_UNITS,
    SPEED_UNITS,
    TIME_UNITS,
    convert_distance,
    convert_speed,
    convert_time,
    invert_distance,
    invert_speed,
    invert_time,
)

FAMILIES = {
    "time": (convert_time, invert_time, list(TIME_UNITS)),
    "speed": (convert_speed, invert_speed, list(SPEED_UNITS)),
    "distance": (convert_distance, invert_distance, list(DISTANCE_UNITS)),
}


@st.composite
def family_unit(draw):
    family = draw(st.sampled_from(sorted(FAMILIES)))
    return family, draw(st.sampled_from(FAMILIES[family][2]))


# Feature: f1-unified-telemetry-import, Property 9: Unit conversion round-trips within tolerance
# **Validates: Requirements 5.1, 5.2, 5.3, 5.11**
@settings(max_examples=200, deadline=None)
@given(
    value=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    fu=family_unit(),
)
def test_unit_conversion_round_trips(value, fu):
    family, unit = fu
    convert, invert, _ = FAMILIES[family]
    back = invert(convert(value, unit), unit)
    assert abs(back - value) <= 1e-9 * max(abs(value), 1e-300) or back == value
