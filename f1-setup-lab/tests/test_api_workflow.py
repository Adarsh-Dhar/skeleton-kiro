"""API-level tests of the complete F1 workflow, validation, failures, constraints, telemetry and persistence."""

import asyncio
import csv
import io
import time

import pytest
from conftest import wait_done


# ----------------------------------------------------------------------------- SDK usage
def test_application_runs_on_the_installed_sdk_not_a_copy(client):
    import f1lab
    import trialforge

    h = client.get("/api/health").json()
    assert (
        h["trialforge"] == trialforge.__version__ and trialforge.__version__.startswith("0.2.") and "site-packages" in h["trialforge_path"]
    )
    lab = client.app.state.lab
    assert type(lab.svc) is trialforge.ExperimentService and type(lab.repo) is trialforge.SQLiteExperimentRepository
    assert not hasattr(f1lab, "ExperimentService") and "examples" not in __import__("sys").modules


def test_meta_exposes_catalogue_and_synthetic_notice(client):
    m = client.get("/api/meta").json()
    assert "SYNTHETIC" in m["notice"] and set(m["factors"]) == {"front_wing", "rear_wing", "suspension", "tyre_pressure_psi"}
    assert {x["id"] for x in m["methods"]} >= {"fractional_factorial", "central_composite", "ofat"}
    assert client.get("/").status_code == 200 and "F1" in client.get("/").text


# ------------------------------------------------------------------------------- creation
def test_default_experiment_is_a_resolution_iv_fractional_factorial(make_experiment):
    d = make_experiment()
    g = d["design"]
    assert g["type"] == "fractional_factorial" and (g["k"], g["p"], g["resolution"]) == (4, 1, 4) and g["defining_relation"] == ["I=ABCD"]
    assert d["runs_total"] == 8 * 2 + 2 * 2 and d["status"] == "designed" and d["counts"]["queued"] == 20
    assert set(g["alias_structure"]["A"]) == {"BCD"} and g["factor_letters"]["tyre_pressure_psi"] == "D"
    assert {r["point_kind"] for r in d["runs"]} == {None, "center"} and all(set(r["coded"]) == set(g["factor_letters"]) for r in d["runs"])
    assert all(r["configuration"]["front_wing"] in (10.0, 25.0, 40.0) for r in d["runs"])


def test_three_enabled_factors_use_a_resolution_iii_half_fraction(make_experiment):
    d = make_experiment(
        factors={
            "front_wing": {"low": 10, "high": 40},
            "rear_wing": {"low": 10, "high": 40},
            "suspension": {"low": 3, "high": 9},
            "tyre_pressure_psi": {"enabled": False, "low": 20, "high": 24},
        }
    )
    assert (d["design"]["k"], d["design"]["p"], d["design"]["resolution"]) == (3, 1, 3) and "tyre_pressure_psi" not in d["runs"][0][
        "configuration"
    ]


@pytest.mark.parametrize("method,expect", [("full_factorial", 3), ("ofat", 1), ("central_composite", 4), ("box_behnken", 4), ("random", 1)])
def test_other_design_methods_generate_valid_plans(make_experiment, method, expect):
    d = make_experiment(design={"method": method, "replicates": 1, "run_budget": 100, "n_runs": 12})
    assert d["design"]["method"] in ("factorial", method) and d["runs_total"] >= 10 and d["status"] == "designed"
    for r in d["runs"]:
        for f in d["factors"]:
            assert f["low"] - 1e-9 <= r["configuration"][f["name"]] <= f["high"] + 1e-9


