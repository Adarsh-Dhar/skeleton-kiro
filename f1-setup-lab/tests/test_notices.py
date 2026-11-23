"""Data-source notices: every supported mode has its own notice; an unknown mode is an error, not 'synthetic'.

Design D2: synthetic and live_simulator notices equal the archive text; the historical notice keeps the OpenF1 wording."""
from __future__ import annotations

import pytest

import f1lab
from f1lab import HISTORICAL_NOTICE, NOTICES, SIM_RECORDED_NOTICE, SYNTHETIC_NOTICE, notice_for

ARCHIVE_SYNTHETIC = (
    "SYNTHETIC DATA: the vehicle model is an invented response surface. This is not real Formula 1 telemetry, "
    "and synthetic experiments use no game, car or timing data."
)
ARCHIVE_SIM_RECORDED = (
    "SIMULATOR-RECORDED DATA: laps were driven by one person in EA SPORTS F1 25 on one track and setups were read from the game's UDP output. "
    "This is video-game data, not real Formula 1 telemetry, and the findings say nothing about a real car or other drivers."
)


def test_each_supported_mode_has_its_notice():
    assert notice_for("synthetic") is SYNTHETIC_NOTICE
    assert notice_for("live_simulator") is SIM_RECORDED_NOTICE
    assert notice_for("historical") is HISTORICAL_NOTICE
    assert set(NOTICES) == {"synthetic", "live_simulator", "historical"}


def test_unknown_mode_is_rejected():
    for bad in ("live", "", "SYNTHETIC", None):
        with pytest.raises(ValueError, match="unknown data mode"):
            notice_for(bad)


def test_synthetic_and_simulator_notices_equal_the_archive():
    assert NOTICES["synthetic"] == ARCHIVE_SYNTHETIC
    assert NOTICES["live_simulator"] == ARCHIVE_SIM_RECORDED


def test_disclaimers_are_kept():
    assert "video-game data" in SIM_RECORDED_NOTICE and "not real Formula 1 telemetry" in SIM_RECORDED_NOTICE
    assert "OpenF1" in HISTORICAL_NOTICE and "unofficial" in HISTORICAL_NOTICE and "FastF1" not in HISTORICAL_NOTICE
    doc = f1lab.__doc__
    assert "no connection to any Formula 1 team or real telemetry" not in doc
    assert all(m in doc for m in ("synthetic", "live_simulator", "historical", "OpenF1", "EA SPORTS F1 25"))


def test_meta_exposes_all_three_notices(client):
    assert client.get("/api/meta").json()["notices"] == NOTICES
