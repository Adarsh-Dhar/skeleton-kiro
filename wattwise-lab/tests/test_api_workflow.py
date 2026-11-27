"""API-level tests of the complete WATTWISE workflow: tariffs, simulation, design, execution, failures, analysis, optimisation, reports."""

from __future__ import annotations

import csv
import io
import time

import pytest
from conftest import wait_done
from inputs import SRC, appliance, ev, inputs, seed_tariffs, tariff

THREE = {
    "charge_start": {"low": 19, "high": 29},
    "charge_power_kw": {"low": 3.7, "high": 11},
    "appliance_start": {"low": 18, "high": 26},
}
OFAT = {"design": {"method": "ofat", "replicates": 1}}


# ------------------------------------------------------------------ meta, frontend, tariffs
def test_meta_and_frontend(client):
    m = client.get("/api/meta").json()
    assert "PROJECTED" in m["notice"] and set(m["factors"]) == {"charge_start", "charge_power_kw", "appliance_start"}
    assert {x["id"] for x in m["methods"]} == {"full_factorial", "fractional_factorial", "ofat", "central_composite", "box_behnken", "random"}
    assert {t["id"] for t in m["tariffs"]} >= {"time-of-use", "flat-rate"} and "baseline" not in m and "default_ev" not in m
    assert "user_provided" in m["source_types"] and "example" not in m["source_types"] and m["input_units"]["ev.battery_kwh"] == "kWh"
    page = client.get("/")
    assert page.status_code == 200 and "Wattwise Lab" in page.text
    assert client.get("/app.js").status_code == 200 and client.get("/charts.js").status_code == 200 and client.get("/style.css").status_code == 200


def test_tariff_crud_and_validation_over_http(client):
    t = tariff("Weekend saver", [{"label": "Off", "start": 0, "end": 8, "price": 0.1}, {"label": "On", "start": 8, "end": 24, "price": 0.3}], demand_charge_per_kw=0.2)
    r = client.post("/api/tariffs", json=t)
    assert r.status_code == 201 and r.json()["id"] == "weekend-saver"
    assert client.post("/api/tariffs", json=t).status_code == 422  # duplicate name
    t["periods"][1]["price"] = 0.35
    assert client.put("/api/tariffs/weekend-saver", json=t).json()["periods"][1]["price"] == 0.35
    assert client.put("/api/tariffs/nope", json=t).status_code == 404
    assert any(x["id"] == "weekend-saver" for x in client.get("/api/tariffs").json())
    assert client.delete("/api/tariffs/weekend-saver").status_code == 204 and client.delete("/api/tariffs/weekend-saver").status_code == 404
    gap = tariff("Gappy", [{"label": "x", "start": 0, "end": 5, "price": 0.1}])
    r = client.post("/api/tariffs", json=gap)
    assert r.status_code == 422 and any("gaps" in i["message"] for i in r.json()["detail"])
    v = client.post("/api/tariffs/validate", json=gap).json()
    assert v["valid"] is False and any("gaps" in i for i in v["issues"]) and client.post("/api/tariffs/validate", json=t).json() == {"valid": True, "issues": []}


def test_tariff_changes_do_not_alter_existing_experiments(make_experiment, client):
    d = make_experiment()
    before = d["household"]["tariff"]
    client.put("/api/tariffs/time-of-use", json=tariff("Time-of-use", [{"label": "all", "start": 0, "end": 24, "price": 9.0}]))
    assert client.get(f"/api/experiments/{d['id']}").json()["household"]["tariff"] == before