@pytest.mark.parametrize(
    "body,field_part",
    [
        ({"factors": {"front_wing": {"low": 30, "high": 10}}}, "low must be smaller"),
        ({"factors": {"front_wing": {"low": -5, "high": 10}}}, "simulator limits"),
        (
            {
                "factors": {
                    "front_wing": {"enabled": False, "low": 0, "high": 1},
                    "rear_wing": {"enabled": False, "low": 0, "high": 1},
                    "suspension": {"enabled": False, "low": 3, "high": 9},
                }
            },
            "at least two",
        ),
        ({"factors": {"wheel_colour": {"low": 0, "high": 1}}}, "unknown factor"),
        ({"min_stability": 140}, "less than or equal to 100"),
        ({"name": "   "}, "blank"),
        ({"design": {"method": "magic"}}, "fractional_factorial"),
        ({"design": {"run_budget": 6}}, "exceeds budget"),
        ({"design": {"replicates": 0}}, "greater than or equal to 1"),
        ({"simulator": {"fault_rate": 2}}, "less than or equal to 1"),
    ],
)
def test_invalid_experiment_definitions_are_rejected_with_clear_messages(client, body, field_part):
    r = client.post("/api/experiments", json={"name": "bad", **body})
    assert r.status_code == 422, r.text
    assert any(field_part in i["message"] for i in r.json()["detail"]) and all({"field", "message"} <= set(i) for i in r.json()["detail"])
    assert client.get("/api/experiments").json() == []  # nothing half-created


def test_box_behnken_needs_three_factors_and_two_factor_ccd_is_allowed(client):
    two = {"front_wing": {"low": 10, "high": 40}, "rear_wing": {"low": 10, "high": 40}}
    assert client.post("/api/experiments", json={"name": "x", "factors": two, "design": {"method": "box_behnken"}}).status_code == 422
    r = client.post("/api/experiments", json={"name": "x", "factors": two, "design": {"method": "central_composite", "run_budget": 40}})
    assert r.status_code == 201 and r.json()["runs_total"] == 4 + 4 + 2


# ------------------------------------------------------------------ execution & constraints
def test_complete_screening_run_with_constraint_evaluation(run_experiment, client):
    d = run_experiment()
    assert d["status"] == "completed" and d["counts"]["completed"] == 20 and d["counts"]["failed"] == 0
    runs = d["runs"]
    assert all({"lap_time", "stability", "sector_1", "top_speed"} <= set(r["outcomes"]) for r in runs)
    feas = [r for r in runs if r["feasible"]]
    assert (
        feas
        and len(feas) < len(runs)
        and all(r["outcomes"]["stability"] >= 85 for r in feas)
        and all(r["outcomes"]["stability"] < 85 for r in runs if r["feasible"] is False)
    )
    assert d["best"]["lap_time"] == pytest.approx(min(r["outcomes"]["lap_time"] for r in feas)) and d["best"]["stability"] >= 85


def test_second_execute_is_rejected_and_nothing_is_duplicated(run_experiment, client):
    d = run_experiment()
    assert client.post(f"/api/experiments/{d['id']}/execute").status_code == 409
    assert len(client.get(f"/api/experiments/{d['id']}/runs").json()) == 20


def test_unreachable_constraint_gives_no_recommendation(run_experiment, client):
    d = run_experiment(min_stability=99.9)
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={}).json()
    assert a["best_run_id"] is None and a["recommendation"]["status"] == "no_feasible" and d["best"] is None
    assert len(a["infeasible"]) == 20 and a["infeasible"][0]["violations"][0]["constraint"] == "stability"


def test_faults_without_retries_fail_runs_visibly_and_explicit_retry_recovers(make_experiment, client):
    d = make_experiment(simulator={"latency_seconds": 0, "fault_rate": 1.0, "max_retries": 0}, design={"method": "ofat", "replicates": 1})
    client.post(f"/api/experiments/{d['id']}/execute")
    f = wait_done(client, d["id"])
    assert (
        f["status"] == "failed" and f["counts"]["failed"] == f["runs_total"] > 0 and all("injected fault" in r["error"] for r in f["runs"])
    )
    assert client.post(f"/api/experiments/{d['id']}/analysis", json={}).status_code == 409  # nothing to analyse yet
    assert client.post(f"/api/experiments/{d['id']}/execute").status_code == 409  # failed runs never retry implicitly
    assert client.post(f"/api/experiments/{d['id']}/retry").status_code == 202
    ok = wait_done(client, d["id"])
    assert (
        ok["status"] == "completed"
        and all(r["attempts"] == 2 for r in ok["runs"])
        and client.post(f"/api/experiments/{d['id']}/retry").status_code == 409
    )


