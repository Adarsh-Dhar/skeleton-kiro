"""Acceptance tests for the zero-fabricated-input-data policy (one test per criterion of the requirement).

Every input is the household's own or comes from a cited source; missing data stays missing; projections are never measurements.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from conftest import wait_done
from fastapi.testclient import TestClient
from inputs import SRC, TARIFFS, appliance, household, inputs, meter_csv, retrieved_source, seed_tariffs, tariff
from sqlalchemy import text

from wattwise.api.app import create_app

OFAT = {"design": {"method": "ofat", "replicates": 1}, "simulator": {"latency_seconds": 0}}
OFFICIAL = {"source_type": "official", "source_name": "State electricity regulatory commission", "document": "Tariff order 2025-26, schedule LT-1",
            "collected_on": "2026-11-01", "effective_from": "2025-04-01", "effective_to": "2026-03-31", "verification": "verified"}


def test_fresh_installation_has_no_example_household_tariffs_or_meter_data(empty_client):
    c = empty_client
    assert c.get("/api/tariffs").json() == [] and c.get("/api/meter/imports").json() == [] and c.get("/api/experiments").json() == []
    h = c.get("/api/household").json()
    assert h["saved"] is False and h["settings"]["appliances"] == [] and h["settings"]["ev"] is None and h["settings"]["base_load_kw"] is None
    m = c.get("/api/meta").json()
    assert not {"baseline", "default_ev", "default_appliances"} & set(m) and "zero fabricated input data" in m["data_policy"]
    assert c.get("/api/demo/meter.csv").status_code == 404


def test_empty_household_requires_configuration(empty_client):
    r = empty_client.get("/api/household").json()["readiness"]
    assert r["ready"] is False and {m["field"] for m in r["missing"]} == {"tariff_id", "base_load_kw", "appliances"}
    assert empty_client.get("/api/overview").json()["baseline_day"] is None


def test_missing_tariff_blocks_projection_and_optimisation(client):
    body = inputs(name="x", tariff_id=None, **OFAT)
    r = client.post("/api/experiments", json=body)
    assert r.status_code == 422 and "a tariff" in r.json()["detail"][0]["message"]
    assert client.post("/api/simulate/day", json=inputs(tariff_id=None)).status_code == 422
    assert client.post("/api/meter/preview", json={"csv": meter_csv()}).status_code == 422


def test_missing_appliance_specifications_are_not_invented(client):
    no_power = {k: v for k, v in appliance("Washer").items() if k != "power_kw"}
    no_start = {k: v for k, v in appliance("Washer").items() if k != "fixed_start"}
    no_source = {k: v for k, v in appliance("Washer").items() if k != "source"}
    for a in (no_power, no_start, no_source):
        r = client.post("/api/simulate/day", json=inputs(appliances=[a], ev=None))
        assert r.status_code == 422, a
    assert client.put("/api/household", json=household(appliances=[no_power])).status_code == 422


def test_missing_ev_specifications_disable_the_ev(client):
    partial = {"battery_kwh": 40, "source": SRC}
    assert client.post("/api/simulate/day", json=inputs(ev=partial)).status_code == 422
    assert client.put("/api/household", json=household(ev=partial)).status_code == 422
    # an EV without its charger capacity is not projected either (no default charger power)
    no_charger = inputs()
    no_charger.pop("charger_capacity_kw")
    no_charger.pop("charger_source")
    r = client.post("/api/simulate/day", json=no_charger)
    assert r.status_code == 422 and "charger_capacity_kw" in r.json()["detail"][0]["message"]
    # an EV saved before plug-in times existed stays a draft: not used, not completed
    old = {"tariff_id": "time-of-use", "appliances": [], "ev": {"battery_kwh": 60.0, "initial_soc_pct": 30.0, "desired_charge_pct": 80.0, "departure_hour": 7.5, "efficiency": 0.92}}
    with client.app.state.lab.engine.begin() as c:
        c.execute(text("INSERT INTO ww_household (id, settings_json, source, updated_at) VALUES (1, :j, 'user', CURRENT_TIMESTAMP)"), {"j": json.dumps(old)})
    h = client.get("/api/household").json()
    assert h["settings"]["ev"] is None and h["ev_draft"]["missing"] == ["plug_in_hour"]


def test_real_meter_csv_keeps_its_readings_and_provenance(client):
    record = {"source_type": "meter_export", "source_name": "DISCOM consumer portal", "document": "interval data export", "collected_on": "2026-11-20",
              "verification": "user_supplied"}
    m = client.post("/api/meter/import", json={"csv": meter_csv(), "file_name": "export.csv", "source": "DISCOM consumer portal", "provenance": "real_meter",
                                               "meter_source": record}).json()
    assert m["rows_used"] == 96 and m["source_label"] == "real_meter"
    assert m["source_record"]["collected_on"] == "2026-11-20" and m["source_status"]["label"] == "USER SUPPLIED"
    rd = client.get(f"/api/meter/imports/{m['import_id']}/readings", params={"limit": 1}).json()["readings"][0]
    assert rd["original_ts"] == "2026-03-03T16:00:00" and rd["kw"] == 0.397  # the reading exactly as in the file


def test_a_meter_export_is_never_reported_as_independently_verified(client):
    claim = {"source_type": "meter_export", "source_name": "portal", "document": "x", "collected_on": "2026-11-20", "verification": "verified"}
    r = client.post("/api/meter/import", json={"csv": meter_csv(), "source": "portal", "provenance": "real_meter", "meter_source": claim})
    assert r.status_code == 422 and "user_supplied" in r.json()["detail"][0]["message"]
    unknown = client.post("/api/meter/import", json={"csv": meter_csv(), "source": "a friend", "provenance": "unknown"}).json()
    assert unknown["source_record"]["verification"] == "unverified" and unknown["source_status"]["label"] == "UNVERIFIED"


def test_invalid_or_untraceable_sources_are_labelled_unverified_or_rejected(client):
    r = client.post("/api/tariffs", json=tariff("No source", [{"start": 0, "end": 24, "price": 0.2}], source=None))
    assert r.status_code == 422
    r = client.post("/api/tariffs", json=tariff("Example claim", [{"start": 0, "end": 24, "price": 0.2}], source={**SRC, "source_type": "example", "verification": "unverified"}))
    assert r.status_code == 422
    loose = {"source_type": "unverified", "source_name": "a forum post", "verification": "unverified"}
    t = client.post("/api/tariffs", json=tariff("Forum rates", [{"start": 0, "end": 24, "price": 0.2}], source=loose)).json()
    assert t["source_status"]["label"] == "UNVERIFIED" and any("unverified" in w for w in t["source_status"]["warnings"])


def test_missing_source_date_cannot_claim_verified(client):
    no_date = {k: v for k, v in OFFICIAL.items() if k != "collected_on"}
    r = client.post("/api/tariffs", json=tariff("Order", [{"start": 0, "end": 24, "price": 5.5}], source=no_date))
    assert r.status_code == 422 and "collected_on" in r.json()["detail"][0]["message"]
    future = {**OFFICIAL, "collected_on": (date.today() + timedelta(days=3)).isoformat()}
    assert client.post("/api/tariffs", json=tariff("Order", [{"start": 0, "end": 24, "price": 5.5}], source=future)).status_code == 422
    ok = client.post("/api/tariffs", json=tariff("Order", [{"start": 0, "end": 24, "price": 5.5}], source=retrieved_source(client))).json()
    assert ok["source_status"]["label"] == "VERIFIED · DOCUMENT RETRIEVED"


def test_expired_tariff_is_flagged(client):
    src = retrieved_source(client, effective_to="2026-03-31")
    t = client.post("/api/tariffs", json=tariff("Last year's order", [{"start": 0, "end": 24, "price": 5.5}], source=src)).json()
    st = t["source_status"]
    assert st["expired"] is True and st["label"] == "VERIFIED · DOCUMENT RETRIEVED · EXPIRED" and any("expired" in w for w in st["warnings"])
    client.put("/api/household", json=household(tariff_id=t["id"]))
    warnings = client.get("/api/household").json()["readiness"]["warnings"]
    assert any("expired" in w for w in warnings)


def test_example_tariffs_from_earlier_versions_are_never_used(client):
    now = datetime.now(UTC)
    from db.models.wattwise import Tariff
    old = {"id": "flat-rate-old", "name": "Old flat", "demand_charge_per_kw": 0.0, "periods": [{"label": "All day", "start": 0, "end": 24, "price": 0.25}]}
    with client.app.state.lab.engine.begin() as c:
        c.execute(Tariff.__table__.delete().where(Tariff.tariff_id == "flat-rate"))
        c.execute(Tariff.__table__.insert().values(tariff_id="flat-rate", name="Flat rate", position=9, created_at=now, updated_at=now,
                                                   data_json=json.dumps({**old, "id": "flat-rate", "name": "Flat rate"})))
    t = next(x for x in client.get("/api/tariffs").json() if x["id"] == "flat-rate")
    assert t["source"]["source_type"] == "example" and t["source_status"]["usable"] is False
    r = client.post("/api/simulate/day", json=inputs(tariff_id="flat-rate"))
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "source" and "built-in example" in r.json()["detail"][0]["message"]
    client.put("/api/household", json=household(tariff_id="flat-rate"))
    assert client.get("/api/household").json()["readiness"]["ready"] is False


def test_application_restart_preserves_every_imported_and_configured_input(db_path):
    with TestClient(create_app(db_path)) as c:
        seed_tariffs(c)
        c.put("/api/household", json=household())
        m = c.post("/api/meter/import", json={"csv": meter_csv(), "source": "portal", "provenance": "real_meter"}).json()
        before = c.get("/api/inputs/export.json").json()
    with TestClient(create_app(db_path)) as c2:
        after = c2.get("/api/inputs/export.json").json()
        assert after["inputs"] == before["inputs"] and any(i["input"] == f"meter_import[{m['import_id']}]" for i in after["inputs"])
        assert {t["id"] for t in c2.get("/api/tariffs").json()} == {"time-of-use", "flat-rate", "night-saver-demand"}


def test_export_keeps_the_provenance_of_every_input(client):
    client.put("/api/household", json=household())
    client.post("/api/meter/import", json={"csv": meter_csv(), "source": "portal", "provenance": "real_meter"})
    x = client.get("/api/inputs/export.json").json()
    kinds = {i["input"].split("[")[0] for i in x["inputs"]}
    assert kinds == {"tariff", "ev", "charger", "appliances", "base_load", "meter_import"}
    for i in x["inputs"]:
        assert i["source"] and i["source"]["source_name"] and i["status"]["label"], i
    ev_row = next(i for i in x["inputs"] if i["input"] == "ev")
    assert ev_row["units"]["battery_kwh"] == "kWh" and ev_row["values"]["battery_kwh"] == 60.0


def test_experiment_report_separates_inputs_from_projected_results(client):
    d = client.post("/api/experiments", json=inputs(name="report", **OFAT)).json()
    client.post(f"/api/experiments/{d['id']}/execute")
    wait_done(client, d["id"])
    rep = client.get(f"/api/experiments/{d['id']}/report").json()
    assert rep["result_kind"] == "projected" and {i["input"] for i in rep["input_sources"]} >= {"tariff", "ev", "charger", "base_load"}
    assert all(i["source"]["source_name"] == SRC["source_name"] for i in rep["input_sources"])
    html = client.get(f"/api/experiments/{d['id']}/report.html").text
    assert html.index("Inputs (your data and cited sources)") < html.index("Recommendation (projected, not measured)")
    run = client.get(f"/api/experiments/{d['id']}/runs").json()[0]
    ms = client.get(f"/api/experiments/{d['id']}/export/measurements.csv").text
    assert "(projection)" in ms  # each model result names the model that projected it
    assert run["imported"] is False


def test_every_experiment_keeps_a_snapshot_of_its_input_sources(client):
    d = client.post("/api/experiments", json=inputs(name="snapshot", **OFAT)).json()
    snap = d["input_sources"]
    tariff_row = next(i for i in snap if i["input"] == "tariff")
    assert tariff_row["source"]["source_name"] == SRC["source_name"]
    # changing the tariff later does not change the stored snapshot
    client.put("/api/tariffs/time-of-use", json=tariff("Time-of-use", [{"start": 0, "end": 24, "price": 9.0}], source=retrieved_source(client)))
    again = client.get(f"/api/experiments/{d['id']}").json()["input_sources"]
    assert again == snap


def test_projections_add_no_noise_and_are_reproducible(client):
    d = client.post("/api/experiments", json=inputs(name="det", **{**OFAT, "design": {"method": "ofat", "replicates": 2}})).json()
    client.post(f"/api/experiments/{d['id']}/execute")
    done = wait_done(client, d["id"])
    by_cfg: dict = {}
    for r in done["runs"]:
        by_cfg.setdefault(json.dumps(r["configuration"], sort_keys=True), []).append(r["outcomes"]["energy_cost"])
    assert all(len(set(v)) == 1 for v in by_cfg.values())  # replicates of one schedule give the same projection


def test_legacy_experiments_on_example_inputs_are_flagged_and_cannot_be_run(client, db_path):
    d = client.post("/api/experiments", json=inputs(name="legacy", **OFAT)).json()
    lab = client.app.state.lab
    spec = lab.repo.get_spec(d["id"])
    meta = dict(spec.metadata)
    meta["lab"] = {k: v for k, v in meta["lab"].items() if k != "input_sources"}  # what an experiment from an earlier version looks like
    with lab.engine.begin() as c:
        row = c.execute(text("SELECT spec_json FROM experiments WHERE experiment_id = :i"), {"i": d["id"]}).scalar()
        stored = json.loads(row)
        stored["metadata"] = meta
        c.execute(text("UPDATE experiments SET spec_json = :s WHERE experiment_id = :i"), {"s": json.dumps(stored), "i": d["id"]})
    with TestClient(create_app(db_path)) as c2:  # a restart reads the stored experiment as an earlier version left it
        got = c2.get(f"/api/experiments/{d['id']}").json()
        assert got["legacy_inputs"] is True and got["baseline_cost"] is None and "example inputs" in got["legacy_notice"]
        r = c2.post(f"/api/experiments/{d['id']}/execute")
        assert r.status_code == 409 and "example inputs" in r.json()["detail"][0]["message"]
        assert c2.get("/api/overview").status_code == 200


def test_clear_configuration_keeps_a_restorable_backup(client):
    client.put("/api/household", json=household())
    cleared = client.post("/api/household/reset").json()
    assert cleared["settings"]["appliances"] == [] and cleared["settings"]["tariff_id"] is None  # nothing is filled in
    restored = client.post("/api/household/restore").json()
    assert restored["settings"]["appliances"][0]["name"] == "Dishwasher" and restored["readiness"]["ready"] is True


def test_tariff_fields_for_an_official_record_round_trip(client):
    body = tariff("LT-1 Domestic", [{"label": "all", "start": 0, "end": 24, "price": 5.9}], source=retrieved_source(client, document=OFFICIAL["document"]),
                  utility="Example DISCOM", consumer_category="LT-1 Domestic", currency="INR", fixed_monthly_charge=110.0, tax_pct=9)
    t = client.post("/api/tariffs", json=body).json()
    assert (t["utility"], t["consumer_category"], t["currency"]) == ("Example DISCOM", "LT-1 Domestic", "INR")
    assert t["source"]["document"] == OFFICIAL["document"] and t["source"]["effective_from"] == "2025-04-01"
    assert TARIFFS  # the test fixtures are used, never shipped