# ------------------------------------------------------------------ day simulation
def test_day_simulation_matches_the_model_and_validates_input(client):
    base = client.post("/api/simulate/day", json=inputs()).json()
    assert len(base["steps"]) == 96 and base["result_kind"] == "projected" and base["setup"]["charge_start"] == 19.0  # the EV's own plug-in time
    night = client.post("/api/simulate/day", json=inputs(setup={"charge_start": 24.0, "charge_power_kw": 7.4, "appliance_start": 24.0})).json()
    assert night["metrics"]["energy_cost"] < base["metrics"]["energy_cost"] and night["metrics"]["departure_charge_pct"] >= 80
    assert sum(p["cost"] for p in night["cost_by_period"].values()) == pytest.approx(night["metrics"]["energy_charge"], abs=1e-3)
    for bad in ({"setup": {"charge_start": 99}}, {"setup": {"wheel": 1}}, {"ev": ev(initial_soc_pct=90, desired_charge_pct=80)}, {"tariff_id": "nope"}):
        assert client.post("/api/simulate/day", json=inputs(**bad)).status_code in (404, 422), bad


def test_overview_starts_empty_then_counts_experiments(client, run_experiment):
    o = client.get("/api/overview").json()
    # nothing configured: no example household to show a day for
    assert o["kpis"]["experiments"] == 0 and o["kpis"]["best_saving_pct"] is None and o["baseline_day"] is None and not o["readiness"]["ready"]
    run_experiment(**OFAT)
    o = client.get("/api/overview").json()
    assert o["kpis"]["experiments"] == 1 and o["kpis"]["runs"] > 0 and o["kpis"]["simulated_kwh"] > 0 and o["experiments"][0]["best"]["saving_pct"] is not None


# ------------------------------------------------------------------ creation and validation
def test_default_experiment_is_a_three_level_full_factorial(make_experiment):
    d = make_experiment()
    assert d["design"]["method"] == "factorial" and 27 <= d["runs_total"] <= 29 and d["status"] == "designed" and d["counts"]["queued"] == d["runs_total"]
    assert d["desired_charge_pct"] == 80 and d["tariff"] == "Time-of-use" and d["baseline_cost"] > 0
    assert all(set(r["schedule"]) == set(THREE) for r in d["runs"]) and all(set(r["coded"]) == set(THREE) for r in d["runs"])


@pytest.mark.parametrize("method", ["full_factorial", "fractional_factorial", "ofat", "central_composite", "box_behnken", "random"])
def test_every_design_method_offered_by_the_ui_generates_a_valid_plan(make_experiment, method):
    d = make_experiment(design={"method": method, "replicates": 1, "run_budget": 120, "n_runs": 12})
    assert d["status"] == "designed" and d["runs_total"] >= 4
    for r in d["runs"]:
        for f in d["factors"]:
            assert f["low"] - 1e-9 <= r["configuration"][f["name"]] <= f["high"] + 1e-9


def test_two_factors_with_ofat_and_disabled_factor_stays_at_baseline(make_experiment):
    d = make_experiment(factors={**THREE, "appliance_start": {"enabled": False, "low": 18, "high": 26}}, design={"method": "full_factorial", "levels": 2, "replicates": 1})
    assert "appliance_start" not in d["runs"][0]["configuration"] and {f["name"] for f in d["factors"]} == {"charge_start", "charge_power_kw"}
    one = make_experiment(factors={"charge_start": {"low": 19, "high": 29}, "charge_power_kw": {"enabled": False, "low": 3.7, "high": 11}, "appliance_start": {"enabled": False, "low": 18, "high": 26}}, design={"method": "ofat"})
    assert [f["name"] for f in one["factors"]] == ["charge_start"]


