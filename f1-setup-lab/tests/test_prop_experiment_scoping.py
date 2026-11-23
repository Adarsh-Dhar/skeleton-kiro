"""Property 13: experiment listing is scoped to F1 SETUP LAB."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from db import create_db_engine
from f1lab.api.app import create_app

F1, WW = "f1-setup-lab", "wattwise-lab"

# owner of each SDK experiment, and owner rows with no SDK experiment behind them
sdk_owners = st.lists(st.sampled_from([F1, WW]), min_size=0, max_size=4)
orphan_owners = st.lists(st.sampled_from([F1, WW]), min_size=0, max_size=3)


# Feature: f1-setup-lab-restoration, Property 13: Experiment listing is scoped to F1 SETUP LAB
# **Validates: Requirements 6.6**
@settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))
@given(owners=sdk_owners, orphans=orphan_owners)
def test_experiment_listing_returns_only_f1_owned(owners, orphans):
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "trialforge.db")
        with TestClient(create_app(db)) as client:
            body = {"name": "scoped", "hypothesis": "h", "design": {"replicates": 1}, "simulator": {"latency_seconds": 0.0}}
            ids = []
            for n in range(len(owners)):
                r = client.post("/api/experiments", json={**body, "experiment_id": f"exp-{n:03d}"})
                assert r.status_code == 201, r.text
                ids.append(f"exp-{n:03d}")

            with create_db_engine(db).begin() as c:
                for exp_id, owner in zip(ids, owners):
                    c.execute(text("UPDATE app_experiment_owner SET app=:a WHERE experiment_id=:i"), {"a": owner, "i": exp_id})
                for n, owner in enumerate(orphans):
                    c.execute(
                        text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES (:i, :a, CURRENT_TIMESTAMP)"),
                        {"i": f"orphan-{n:03d}", "a": owner},
                    )

            listed = [e["id"] for e in client.get("/api/experiments").json()]

    expected = {i for i, o in zip(ids, owners) if o == F1}
    wattwise = {i for i, o in zip(ids, owners) if o == WW} | {f"orphan-{n:03d}" for n, o in enumerate(orphans) if o == WW}
    assert set(listed) == expected
    assert len(listed) == len(expected)
    assert not set(listed) & wattwise
