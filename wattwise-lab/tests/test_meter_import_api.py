"""Meter CSV preview and import through the HTTP API."""

from __future__ import annotations

import pytest
from wattwise.adapters.household_model import DEFAULT_TARIFFS, summarise

CSV_HEAD = "timestamp,power_kw\n"


def quarter_hours(values, start_hour=16):
    rows = []
    for i, v in enumerate(values):
        t = start_hour * 60 + 15 * i
        rows.append(f"2026-03-0{3 + t // 1440}T{(t // 60) % 24:02d}:{t % 60:02d}:00,{v}")
    return CSV_HEAD + "\n".join(rows) + "\n"


def demo_readings(client):
    text = client.get("/api/demo/meter.csv").text
    return [(16 + 0.25 * i, float(line.split(",")[1])) for i, line in enumerate(text.strip().splitlines()[1:])]


def test_demo_files_are_served_and_unknown_names_404(client):
    assert client.get("/api/demo/meter.csv").text.startswith("timestamp,power_kw")
    assert client.get("/api/demo/meter_w.csv").text.startswith("timestamp,power_w")
    assert client.get("/api/demo/other.csv").status_code == 404


def test_preview_prices_the_readings_with_the_chosen_tariff(client):
    csv_text = client.get("/api/demo/meter.csv").text
    p = client.post("/api/meter/preview", json={"csv": csv_text, "tariff_id": "time-of-use"}).json()
    expected = summarise(demo_readings(client), DEFAULT_TARIFFS[0])
    s = p["summary"]
    assert s["energy_kwh"] == pytest.approx(expected["energy_kwh"], rel=1e-6) and s["energy_cost"] == pytest.approx(expected["energy_cost"], rel=1e-6)
    assert s["peak_power_kw"] == pytest.approx(expected["peak_power_kw"]) and s["readings"] == 96 and s["interval_minutes"] == 15 and s["hours_covered"] == 24
    assert len(s["hourly_profile"]) == 24 and p["reading_kind"] == "imported" and p["quality"]["invalid"] == 0
    flat = client.post("/api/meter/preview", json={"csv": csv_text, "tariff_id": "flat-rate"}).json()["summary"]
    assert flat["energy_cost"] == pytest.approx(flat["energy_kwh"] * 0.25, rel=1e-6) and flat["energy_cost"] != s["energy_cost"]


def test_watts_and_kilowatts_give_the_same_result_and_report_conversion(client):
    kw = client.post("/api/meter/preview", json={"csv": client.get("/api/demo/meter.csv").text}).json()
    w = client.post("/api/meter/preview", json={"csv": client.get("/api/demo/meter_w.csv").text, "unit": "W"}).json()
    assert w["summary"]["energy_kwh"] == pytest.approx(kw["summary"]["energy_kwh"], rel=2e-3)
    assert w["summary"]["peak_power_kw"] == pytest.approx(kw["summary"]["peak_power_kw"], rel=2e-3)
    assert w["quality"]["unit_converted"] > 0 and kw["quality"]["unit_converted"] == 0


def test_inline_tariff_can_be_used_without_saving_it(client):
    t = {"name": "Inline", "demand_charge_per_kw": 1.0, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.5}]}
    s = client.post("/api/meter/preview", json={"csv": client.get("/api/demo/meter.csv").text, "tariff": t}).json()["summary"]
    assert s["demand_charge"] == pytest.approx(s["peak_power_kw"]) and s["energy_charge"] == pytest.approx(s["energy_kwh"] * 0.5, rel=1e-6)
    assert all(x["name"] != "Inline" for x in client.get("/api/tariffs").json())


def test_bad_rows_are_reported_but_good_rows_are_used(client):
    text = CSV_HEAD + "2026-03-03T16:00:00,1\nnot-a-time,2\n2026-03-03T16:15:00,x\n2026-03-03T16:30:00,3\n"
    p = client.post("/api/meter/preview", json={"csv": text}).json()
    assert p["summary"]["readings"] == 2 and len(p["errors"]) == 2


