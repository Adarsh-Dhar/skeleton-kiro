"""Shared fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "trialforge.db")  # a per-test root database