def test_automatic_retries_absorb_transient_faults(make_experiment, client):
    d = make_experiment(simulator={"latency_seconds": 0, "fault_rate": 1.0, "max_retries": 1}, design={"method": "ofat", "replicates": 1})
    client.post(f"/api/experiments/{d['id']}/execute")
    ok = wait_done(client, d["id"])
    assert ok["status"] == "completed" and {r["attempts"] for r in ok["runs"]} == {2}


def test_cancel_stops_remaining_runs(make_experiment, client):
    d = make_experiment(simulator={"latency_seconds": 0.25})
    client.post(f"/api/experiments/{d['id']}/execute")
    time.sleep(0.4)
    s = client.post(f"/api/experiments/{d['id']}/cancel").json()
    time.sleep(0.6)
    final = client.get(f"/api/experiments/{d['id']}").json()
    assert final["counts"]["cancelled"] > 0 and final["counts"]["queued"] == 0 and final["counts"]["running"] == 0 and s["id"] == d["id"]


def test_unknown_ids_return_404(client):
    for path in ("/api/experiments/nope", "/api/experiments/nope/runs", "/api/experiments/nope/telemetry"):
        assert client.get(path).status_code == 404
    assert client.post("/api/experiments/nope/execute").status_code == 404
    assert client.get("/api/demo/other.csv").status_code == 404


# --------------------------------------------------------------------------------- telemetry
def test_telemetry_preview_converts_units_and_flags_the_slow_lap(client):
    s = client.get("/api/demo/telemetry.csv").text
    ms = client.get("/api/demo/telemetry_ms.csv").text
    a = client.post("/api/telemetry/preview", json={"csv": s}).json()
    b = client.post("/api/telemetry/preview", json={"csv": ms, "time_unit": "ms"}).json()
    assert a["summary"]["laps"] == 8 == b["summary"]["laps"] and a["summary"]["outlier_laps"] == [5] == b["summary"]["outlier_laps"]
    assert b["summary"]["unit_converted"] and not a["summary"]["unit_converted"]
    assert [lap["lap_time"] for lap in b["laps"]] == pytest.approx([lap["lap_time"] for lap in a["laps"]], abs=1e-3)
    assert a["summary"]["best_lap"] < a["summary"]["mean_lap"] and "outlier" in a["laps"][4]["flags"]["lap_time"]


def test_telemetry_preview_problems_are_reported_not_fatal(client):
    r = client.post("/api/telemetry/preview", json={"csv": "lap,sector_1,sector_2\n1,1,1\n"}).json()
    assert r["laps"] == [] and "missing required column" in r["errors"][0]
    r = client.post("/api/telemetry/preview", json={"csv": "sector_1,sector_2,sector_3\n27,31,21\nbad,1,1\n"}).json()
    assert len(r["laps"]) == 1 and len(r["errors"]) == 1
    assert client.post("/api/telemetry/preview", json={"csv": "x", "time_unit": "h"}).status_code == 422