def test_duplicates_and_disorder_are_flagged(client):
    text = CSV_HEAD + "2026-03-03T16:00:00,1\n2026-03-03T16:15:00,1\n2026-03-03T16:15:00,1\n2026-03-03T16:45:00,1\n2026-03-03T16:30:00,1\n"
    q = client.post("/api/meter/preview", json={"csv": text}).json()["quality"]
    assert q["duplicates"] + q["out_of_order"] > 0


@pytest.mark.parametrize(
    "body,fragment",
    [
        ({"csv": "time,watts\n1,2\n"}, "required columns"),
        ({"csv": CSV_HEAD}, "no data rows"),
        ({"csv": quarter_hours([1, 1]).replace("T16:15", "T18:15")}, "maximum supported interval"),
        ({"csv": CSV_HEAD + "2026-03-03T16:00:00,1\n", "unit": "MW"}, "kW"),
        ({"csv": CSV_HEAD + "2026-03-03T16:00:00,1\n", "tariff_id": "nope"}, "not found"),
        ({"csv": CSV_HEAD + "2026-03-03T16:00:00,1\n", "tariff": {"name": "x", "periods": [{"label": "a", "start": 0, "end": 5, "price": 1}]}}, "gaps"),
    ],
)
def test_unusable_files_are_rejected_clearly(client, body, fragment):
    r = client.post("/api/meter/preview", json=body)
    assert r.status_code in (404, 422), r.text
    assert any(fragment in i["message"] for i in r.json()["detail"]), r.text


def test_imported_readings_replace_a_simulated_run(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1})
    run = d["runs"][0]["run_id"]
    csv_text = client.get("/api/demo/meter.csv").text
    r = client.post(f"/api/experiments/{d['id']}/runs/{run}/meter", json={"csv": csv_text, "filename": "tuesday.csv", "departure_charge_pct": 85})
    assert r.status_code == 200, r.text
    row = r.json()["run"]
    assert row["status"] == "completed" and row["imported"] is True and row["feasible"] is True
    assert row["outcomes"]["departure_charge_pct"] == pytest.approx(85) and row["outcomes"]["energy_kwh"] == pytest.approx(r.json()["summary"]["energy_kwh"])
    assert row["outcomes"]["energy_cost"] == pytest.approx(r.json()["summary"]["energy_cost"])  # priced with the experiment's own tariff
    assert client.post(f"/api/experiments/{d['id']}/runs/{run}/meter", json={"csv": csv_text, "departure_charge_pct": 85}).status_code == 409  # completed runs are immutable
    client.post(f"/api/experiments/{d['id']}/execute")
    from conftest import wait_done

    final = wait_done(client, d["id"])
    assert final["counts"]["completed"] == final["runs_total"] and [x["imported"] for x in final["runs"]].count(True) == 1


def test_imported_run_that_leaves_the_car_short_is_infeasible(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1})
    run = d["runs"][0]["run_id"]
    r = client.post(f"/api/experiments/{d['id']}/runs/{run}/meter", json={"csv": client.get("/api/demo/meter.csv").text, "departure_charge_pct": 50})
    assert r.json()["run"]["feasible"] is False


def test_attach_validation_and_unknown_ids(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1})
    run = d["runs"][0]["run_id"]
    csv_text = client.get("/api/demo/meter.csv").text
    assert client.post(f"/api/experiments/{d['id']}/runs/{run}/meter", json={"csv": csv_text}).status_code == 422  # charge at departure is required
    assert client.post(f"/api/experiments/{d['id']}/runs/{run}/meter", json={"csv": csv_text, "departure_charge_pct": 150}).status_code == 422
    assert client.post(f"/api/experiments/{d['id']}/runs/nope/meter", json={"csv": csv_text, "departure_charge_pct": 80}).status_code == 404
    assert client.post("/api/experiments/nope/runs/x/meter", json={"csv": csv_text, "departure_charge_pct": 80}).status_code == 404