@pytest.mark.parametrize(
    "body,fragment",
    [
        ({"factors": {"charge_start": {"low": 30, "high": 20}}}, "low must be smaller"),
        ({"factors": {"charge_start": {"low": 5, "high": 20}}}, "range must lie within"),
        ({"factors": {k: {"enabled": False, "low": 0, "high": 1} for k in THREE}}, "enable at least one factor"),
        ({"factors": {"charge_start": {"low": 19, "high": 29}, "charge_power_kw": {"enabled": False, "low": 1, "high": 2}, "appliance_start": {"enabled": False, "low": 1, "high": 2}}}, "at least two factors"),
        ({"factors": {"wheel": {"low": 0, "high": 1}}}, "unknown factor"),
        ({"name": "   "}, "blank"),
        ({"ev": ev(initial_soc_pct=90, desired_charge_pct=80)}, "initial charge must be below"),
        ({"ev": ev(departure_hour=25)}, "less than 24"),
        ({"design": {"method": "magic"}}, "fractional_factorial"),
        ({"design": {"run_budget": 6}}, "exceeds budget"),
        ({"design": {"replicates": 0}}, "greater than or equal to 1"),
        ({"simulator": {"fault_rate": 2}}, "less than or equal to 1"),
        ({"appliances": [appliance(f"a{i}") for i in range(21)]}, "at most 20"),
        ({"appliances": [appliance("x", power_kw=0)]}, "greater than 0"),
        ({"experiment_id": "Bad Id"}, "pattern"),
    ],
)
def test_invalid_experiment_definitions_are_rejected_with_clear_messages(client, body, fragment):
    r = client.post("/api/experiments", json=inputs(**{"name": "bad", **body}))
    assert r.status_code == 422, r.text
    assert any(fragment in i["message"] for i in r.json()["detail"]) and all({"field", "message"} <= set(i) for i in r.json()["detail"])
    assert client.get("/api/experiments").json() == []  # nothing half-created


def test_unknown_tariff_and_duplicate_ids_are_rejected(client, make_experiment):
    assert client.post("/api/experiments", json=inputs(name="x", tariff_id="nope")).status_code == 404
    d = make_experiment()
    assert client.post("/api/experiments", json=inputs(name="again", experiment_id=d["id"])).status_code == 409


def test_inline_tariff_is_stored_with_the_experiment(client):
    t = tariff("Inline", [{"label": "all", "start": 0, "end": 24, "price": 0.5}])
    d = client.post("/api/experiments", json=inputs(name="inline", tariff=t, simulator={"latency_seconds": 0}, **OFAT)).json()
    assert d["household"]["tariff"]["name"] == "Inline" and d["tariff"] == "Inline" and d["household"]["tariff"]["source"]["source_name"] == SRC["source_name"]


def test_household_settings_flow_into_the_experiment(make_experiment):
    d = make_experiment(ev=ev(battery_kwh=80, initial_soc_pct=20, desired_charge_pct=90, departure_hour=6.0), base_load_kw=0.5,
                        appliances=[appliance("Oven", power_kw=2, flexible=False, fixed_start=18)])
    assert d["desired_charge_pct"] == 90 and d["household"]["ev"]["battery_kwh"] == 80 and d["household"]["appliances"][0]["name"] == "Oven" and d["household"]["base_load_kw"] == 0.5


# ------------------------------------------------------------------ execution, constraint, failures
def test_complete_run_evaluates_the_departure_charge_constraint(run_experiment):
    d = run_experiment()
    assert d["status"] == "completed" and d["counts"]["completed"] == d["runs_total"] and d["counts"]["failed"] == 0
    runs = d["runs"]
    assert all({"energy_cost", "energy_kwh", "peak_power_kw", "departure_charge_pct"} <= set(r["outcomes"]) for r in runs)
    feas = [r for r in runs if r["feasible"]]
    assert feas and len(feas) < len(runs)
    assert all(r["outcomes"]["departure_charge_pct"] >= 80 - 1e-6 for r in feas) and all(r["outcomes"]["departure_charge_pct"] < 80 for r in runs if r["feasible"] is False)
    assert d["best"]["cost"] == pytest.approx(min(r["outcomes"]["energy_cost"] for r in feas)) and d["best"]["charge"] >= 80 - 1e-6 and d["best"]["saving_pct"] is not None


def test_second_execute_is_rejected_and_nothing_is_duplicated(run_experiment, client):
    d = run_experiment(**OFAT)
    assert client.post(f"/api/experiments/{d['id']}/execute").status_code == 409
    assert len(client.get(f"/api/experiments/{d['id']}/runs").json()) == d["runs_total"]