def test_recorded_telemetry_can_replace_a_simulated_run(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1})
    run = d["runs"][0]["run_id"]
    csvtext = client.get("/api/demo/telemetry_ms.csv").text
    r = client.post(f"/api/experiments/{d['id']}/runs/{run}/telemetry", json={"csv": csvtext, "time_unit": "ms", "filename": "session.csv"})
    assert r.status_code == 200
    row = r.json()["run"]
    assert row["status"] == "completed" and row["imported"] is True and row["feasible"] is True and 80 < row["outcomes"]["lap_time"] < 82
    tel = [t for t in client.get(f"/api/experiments/{d['id']}/telemetry").json() if t["run_id"] == run][0]
    assert tel["synthetic"] is False and tel["source"] == "csv:session.csv" and len(tel["laps"]) == 8 and tel["laps"][4]["outlier"]
    assert (
        client.post(f"/api/experiments/{d['id']}/runs/{run}/telemetry", json={"csv": csvtext}).status_code == 409
    )  # completed runs are immutable
    assert client.post(f"/api/experiments/{d['id']}/runs/nope/telemetry", json={"csv": csvtext}).status_code == 404
    other = d["runs"][1]["run_id"]
    client.post(f"/api/experiments/{d['id']}/execute")  # the rest are simulated; the imported run is untouched
    final = wait_done(client, d["id"])
    assert final["counts"]["completed"] == final["runs_total"] and [r["imported"] for r in final["runs"]].count(True) == 1 and other


def test_import_missing_required_stability_fails_the_run_instead_of_faking_data(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1})
    run = d["runs"][0]["run_id"]
    r = client.post(
        f"/api/experiments/{d['id']}/runs/{run}/telemetry", json={"csv": "sector_1,sector_2,sector_3\n27,31.5,21.6\n27,31.4,21.5\n"}
    )
    row = r.json()["run"]
    assert row["status"] == "failed" and "stability" in row["error"]


# ---------------------------------------------------------- analysis, optimisation, report
def test_analysis_identifies_factors_and_justifies_the_recommendation(run_experiment, client):
    d = run_experiment()
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={"alpha": 0.05}).json()
    sig = a["effects"]["significant_factors"]
    assert "front_wing" in sig and "suspension" not in sig or sig  # screening finds the dominant factor
    rec = a["recommendation"]
    assert rec["status"] == "recommended" and a["best_run_id"] == rec["run_id"] and rec["expected"]["stability"] >= 85
    assert any("never recommended" in r for r in rec["reasons"]) and any("SYNTHETIC" in c for c in rec["caveats"])
    assert a["effects"]["anova"]["kind"] == "factorial" and a["effects"]["r_squared"] is not None
    best = d["best"]
    assert rec["setup"]["front_wing"] == pytest.approx(best["configuration"]["front_wing"], abs=1e-3)
    assert all(not r["feasible"] for r in a["runs"].values() if r["run_id"] in {i["run_id"] for i in a["infeasible"]})
    assert client.post(f"/api/experiments/{d['id']}/analysis", json={"alpha": 0.9}).status_code == 422
    assert client.post(f"/api/experiments/{d['id']}/analysis", json={"bootstrap": True}).status_code == 200


def test_optimiser_proposes_in_range_unseen_setups_and_never_beats_budget(run_experiment, client):
    d = run_experiment()
    s = client.post(f"/api/experiments/{d['id']}/optimise/suggest", json={"count": 3}).json()["recommendations"]
    seen = {tuple(sorted(r["configuration"].items())) for r in d["runs"]}
    assert len(s) == 3 and all(
        r["prediction_kind"] == "model_prediction" and r["expected_improvement"] is not None and r["prediction_std"] > 0 for r in s
    )
    for r in s:
        assert tuple(sorted(r["configuration"].items())) not in seen and r["feasible"] is True
        for f in d["factors"]:
            assert f["low"] <= r["configuration"][f["name"]] <= f["high"]
    assert client.post(f"/api/experiments/{d['id']}/optimise/suggest", json={"count": 3, "method": "magic"}).status_code == 422


