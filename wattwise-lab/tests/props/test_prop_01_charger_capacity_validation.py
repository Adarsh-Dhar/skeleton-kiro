"""Property 1: charger capacity validation.
**Validates: Requirements 1.1, 1.3, 1.4**
"""
from __future__ import annotations

import math
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from wattwise.api.schemas import DayRequest, ExperimentRequest, HouseholdSettings, validate_charger_capacity


def _expected_valid(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return False
    if not 1.4 <= v <= 350:
        return False
    return Decimal(repr(float(v))).as_tuple().exponent >= -2


valid_values = st.integers(140, 35000).map(lambda c: c / 100)  # 1.40 to 350.00, 2 decimals
numbers = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(min_value=-10, max_value=400, allow_nan=False),
    st.integers(-1000, 1000),
    st.integers(1, 3500).map(lambda c: c / 1000),  # up to 3 decimals
)
non_numbers = st.one_of(st.booleans(), st.text(max_size=8), st.lists(st.integers(), max_size=2))
any_value = st.one_of(numbers, non_numbers)


def _check_rejected(v, message: str) -> None:
    assert "charger capacity" in message
    is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
    if is_num and math.isfinite(v) and v < 1.4:
        assert str(float(v)) in message and "minimum" in message and "1.4 kW" in message
    else:
        assert str(v) in message and "1.4 kW to 350 kW" in message


# Feature: wattwise-household-model-improvements, Property 1: Charger capacity validation
@settings(max_examples=100, deadline=None)
@given(v=any_value)
def test_validator_accepts_iff_in_range_with_two_decimals(v):
    if _expected_valid(v):
        assert validate_charger_capacity(v) == float(v)
    else:
        with pytest.raises(ValueError) as exc:
            validate_charger_capacity(v)
        _check_rejected(v, str(exc.value))


@settings(max_examples=100, deadline=None)
@given(v=valid_values)
def test_all_two_decimal_values_in_range_are_accepted_by_models(v):
    assert HouseholdSettings(charger_power_kw=v).charger_power_kw == v
    assert DayRequest(charger_capacity_kw=v).charger_capacity_kw == v


@settings(max_examples=100, deadline=None)
@given(v=any_value.filter(lambda x: not _expected_valid(x)))
def test_models_reject_invalid_values(v):
    with pytest.raises(ValidationError):
        HouseholdSettings(charger_power_kw=v)
    with pytest.raises(ValidationError):
        DayRequest(charger_capacity_kw=v)


def test_boundaries_and_none():
    assert validate_charger_capacity(None) is None
    assert validate_charger_capacity(1.4) == 1.4
    assert validate_charger_capacity(350) == 350.0
    for bad in (1.39, 350.01, 7.123, True, "7.4", float("nan")):
        with pytest.raises(ValueError):
            validate_charger_capacity(bad)
    assert "charger_capacity_kw" in ExperimentRequest.model_fields
