"""Task 7 (WATTWISE-K1): household settings persisted on the server."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from db import create_db_engine
from wattwise.api.app import create_app

MINE = {"tariff_id": "flat-rate", "base_load_kw": 0.5, "charger_power_kw": 7.4,
        "appliances": [{"name": "Heat pump", "power_kw": 2.0, "duration_h": 3, "flexible": True, "fixed_start": 20}],
        "ev": {"battery_kwh": 77, "initial_soc_pct": 20, "desired_charge_pct": 85, "departure_hour": 6.75, "efficiency": 0.9}}


def test_defaults_until_saved(client):
    r = client.get("/api/household").json()
    assert r["saved"] is False and r["source"] == "defaults"
    assert r["settings"]["tariff_id"] == "time-of-use" and len(r["settings"]["appliances"]) == 3


def test_put_persists_across_restart_and_clients(db_path):
    with TestClient(create_app(db_path)) as c:
        r = c.put("/api/household", json=MINE)
        assert r.status_code == 200 and r.json()["saved"] is True
    with TestClient(create_app(db_path)) as c2:  # a new process / another browser: same installation, same settings
        s = c2.get("/api/household").json()
        assert s["saved"] is True and s["source"] == "user"
        assert s["settings"]["ev"]["battery_kwh"] == 77 and s["settings"]["charger_power_kw"] == 7.4
        assert s["settings"]["appliances"][0]["name"] == "Heat pump" and s["settings"]["tariff_id"] == "flat-rate"
    with create_db_engine(db_path).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ww_household")).scalar() == 1


def test_validation_and_unknown_tariff(client):
    bad = {**MINE, "ev": {**MINE["ev"], "initial_soc_pct": 90, "desired_charge_pct": 80}}
    assert client.put("/api/household", json=bad).status_code == 422
    assert client.put("/api/household", json={**MINE, "base_load_kw": -1}).status_code == 422
    assert client.put("/api/household", json={**MINE, "tariff_id": "no-such-tariff"}).status_code == 404
    assert client.put("/api/household", json={**MINE, "baseline_import_id": "mi-missing"}).status_code == 404
    assert client.get("/api/household").json()["saved"] is False  # nothing stored by rejected requests


def test_reset(client):
    client.put("/api/household", json=MINE)
    r = client.post("/api/household/reset").json()
    assert r["saved"] is False and r["settings"]["tariff_id"] == "time-of-use"


def test_localstorage_migration_applies_only_once(client):
    r = client.put("/api/household", json={**MINE, "migrated_from": "localStorage"}).json()
    assert r["migration_applied"] is True and r["source"] == "migrated-localstorage"
    # a second browser with older local settings must not overwrite what the server already has
    other = {**MINE, "base_load_kw": 1.9, "migrated_from": "localStorage"}
    r = client.put("/api/household", json=other).json()
    assert r["migration_applied"] is False and r["settings"]["base_load_kw"] == 0.5
    # a normal save still replaces it
    assert client.put("/api/household", json={**MINE, "base_load_kw": 1.0}).json()["settings"]["base_load_kw"] == 1.0


def test_frontend_no_longer_writes_localstorage():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "frontend" / "app.js").read_text()
    assert "localStorage.setItem" not in js and "'/api/household'" in js