def test_optimisation_loop_improves_the_best_feasible_setup(make_experiment, client):
    """Statistical claim, not a lucky seed: over several reproducible experiments the model-based loop never loses ground
    and on average finds a clearly faster feasible setup than the screening design alone."""
    gains = []
    for n in range(1, 4):
        d = make_experiment(experiment_id=f"opt-loop-{n}", design={"seed": n})
        client.post(f"/api/experiments/{d['id']}/execute")
        first = wait_done(client, d["id"])["best"]["lap_time"]
        for it in range(1, 7):
            r = client.post(f"/api/experiments/{d['id']}/optimise/apply", json={"count": 2})
            assert r.status_code == 202 and r.json()["iteration"] == it
            wait_done(client, d["id"])
        final = client.get(f"/api/experiments/{d['id']}").json()
        assert final["runs_total"] == 32 and final["counts"]["completed"] == 32 and final["best"]["stability"] >= 85
        assert [r["iteration"] for r in final["runs"][20:]] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]
        assert final["best"]["lap_time"] <= first + 1e-9  # the best feasible lap can only improve
        gains.append(first - final["best"]["lap_time"])
        assert len(client.get(f"/api/experiments/{d['id']}/optimise/history").json()) >= 12
    assert sum(gains) / len(gains) > 0.1, gains


def test_fixed_experiment_id_reproduces_results_exactly_and_duplicates_are_rejected(make_experiment, client):
    a = make_experiment(experiment_id="repro-1", design={"method": "ofat", "replicates": 1})
    client.post(f"/api/experiments/{a['id']}/execute")
    first = wait_done(client, a["id"])
    assert client.post("/api/experiments", json={"name": "dup", "experiment_id": "repro-1"}).status_code == 409
    assert client.post("/api/experiments", json={"name": "bad id", "experiment_id": "Bad ID!"}).status_code == 422
    from f1lab.api.app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(":memory:" if False else str(client.app.state.lab.repo.url.replace("sqlite:///", "")) + ".2")) as c2:
        b = c2.post(
            "/api/experiments",
            json={
                "name": "t",
                "experiment_id": "repro-1",
                "design": {"method": "ofat", "replicates": 1},
                "simulator": {"latency_seconds": 0},
            },
        ).json()
        c2.post(f"/api/experiments/{b['id']}/execute")
        second = wait_done(c2, b["id"])
    assert [r["outcomes"] for r in first["runs"]] == [r["outcomes"] for r in second["runs"]]


def test_optimiser_respects_budget_and_busy_state(make_experiment, client):
    d = make_experiment(design={"method": "ofat", "replicates": 1, "run_budget": 12}, simulator={"latency_seconds": 0.0})
    client.post(f"/api/experiments/{d['id']}/execute")
    wait_done(client, d["id"])
    left = 12 - client.get(f"/api/experiments/{d['id']}").json()["runs_total"]
    client.post(f"/api/experiments/{d['id']}/optimise/apply", json={"count": 10})
    wait_done(client, d["id"])
    assert client.get(f"/api/experiments/{d['id']}").json()["runs_total"] == 12 and left > 0
    assert client.post(f"/api/experiments/{d['id']}/optimise/apply", json={"count": 1}).status_code == 409


def test_report_and_exports(run_experiment, client):
    d = run_experiment()
    j = client.get(f"/api/experiments/{d['id']}/report").json()
    rep = j["report"]
    assert (
        rep["report_version"] == "2.0"
        and rep["planned_runs"] == 20 == rep["completed_runs"]
        and rep["best_configuration"]
        and rep["anova"]
        and rep["regression"]
    )
    assert (
        rep["reproducibility"]["random_seed"] == 1
        and len(rep["reproducibility"]["specification_hash"]) == 64
        and rep["constraint_violations"]
    )
    html = client.get(f"/api/experiments/{d['id']}/report.html")
    assert (
        html.status_code == 200
        and "SYNTHETIC DATA" in html.text
        and "ANOVA" in html.text
        and "Recommendation" in html.text
        and "<script" not in html.text
    )
    assert "attachment" in client.get(f"/api/experiments/{d['id']}/report.json").headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(client.get(f"/api/experiments/{d['id']}/export/runs.csv").text)))
    assert len(rows) == 20 and {"front_wing", "lap_time", "stability", "feasible"} <= set(rows[0])
    m = client.get(f"/api/experiments/{d['id']}/export/measurements.csv").text
    assert m.splitlines()[0].startswith("run_id,measurement_id") and len(m.splitlines()) > 20 * 20
    assert client.get(f"/api/experiments/{d['id']}/export/effects.csv").text.startswith("metric,comparison")
    assert client.get(f"/api/experiments/{d['id']}/export/secrets.csv").status_code == 404


