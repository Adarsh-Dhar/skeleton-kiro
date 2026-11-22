"""Data-source notices: every supported mode has its own notice; an unknown mode is an error, not 'synthetic'."""
from __future__ import annotations

import pytest

import f1lab
from f1lab import HISTORICAL_NOTICE, NOTICES, SIM_RECORDED_NOTICE, SYNTHETIC_NOTICE, notice_for


def test_each_supported_mode_has_its_notice():
    assert notice_for("synthetic") is SYNTHETIC_NOTICE
    assert notice_for("live_simulator") is SIM_RECORDED_NOTICE
    assert notice_for("historical") is HISTORICAL_NOTICE
    assert set(NOTICES) == {"synthetic", "live_simulator", "historical"}


def test_unknown_mode_is_rejected():
    for bad in ("live", "", "SYNTHETIC", None):
        with pytest.raises(ValueError, match="unknown data mode"):
            notice_for(bad)


def test_disclaimers_are_kept():
    assert "video-game data" in SIM_RECORDED_NOTICE and "not real Formula 1 telemetry" in SIM_RECORDED_NOTICE
    assert "unofficial" in HISTORICAL_NOTICE and "never used for setup optimisation" in HISTORICAL_NOTICE
    doc = f1lab.__doc__
    assert "no connection to any Formula 1 team or real telemetry" not in doc
    assert all(m in doc for m in ("synthetic", "live_simulator", "historical", "FastF1", "EA SPORTS F1 25"))


def test_meta_exposes_all_three_notices(client):
    assert client.get("/api/meta").json()["notices"] == NOTICES