def test_unreachable_requirement_gives_no_recommendation(run_experiment, client):
    d = run_experiment(ev=ev(battery_kwh=200, initial_soc_pct=0, desired_charge_pct=100), **OFAT)
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={}).json()
    assert a["best_run_id"] is None and a["recommendation"]["status"] == "no_feasible" and d["best"] is None and len(a["infeasible"]) == d["runs_total"]


def test_faults_without_retries_fail_runs_visibly_and_explicit_retry_recovers(make_experiment, client):
    d = make_experiment(simulator={"latency_seconds": 0, "fault_rate": 1.0, "max_retries": 0}, **OFAT)
    client.post(f"/api/experiments/{d['id']}/execute")
    f = wait_done(client, d["id"])
    assert f["status"] == "failed" and f["counts"]["failed"] == f["runs_total"] > 0 and all("injected fault" in r["error"] for r in f["runs"])
    assert client.post(f"/api/experiments/{d['id']}/analysis", json={}).status_code == 409  # nothing to analyse yet
    assert client.post(f"/api/experiments/{d['id']}/execute").status_code == 409  # failed runs never retry implicitly
    assert client.post(f"/api/experiments/{d['id']}/retry").status_code == 202
    ok = wait_done(client, d["id"])
    assert ok["status"] == "completed" and all(r["attempts"] == 2 for r in ok["runs"]) and client.post(f"/api/experiments/{d['id']}/retry").status_code == 409


def test_automatic_retries_absorb_transient_faults(make_experiment, client):
    d = make_experiment(simulator={"latency_seconds": 0, "fault_rate": 1.0, "max_retries": 1}, **OFAT)
    client.post(f"/api/experiments/{d['id']}/execute")
    ok = wait_done(client, d["id"])
    assert ok["status"] == "completed" and {r["attempts"] for r in ok["runs"]} == {2}


def test_progress_is_visible_while_running_and_cancel_stops_remaining_runs(make_experiment, client):
    d = make_experiment(simulator={"latency_seconds": 0.25})
    client.post(f"/api/experiments/{d['id']}/execute")
    time.sleep(0.4)
    mid = client.get(f"/api/experiments/{d['id']}").json()
    assert mid["counts"]["running"] > 0 or mid["counts"]["completed"] > 0
    s = client.post(f"/api/experiments/{d['id']}/cancel").json()
    time.sleep(0.8)
    final = client.get(f"/api/experiments/{d['id']}").json()
    assert final["counts"]["cancelled"] > 0 and final["counts"]["queued"] == 0 and final["counts"]["running"] == 0 and s["id"] == d["id"]


def test_unknown_ids_return_404(client):
    for path in ("/api/experiments/nope", "/api/experiments/nope/runs", "/api/experiments/nope/optimise/history", "/api/experiments/nope/runs/x/profile"):
        assert client.get(path).status_code == 404, path
    for path in ("execute", "cancel", "retry", "analysis"):
        assert client.post(f"/api/experiments/nope/{path}").status_code == 404, path
    assert client.get("/api/experiments/nope/report").status_code == 404


def test_run_profile_matches_the_measured_outcome(run_experiment, client):
    d = run_experiment(**OFAT)
    r = d["runs"][0]
    p = client.get(f"/api/experiments/{d['id']}/runs/{r['run_id']}/profile").json()
    assert len(p["steps"]) == 96 and p["model_profile"] is True
    assert p["metrics"]["energy_kwh"] == pytest.approx(r["outcomes"]["energy_kwh"], rel=0.05)
    assert p["metrics"]["departure_charge_pct"] == pytest.approx(r["outcomes"]["departure_charge_pct"], abs=0.5)
    assert client.get(f"/api/experiments/{d['id']}/runs/nope/profile").status_code == 404


