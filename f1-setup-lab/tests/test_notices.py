"""Data-source notices: every supported mode has its own notice; an unknown mode is an error, not 'synthetic'.
The synthetic notice keeps the archive wording minus the removed recorded-stint feature; the historical notice keeps the OpenF1 wording."""
from __future__ import annotations

import pytest

import f1lab
from f1lab import HISTORICAL_NOTICE, NOTICES, SYNTHETIC_NOTICE, notice_for

EXPECTED_SYNTHETIC = (
    "SYNTHETIC DATA: the vehicle model is an invented response surface. This is not real Formula 1 telemetry, "
    "and synthetic experiments use no car or timing data."
)


def test_each_supported_mode_has_its_notice():
    assert notice_for("synthetic") is SYNTHETIC_NOTICE
    assert notice_for("historical") is HISTORICAL_NOTICE
    assert set(NOTICES) == {"synthetic", "historical"}


def test_unknown_mode_is_rejected():
    for bad in ("live", "", "SYNTHETIC", None):
        with pytest.raises(ValueError, match="unknown data mode"):
            notice_for(bad)


def test_synthetic_notice_text():
    assert NOTICES["synthetic"] == EXPECTED_SYNTHETIC


def test_disclaimers_are_kept():
    assert "OpenF1" in HISTORICAL_NOTICE and "unofficial" in HISTORICAL_NOTICE and "FastF1" not in HISTORICAL_NOTICE
    doc = f1lab.__doc__
    assert "no connection to any Formula 1 team or real telemetry" not in doc
    assert all(m in doc for m in ("synthetic", "historical", "OpenF1"))


def test_meta_exposes_both_notices(client):
    assert client.get("/api/meta").json()["notices"] == NOTICES
