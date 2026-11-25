"""The F1 analysis of a continuous-factor experiment: the 16-run random design (16 distinct settings per factor) and a central composite."""

random16 = {"design": {"method": "random", "n_runs": 16, "run_budget": 40, "seed": 1}, "simulator": {"latency_seconds": 0.0, "stint_laps": 5}}


def test_sixteen_run_random_design_has_anova_and_no_level_comparisons(run_experiment, client):
    d = run_experiment(**random16)
    assert d["design"]["method"] == "random" and d["runs_total"] == 16
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={"alpha": 0.10}).json()
    fx = a["effects"]
    assert fx["residual_df"] == 5 and fx["anova"]["residual_df"] == 5 and fx["anova"]["rows"]
    mi = fx["model_info"]
    assert (mi["n_observations"], mi["rank"], mi["residual_df"]) == (16, 11, 5)
    assert mi["unique_values_per_factor"] == {k: 16 for k in ("front_wing", "rear_wing", "suspension", "tyre_pressure_psi")}
    assert mi["replicated_configurations"] == 0 and mi["pure_error_estimable"] is False and mi["quadratic_terms_included"] is False
    assert len(fx["effects"]) == 4 and len(fx["interactions"]) == 6
    assert a["main_effects"] == []  # no categorical-style rows for continuous factors
    assert any("only 5 residual degrees of freedom" in w for w in fx["anova"]["warnings"])
    assert any("no squared terms" in w for w in fx["regression_warnings"])


def test_random_design_reports_that_replicates_and_centre_points_are_not_used(make_experiment, client):
    d = make_experiment(design={"method": "random", "n_runs": 12, "replicates": 2, "center_points": 2, "run_budget": 40})
    g = d["design"]
    assert d["runs_total"] == 12
    assert g["settings_used"] == ["n_runs"] and set(g["settings_not_used"]) == {"replicates", "center_points"}
    assert g["settings_ignored"] == {"replicates": 2, "center_points": 2}
    assert "no replicated runs and no centre points" in g["settings_summary"]
    uses = {m["id"]: m["uses"] for m in client.get("/api/meta").json()["methods"]}
    assert uses["random"] == ["n_runs"] and uses["central_composite"] == ["center_points"] and uses["fractional_factorial"] == ["replicates", "center_points"]


def test_central_composite_includes_quadratic_terms_and_pure_error(run_experiment, client):
    d = run_experiment(design={"method": "central_composite", "center_points": 4, "run_budget": 40, "seed": 1},
                       simulator={"latency_seconds": 0.0, "stint_laps": 5})
    assert d["runs_total"] == 28
    fx = client.post(f"/api/experiments/{d['id']}/analysis", json={"alpha": 0.10}).json()["effects"]
    mi = fx["model_info"]
    assert (mi["rank"], mi["residual_df"], mi["pure_error_df"]) == (15, 13, 3) and mi["quadratic_terms_included"]
    assert len(fx["quadratic"]) == 4 and len(fx["effects"]) == 4 and len(fx["interactions"]) == 6
    assert all(e["low_to_high"] is not None for e in fx["effects"])
    assert not any("no squared terms" in w for w in fx["regression_warnings"])


def test_frontend_explains_continuous_effects_and_disables_unused_design_settings():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "frontend" / "setup_lab.js").read_text()
    assert "Continuous factors are reported using regression-based low-to-high effects" in js
    assert "Greyed-out settings are not used" in js and "model-summary" in js