# ------------------------------------------------------------------ analysis and optimisation
def test_analysis_justifies_the_recommendation(run_experiment, client):
    d = run_experiment()
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={"alpha": 0.05}).json()
    rec = a["recommendation"]
    assert rec["status"] == "recommended" and a["best_run_id"] == rec["run_id"] and rec["expected"]["departure_charge_pct"] >= 80 - 1e-6
    assert rec["expected"]["energy_cost"] <= rec["baseline"]["energy_cost"] and any("never recommended" in r for r in rec["reasons"]) and any("PROJECTED" in c for c in rec["caveats"])
    assert a["effects"]["significant_factors"] and a["effects"]["r_squared"] is not None and a["effects"]["anova"]["kind"] == "factorial"
    assert {e["term"] for e in a["effects"]["effects"]} == set(THREE)
    assert all(not r["feasible"] for r in a["runs"].values() if r["run_id"] in {i["run_id"] for i in a["infeasible"]})
    assert client.get(f"/api/experiments/{d['id']}/recommendation").json()["run_id"] == rec["run_id"]
    assert client.post(f"/api/experiments/{d['id']}/analysis", json={"alpha": 0.9}).status_code == 422
    assert client.post(f"/api/experiments/{d['id']}/analysis", json={"bootstrap": True}).status_code == 200


def test_analysis_before_any_run_is_a_conflict(make_experiment, client):
    d = make_experiment()
    assert client.post(f"/api/experiments/{d['id']}/analysis", json={}).status_code == 409 and client.get(f"/api/experiments/{d['id']}/report").status_code == 409


def test_peak_demand_can_be_a_second_objective(run_experiment, client):
    d = run_experiment(also_minimise_peak=True)
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={}).json()
    assert d["multi_objective"] and a["pareto_run_ids"] and set(a["pareto_run_ids"]) <= set(a["feasible_run_ids"])


def test_optimiser_proposes_in_range_unseen_feasible_schedules(run_experiment, client):
    d = run_experiment()
    s = client.post(f"/api/experiments/{d['id']}/optimise/suggest", json={"count": 3}).json()["recommendations"]
    seen = {tuple(sorted(r["configuration"].items())) for r in d["runs"]}
    assert len(s) == 3 and all(r["prediction_kind"] == "model_prediction" and r["schedule"] and r["rationale"] for r in s)
    for r in s:
        assert tuple(sorted(r["configuration"].items())) not in seen and r["feasible"] is True
        for f in d["factors"]:
            assert f["low"] <= r["configuration"][f["name"]] <= f["high"]
    assert client.post(f"/api/experiments/{d['id']}/optimise/suggest", json={"count": 3, "method": "magic"}).status_code == 422
    assert len(client.get(f"/api/experiments/{d['id']}/runs").json()) == d["runs_total"]  # suggesting changes nothing


@pytest.mark.parametrize("method", ["bayesian", "random", "grid"])
def test_optimisation_apply_measures_proposals_and_records_history(run_experiment, client, method):
    d = run_experiment(**OFAT)
    r = client.post(f"/api/experiments/{d['id']}/optimise/apply", json={"count": 2, "method": method})
    assert r.status_code == 202 and r.json()["iteration"] == 1 and len(r.json()["added_runs"]) == 2
    final = wait_done(client, d["id"])
    assert final["runs_total"] == d["runs_total"] + 2 and final["counts"]["completed"] == final["runs_total"]
    assert sum(1 for x in final["runs"] if x["iteration"] == 1) == 2
    hist = client.get(f"/api/experiments/{d['id']}/optimise/history").json()
    assert len(hist) >= 2 and all(h["configuration"] for h in hist)


def test_optimiser_respects_budget_and_busy_state(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1, "run_budget": 12}, simulator={"latency_seconds": 0.0})
    client.post(f"/api/experiments/{d['id']}/execute")
    wait_done(client, d["id"])
    left = 12 - client.get(f"/api/experiments/{d['id']}").json()["runs_total"]
    client.post(f"/api/experiments/{d['id']}/optimise/apply", json={"count": 10})
    wait_done(client, d["id"])
    assert client.get(f"/api/experiments/{d['id']}").json()["runs_total"] == 12 and left > 0
    assert client.post(f"/api/experiments/{d['id']}/optimise/apply", json={"count": 1}).status_code == 409


