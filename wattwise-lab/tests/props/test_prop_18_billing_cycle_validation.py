"""Property 18: billing-cycle consumption validation.
**Validates: Requirements 7.1, 7.4**
"""
from __future__ import annotations

import math
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from wattwise.api.schemas import DayRequest, ExperimentRequest, validate_billing_consumption


def _expected_valid(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return False
    if not 0 <= v <= 1_000_000:
        return False
    return Decimal(repr(float(v))).as_tuple().exponent >= -3


valid_values = st.integers(0, 1_000_000_000).map(lambda c: c / 1000)  # 0 to 1,000,000 with 3 decimals
numbers = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(min_value=-10, max_value=1_000_010, allow_nan=False),
    st.integers(-1000, 1_000_010),
    st.integers(0, 10_000_000).map(lambda c: c / 10_000),  # up to 4 decimals
    st.integers(0, 1_000_000_000).map(lambda c: c / 1000),
)
non_numbers = st.one_of(st.booleans(), st.text(max_size=8), st.lists(st.integers(), max_size=2))
any_value = st.one_of(numbers, non_numbers)


# Feature: wattwise-household-model-improvements, Property 18: Billing-cycle consumption validation
@settings(max_examples=100, deadline=None)
@given(v=any_value)
def test_validator_accepts_iff_in_range_with_three_decimals(v):
    if _expected_valid(v):
        assert validate_billing_consumption(v) == float(v)
    else:
        with pytest.raises(ValueError) as exc:
            validate_billing_consumption(v)
        assert "0 to 1,000,000 kWh" in str(exc.value)


@settings(max_examples=100, deadline=None)
@given(v=valid_values)
def test_valid_values_accepted_by_request_models(v):
    assert DayRequest(billing_cycle_consumption_kwh=v).billing_cycle_consumption_kwh == v
    assert "billing_cycle_consumption_kwh" in ExperimentRequest.model_fields


@settings(max_examples=100, deadline=None)
@given(v=any_value.filter(lambda x: not _expected_valid(x)))
def test_request_models_reject_invalid_values(v):
    with pytest.raises(ValidationError):
        DayRequest(billing_cycle_consumption_kwh=v)


def test_boundaries_and_none():
    assert validate_billing_consumption(None) is None
    assert validate_billing_consumption(0) == 0.0
    assert validate_billing_consumption(1_000_000) == 1_000_000.0
    assert validate_billing_consumption(12.345) == 12.345
    for bad in (-0.001, 1_000_000.001, 1.2345, True, "5", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            validate_billing_consumption(bad)
    assert DayRequest().billing_cycle_consumption_kwh is None
