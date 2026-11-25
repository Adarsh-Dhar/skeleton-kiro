"""Property 8: Exactly one badge per label.
**Validates: Requirements 3.4, 3.5, 3.6**
"""
from __future__ import annotations
from hypothesis import given, settings
from hypothesis import strategies as st
from wattwise.services.meter_library import PROVENANCE_LABELS, provenance_badge

ALL_TEXTS = {provenance_badge(l)["text"] for l in PROVENANCE_LABELS}


# Feature: wattwise-household-model-improvements, Property 8: Exactly one badge per label
@settings(max_examples=100, deadline=None)
@given(label=st.sampled_from(PROVENANCE_LABELS))
def test_exactly_one_badge_per_label(label):
    badge = provenance_badge(label)
    assert set(badge) == {"text", "css"}
    assert isinstance(badge["text"], str) and badge["text"]
    assert isinstance(badge["css"], str) and badge["css"]
    # MEASURED iff real_meter
    assert (badge["text"] == "MEASURED") == (label == "real_meter")
    # each label maps to a distinct badge, so no other label's badge is shown
    others = [provenance_badge(o) for o in PROVENANCE_LABELS if o != label]
    assert all(o != badge for o in others)
    assert len(ALL_TEXTS) == len(PROVENANCE_LABELS)
