"""Household settings persisted on the server, under the zero-fabricated-input-data policy."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from inputs import SRC, appliance, ev, household, seed_tariffs
from sqlalchemy import text

from db import create_db_engine
from wattwise.api.app import create_app

MINE = household(tariff_id="flat-rate", base_load_kw=0.5, charger_power_kw=7.4,
                 appliances=[appliance("Heat pump", power_kw=2.0, duration_h=3, fixed_start=20)],
                 ev=ev(battery_kwh=77, initial_soc_pct=20, desired_charge_pct=85, departure_hour=6.75, efficiency=0.9))


def test_a_fresh_installation_has_no_household_and_no_tariffs(empty_client):
    r = empty_client.get("/api/household").json()
    assert r["saved"] is False and r["source"] == "empty"
    s = r["settings"]
    assert s["tariff_id"] is None and s["appliances"] == [] and s["ev"] is None and s["base_load_kw"] is None and s["charger_power_kw"] is None
    assert not r["readiness"]["ready"] and {m["field"] for m in r["readiness"]["missing"]} == {"tariff_id", "base_load_kw", "appliances"}
    assert empty_client.get("/api/tariffs").json() == [] and empty_client.get("/api/meter/imports").json() == []
    assert empty_client.get("/api/overview").json()["baseline_day"] is None


def test_an_empty_household_cannot_be_simulated(empty_client):
    r = empty_client.post("/api/simulate/day", json={})
    assert r.status_code == 422
    ex = empty_client.post("/api/experiments", json={"name": "x", "appliances": [appliance("a")]})
    assert ex.status_code == 422 and "missing input data" in ex.json()["detail"][0]["message"] and "tariff" in ex.json()["detail"][0]["message"]


def test_put_persists_across_restart_and_clients(db_path):
    with TestClient(create_app(db_path)) as c:
        seed_tariffs(c)
        r = c.put("/api/household", json=MINE)
        assert r.status_code == 200 and r.json()["saved"] is True and r.json()["readiness"]["ready"] is True
    with TestClient(create_app(db_path)) as c2:  # a new process / another browser: same installation, same settings
        s = c2.get("/api/household").json()
        assert s["saved"] is True and s["source"] == "user"
        assert s["settings"]["ev"]["battery_kwh"] == 77 and s["settings"]["charger_power_kw"] == 7.4
        assert s["settings"]["appliances"][0]["name"] == "Heat pump" and s["settings"]["tariff_id"] == "flat-rate"
        assert s["settings"]["appliances"][0]["source"]["source_name"] == SRC["source_name"]  # provenance survives the restart
        assert s["current_schedule"] == {"charge_start": 19.0, "charge_power_kw": 7.4, "appliance_start__heat_pump": 20.0}
    with create_db_engine(db_path).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ww_household")).scalar() == 1


def test_validation_and_unknown_tariff(client):
    bad = {**MINE, "ev": {**MINE["ev"], "initial_soc_pct": 90, "desired_charge_pct": 80}}
    assert client.put("/api/household", json=bad).status_code == 422
    assert client.put("/api/household", json={**MINE, "base_load_kw": -1}).status_code == 422
    assert client.put("/api/household", json={**MINE, "tariff_id": "no-such-tariff"}).status_code == 404
    assert client.put("/api/household", json={**MINE, "baseline_import_id": "mi-missing"}).status_code == 404
    assert client.get("/api/household").json()["saved"] is False  # nothing stored by rejected requests


def test_every_value_needs_its_source(client):
    no_ev_src = {**MINE, "ev": {k: v for k, v in MINE["ev"].items() if k != "source"}}
    no_app_src = {**MINE, "appliances": [{k: v for k, v in MINE["appliances"][0].items() if k != "source"}]}
    no_base_src = {**MINE, "base_load_source": None}
    no_charger_src = {**MINE, "charger_source": None}
    for body in (no_ev_src, no_app_src, no_base_src, no_charger_src):
        assert client.put("/api/household", json=body).status_code == 422
    # an EV needs every value from the owner or the vehicle documents: nothing is filled in
    partial_ev = {**MINE, "ev": {"battery_kwh": 60, "source": SRC}}
    r = client.put("/api/household", json=partial_ev)
    assert r.status_code == 422 and {i["field"] for i in r.json()["detail"]} >= {"ev.plug_in_hour", "ev.efficiency", "ev.departure_hour"}


def test_owner_data_cannot_claim_to_be_verified(client):
    claim = {**MINE, "base_load_source": {**SRC, "verification": "verified", "document": "my notes", "collected_on": "2026-01-01"}}
    r = client.put("/api/household", json=claim)
    assert r.status_code == 422 and "user_supplied" in r.json()["detail"][0]["message"]
    official = {"source_type": "manufacturer", "source_name": "Maker", "verification": "verified"}  # no document, no date
    r = client.put("/api/household", json={**MINE, "ev": {**MINE["ev"], "source": official}})
    assert r.status_code == 422 and "collected" in r.json()["detail"][0]["message"] and "document" in r.json()["detail"][0]["message"]
    ok = {**official, "document": "https://maker.example/spec.pdf", "collected_on": "2026-11-01"}
    got = client.put("/api/household", json={**MINE, "ev": {**MINE["ev"], "source": ok}}).json()
    assert got["settings"]["ev"]["source"]["verification"] == "verified"


def test_clear_keeps_a_backup_that_can_be_restored(client):
    client.put("/api/household", json=MINE)
    r = client.post("/api/household/reset").json()
    assert r["saved"] is False and r["settings"]["tariff_id"] is None and r["settings"]["appliances"] == [] and r["backup"]["available"] is True
    back = client.post("/api/household/restore").json()
    assert back["saved"] is True and back["settings"]["appliances"][0]["name"] == "Heat pump" and back["settings"]["ev"]["battery_kwh"] == 77


def test_restore_without_a_backup_is_404(client):
    assert client.post("/api/household/restore").status_code == 404


def test_localstorage_migration_applies_only_once(client):
    r = client.put("/api/household", json={**MINE, "migrated_from": "localStorage"}).json()
    assert r["migration_applied"] is True and r["source"] == "migrated-localstorage"
    # a second browser with older local settings must not overwrite what the server already has
    other = {**MINE, "base_load_kw": 1.9, "migrated_from": "localStorage"}
    r = client.put("/api/household", json=other).json()
    assert r["migration_applied"] is False and r["settings"]["base_load_kw"] == 0.5
    # a normal save still replaces it
    assert client.put("/api/household", json={**MINE, "base_load_kw": 1.0}).json()["settings"]["base_load_kw"] == 1.0


def test_settings_saved_before_source_tracking_are_kept_and_labelled_not_completed(client):
    old = {"tariff_id": "flat-rate", "base_load_kw": 0.4, "charger_power_kw": None,
           "appliances": [{"name": "Dishwasher", "power_kw": 1.2, "duration_h": 2.0, "flexible": True, "fixed_start": 20.0}],
           "ev": {"battery_kwh": 60.0, "initial_soc_pct": 30.0, "desired_charge_pct": 80.0, "departure_hour": 7.5, "efficiency": 0.92}}
    with client.app.state.lab.engine.begin() as c:
        c.execute(text("INSERT INTO ww_household (id, settings_json, source, updated_at) VALUES (1, :j, 'user', CURRENT_TIMESTAMP)"), {"j": json.dumps(old)})
    r = client.get("/api/household").json()
    s = r["settings"]
    assert s["appliances"][0]["power_kw"] == 1.2 and s["appliances"][0]["source"]["legacy"] is True
    assert s["base_load_kw"] == 0.4 and s["base_load_source"]["verification"] == "user_supplied"
    # the old EV has no usual plug-in time: it is NOT completed with an invented one, it becomes a draft
    assert s["ev"] is None and r["ev_draft"]["battery_kwh"] == 60.0 and r["ev_draft"]["missing"] == ["plug_in_hour"]
    assert "plug_in_hour" in r["notice"]


def test_frontend_no_longer_writes_localstorage():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "frontend" / "app.js").read_text()
    assert "localStorage.setItem" not in js and "'/api/household'" in js