def test_optimisation_never_loses_ground(make_experiment, client):
    d = make_experiment(design={"method": "fractional_factorial", "replicates": 1, "run_budget": 40})
    client.post(f"/api/experiments/{d['id']}/execute")
    before = wait_done(client, d["id"])["best"]["cost"]
    for _ in range(2):
        client.post(f"/api/experiments/{d['id']}/optimise/apply", json={"count": 2})
        after = wait_done(client, d["id"])["best"]["cost"]
        assert after <= before + 1e-9
        before = after


# ------------------------------------------------------------------ reports and exports
def test_report_and_exports(run_experiment, client):
    d = run_experiment()
    j = client.get(f"/api/experiments/{d['id']}/report").json()
    rep = j["report"]
    assert rep["planned_runs"] == rep["completed_runs"] == d["runs_total"] and rep["best_configuration"] and rep["anova"] and rep["regression"]
    assert rep["reproducibility"]["random_seed"] == 1 and len(rep["reproducibility"]["specification_hash"]) == 64
    html = client.get(f"/api/experiments/{d['id']}/report.html")
    assert html.status_code == 200 and "PROJECTED RESULTS" in html.text and "ANOVA" in html.text and "Inputs (your data and cited sources)" in html.text and "Recommendation" in html.text and "<script" not in html.text
    assert "Time-of-use" in html.text and "Peak" in html.text  # the tariff used is part of the report
    assert "attachment" in client.get(f"/api/experiments/{d['id']}/report.json").headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(client.get(f"/api/experiments/{d['id']}/export/runs.csv").text)))
    assert len(rows) == d["runs_total"] and {"charge_start", "energy_cost", "departure_charge_pct", "feasible"} <= set(rows[0])
    m = client.get(f"/api/experiments/{d['id']}/export/measurements.csv").text
    assert m.splitlines()[0].startswith("run_id,measurement_id") and len(m.splitlines()) > d["runs_total"] * 4
    assert client.get(f"/api/experiments/{d['id']}/export/effects.csv").text.startswith("metric,comparison")
    assert client.get(f"/api/experiments/{d['id']}/export/secrets.csv").status_code == 404


def test_html_report_escapes_user_text(run_experiment, client):
    d = run_experiment(name="<img src=x onerror=alert(1)>", hypothesis="<script>alert(1)</script>", **OFAT)
    html = client.get(f"/api/experiments/{d['id']}/report.html").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html and "<img src=x" not in html


def test_a_fixed_id_reproduces_results_exactly(tmp_path):
    from fastapi.testclient import TestClient

    from wattwise.api.app import create_app

    out = []
    for i in range(2):
        with TestClient(create_app(str(tmp_path / f"r{i}.db"))) as c:
            seed_tariffs(c)
            c.post("/api/experiments", json=inputs(name="repro", experiment_id="repro-one", simulator={"latency_seconds": 0}, **OFAT))
            c.post("/api/experiments/repro-one/execute")
            d = wait_done(c, "repro-one")
            out.append([(r["configuration"], r["outcomes"]) for r in d["runs"]])
    assert out[0] == out[1]


def test_two_experiments_are_isolated(make_experiment, client):
    a, b = make_experiment(name="A", **OFAT), make_experiment(name="B", design={"method": "ofat", "replicates": 1, "seed": 9})
    client.post(f"/api/experiments/{a['id']}/execute")
    wait_done(client, a["id"])
    assert client.get(f"/api/experiments/{b['id']}").json()["counts"]["completed"] == 0
    assert {e["name"] for e in client.get("/api/experiments").json()} == {"A", "B"}