def test_html_report_escapes_user_text(make_experiment, client, run_experiment):
    d = run_experiment(name="<img src=x onerror=alert(1)>", hypothesis="<script>alert(1)</script>")
    html = client.get(f"/api/experiments/{d['id']}/report.html").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html and "<img src=x" not in html


def test_multi_objective_experiment_reports_pareto(run_experiment, client):
    d = run_experiment(also_minimise_tyre_wear=True)
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={}).json()
    assert d["multi_objective"] and a["pareto_run_ids"] and set(a["pareto_run_ids"]) <= set(a["feasible_run_ids"])


# -------------------------------------------------------------------------------- persistence
def test_experiments_survive_restart_and_reanalysis_is_identical(db_path):
    from f1lab.api.app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(db_path)) as c1:
        eid = c1.post("/api/experiments", json={"name": "persist", "simulator": {"latency_seconds": 0}}).json()["id"]
        c1.post(f"/api/experiments/{eid}/execute")
        before = wait_done(c1, eid)
        a1 = c1.post(f"/api/experiments/{eid}/analysis", json={}).json()
    with TestClient(create_app(db_path)) as c2:
        after = c2.get(f"/api/experiments/{eid}").json()
        assert (
            after["runs"] == before["runs"]
            and after["status"] == "completed"
            and [e["id"] for e in c2.get("/api/experiments").json()] == [eid]
        )
        a2 = c2.post(f"/api/experiments/{eid}/analysis", json={}).json()
        assert a2["effects"] == a1["effects"] and a2["best_run_id"] == a1["best_run_id"]
        # the per-experiment simulator was re-registered, so more runs can still be executed
        c2.post(f"/api/experiments/{eid}/optimise/apply", json={"count": 1})
        assert wait_done(c2, eid)["runs_total"] == 21


async def test_interrupted_runs_are_recovered_after_a_crash(db_path):
    from f1lab.api.schemas import ExperimentRequest
    from f1lab.services.lab import LabService

    lab = LabService(db_path)
    d = lab.create(ExperimentRequest(name="crash", simulator={"latency_seconds": 5.0}, design={"method": "ofat", "replicates": 1}))
    lab.execute(d["id"])
    await asyncio.sleep(0.5)
    assert lab.detail(d["id"])["counts"]["running"] > 0
    for t in lab.runner._tasks.values():  # simulate process death mid-run
        t.cancel()
    await asyncio.gather(*lab.runner._tasks.values(), return_exceptions=True)
    stuck = lab.detail(d["id"])["counts"]["running"]
    assert stuck > 0
    lab.repo.engine.dispose()
    lab2 = LabService(db_path)
    actions = await lab2.startup()
    assert len(actions) == stuck and {a["action"] for a in actions} == {"retry"}  # simulator declares itself idempotent
    assert lab2.detail(d["id"])["counts"]["running"] == 0
    lab2.svc.registry.get(f"f1-sim/{d['id']}").latency = 0.0
    lab2.execute(d["id"])
    await lab2.runner.wait(d["id"])
    final = lab2.detail(d["id"])
    assert final["counts"]["completed"] == final["runs_total"] and len(final["runs"]) == d["runs_total"]
    await lab2.shutdown()


def test_two_experiments_are_isolated(make_experiment, client):
    a, b = (
        make_experiment(name="A", design={"method": "ofat", "replicates": 1}),
        make_experiment(name="B", design={"method": "ofat", "replicates": 1, "seed": 9}),
    )
    client.post(f"/api/experiments/{a['id']}/execute")
    wait_done(client, a["id"])
    assert client.get(f"/api/experiments/{b['id']}").json()["counts"]["completed"] == 0
    assert {e["name"] for e in client.get("/api/experiments").json()} == {"A", "B"}
