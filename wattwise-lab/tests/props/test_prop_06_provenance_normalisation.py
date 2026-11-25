"""Property 6: provenance normalisation and validation.

**Validates: Requirements 3.1, 3.2, 3.9**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.services.meter_library import PROVENANCE_LABELS, ProvenanceError, normalize_provenance

WS = " \t\n\r\f\v"
whitespace = st.text(alphabet=WS, max_size=8)
allowed = st.sampled_from(PROVENANCE_LABELS)
invalid = st.text(max_size=30).filter(lambda s: s.strip() != "" and s.strip() not in PROVENANCE_LABELS)


# Feature: wattwise-household-model-improvements, Property 6: Provenance normalisation and validation
@settings(max_examples=100, deadline=None)
@given(value=st.one_of(st.none(), whitespace))
def test_absent_empty_or_whitespace_yields_unknown(value):
    assert normalize_provenance(value) == "unknown"


# Feature: wattwise-household-model-improvements, Property 6: Provenance normalisation and validation
@settings(max_examples=100, deadline=None)
@given(label=allowed, pre=whitespace, post=whitespace)
def test_allowed_label_yields_itself(label, pre, post):
    assert normalize_provenance(label) == label
    assert normalize_provenance(pre + label + post) == label


# Feature: wattwise-household-model-improvements, Property 6: Provenance normalisation and validation
@settings(max_examples=100, deadline=None)
@given(value=invalid)
def test_other_values_rejected_with_message_listing_allowed(value):
    with pytest.raises(ProvenanceError) as exc:
        normalize_provenance(value)
    msg = str(exc.value)
    for label in PROVENANCE_LABELS:
        assert label in msg
