"""Property 28: provenance labels and flags are a pure function of source."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from f1lab.adapters.telemetry import ParsedTelemetry, to_measurements
from f1lab.integrations.historical.importer import HistoricalImportError
from f1lab.integrations.historical.providers import (
    DEMO_SOURCES,
    PROVENANCE_DEMO,
    PROVENANCE_OPENF1,
    PROVENANCE_USER,
    guard_synthetic,
    provenance_status,
)

USER_SOURCES = ["csv_upload", "user_uploaded", "upload"]


def _variant(name: str):
    """A source spelling with arbitrary case and surrounding whitespace."""
    return st.tuples(st.booleans(), st.text(" \t", max_size=2), st.text(" \t", max_size=2)).map(
        lambda t: f"{t[1]}{name.upper() if t[0] else name}{t[2]}"
    )


demo_source = st.sampled_from(sorted(DEMO_SOURCES)).flatmap(_variant)
user_source = st.sampled_from(USER_SOURCES).flatmap(_variant)
openf1_source = _variant("openf1")


# Feature: f1-unified-telemetry-import, Property 28: Provenance labels and flags are a pure function of source
# **Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5, 13.6**
@settings(max_examples=100, deadline=None)
@given(src=openf1_source)
def test_openf1_label(src):
    assert provenance_status(src) == PROVENANCE_OPENF1 == "OPENF1 — external provider"


@settings(max_examples=100, deadline=None)
@given(src=user_source)
def test_user_upload_label_and_flags(src):
    assert provenance_status(src) == PROVENANCE_USER == "USER UPLOADED — UNVERIFIED"
    # user data may carry synthetic=False; guard leaves it unchanged
    assert guard_synthetic(src, False) is False
    parsed = ParsedTelemetry(laps=[{"lap": 1, "sector_1": 30.0, "sector_2": 30.0, "sector_3": 30.0, "lap_time": 90.0}])
    ms = to_measurements("r1", parsed, src)
    assert ms
    for m in ms:
        assert m.metadata["synthetic"] is False and m.metadata["imported"] is True


@settings(max_examples=100, deadline=None)
@given(src=demo_source)
def test_demo_label_and_synthetic_guard(src):
    assert provenance_status(src) == PROVENANCE_DEMO == "SYNTHETIC DEMO — generated test data"
    assert guard_synthetic(src, True) is True
    with pytest.raises(HistoricalImportError) as exc:
        guard_synthetic(src, False)
    assert "synthetic" in str(exc.value).lower()


@settings(max_examples=100, deadline=None)
@given(src=st.one_of(openf1_source, user_source, demo_source), synthetic=st.booleans())
def test_label_is_pure_function_of_source(src, synthetic):
    first = provenance_status(src)
    assert provenance_status(src) == first
    assert first in {PROVENANCE_OPENF1, PROVENANCE_USER, PROVENANCE_DEMO}
    # the label never depends on the synthetic flag; only demo sources constrain it
    if first == PROVENANCE_DEMO and not synthetic:
        with pytest.raises(HistoricalImportError):
            guard_synthetic(src, synthetic)
    else:
        assert guard_synthetic(src, synthetic) is synthetic
