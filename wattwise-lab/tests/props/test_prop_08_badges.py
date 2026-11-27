"""Property 8: Exactly one badge per label.
**Validates: Requirements 3.4, 3.5, 3.6**
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.services.meter_library import PROVENANCE_LABELS, provenance_badge

ALL_TEXTS = {provenance_badge(label)["text"] for label in PROVENANCE_LABELS}


# Feature: wattwise-household-model-improvements, Property 8: Exactly one badge per label
@settings(max_examples=100, deadline=None)
@given(label=st.sampled_from(PROVENANCE_LABELS))
def test_exactly_one_badge_per_label(label):
    badge = provenance_badge(label)
    assert set(badge) == {"text", "css"}
    assert isinstance(badge["text"], str) and badge["text"]
    assert isinstance(badge["css"], str) and badge["css"]
    # "meter export" iff real_meter, and always shown as the owner's declaration (WATTWISE cannot prove a file came from a meter)
    assert badge["text"].startswith("METER EXPORT") == (label == "real_meter") and "VERIFIED" not in badge["text"].replace("UNVERIFIED", "")
    assert label != "real_meter" or "DECLARED BY YOU" in badge["text"]
    # each label maps to a distinct badge, so no other label's badge is shown
    others = [provenance_badge(o) for o in PROVENANCE_LABELS if o != label]
    assert all(o != badge for o in others)
    assert len(ALL_TEXTS) == len(PROVENANCE_LABELS)
