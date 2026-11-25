"""Property 2: the charging range follows the charger capacity.

**Validates: Requirements 1.2, 1.6, 1.7**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import household_model as HM

EPS = 1e-9  # float rounding in the linspace
capacities = st.integers(140, 35000).map(lambda c: c / 100)  # 1.40 to 350.00 kW, 2 decimals


def _levels(low: float, high: float, n: int) -> list[float]:
    return [low + (high - low) * i / (n - 1) for i in range(n)]


# Feature: wattwise-household-model-improvements, Property 2: Charging range follows capacity
@settings(max_examples=100, deadline=None)
@given(capacity=capacities, n=st.integers(2, 12))
def test_charging_limits_follow_capacity(capacity, n):
    f = HM.factor_limits(capacity, [], True)["charge_power_kw"]
    assert f["low"] == HM.CHARGER_MIN_KW == 1.4
    assert f["high"] == capacity
    for level in _levels(f["low"], f["high"], n):
        assert 1.4 - EPS <= level <= capacity + EPS


# Feature: wattwise-household-model-improvements, Property 2: Charging range follows capacity
@settings(max_examples=100, deadline=None)
@given(n=st.integers(2, 12))
def test_charging_limits_default_without_capacity(n):
    f = HM.factor_limits(None)["charge_power_kw"]
    assert (f["low"], f["high"]) == (1.4, 11.0)
    for level in _levels(f["low"], f["high"], n):
        assert 1.4 - EPS <= level <= 11.0 + EPS
