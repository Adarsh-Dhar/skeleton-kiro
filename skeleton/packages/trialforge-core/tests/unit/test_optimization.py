import numpy as np
import pytest
from conftest import build_result, simple_spec
from scipy import stats as sps

from trialforge import (
    Constraint,
    Factor,
    MeasurementDefinition,
    Objective,
    OptimizationError,
    Optimizer,
    PluginRegistry,
    Recommendation,
    StoppingCriteria,
)
from trialforge.optimization.surrogate import GaussianProcess, expected_improvement, prob_in_interval

OPT = Optimizer()


# ------------------------------------------------------------------------ surrogate maths
def test_expected_improvement_matches_monte_carlo():
    rng = np.random.default_rng(0)
    for mu, sd, best in [(1.0, 0.5, 0.8), (0.2, 1.0, 0.5), (3.0, 0.3, 1.0)]:
        draws = rng.normal(mu, sd, 2_000_000)
        mc = np.maximum(best - draws, 0).mean()
        assert expected_improvement(np.array([mu]), np.array([sd]), best)[0] == pytest.approx(mc, rel=0.01, abs=1e-4)


def test_expected_improvement_edge_cases_and_monotonicity():
    assert expected_improvement(np.array([5.0]), np.array([0.0]), 1.0)[0] == 0.0  # certain & worse
    assert expected_improvement(np.array([0.0]), np.array([0.0]), 1.0)[0] == pytest.approx(1.0)  # certain improvement
    ei = expected_improvement(np.array([1.0, 1.0]), np.array([0.1, 0.9]), 1.0)
    assert ei[1] > ei[0] > 0  # more uncertainty, more EI


def test_probability_of_feasibility_interval():
    p = prob_in_interval(np.array([0.0]), np.array([1.0]), -1.0, 1.0)[0]
    assert p == pytest.approx(sps.norm.cdf(1) - sps.norm.cdf(-1))
    assert prob_in_interval(np.array([10.0]), np.array([0.1]), None, 5.0)[0] < 1e-9


def test_gp_interpolates_smooth_function_with_calibrated_uncertainty():
    rng = np.random.default_rng(0)
    f = lambda X: np.sin(3 * X[:, 0]) + (X[:, 1] - 0.5) ** 2
    X = rng.random((30, 2))
    gp = GaussianProcess(X, f(X))
    T = rng.random((300, 2))
    mu, sd = gp.predict(T)
    err = np.abs(mu - f(T))
    assert np.sqrt(np.mean(err**2)) < 0.05
    assert 0.85 <= np.mean(err < 1.96 * sd) <= 1.0  # intervals not overconfident
    mu_t, sd_t = gp.predict(X[:5])
    assert np.all(sd_t < sd.mean())  # tighter at training points


# -------------------------------------------------------------------------------- fixtures
def quad_spec(**kw):
    return simple_spec(
        [
            Factor(name="x", factor_type="continuous", lower=0, upper=1),
            Factor(name="y", factor_type="continuous", lower=0, upper=1),
        ],
        metric="f",
        measurement_definitions=[MeasurementDefinition(metric="f", unit="u"), MeasurementDefinition(metric="g", unit="u")],
        run_budget=kw.pop("run_budget", 60),
        random_seed=kw.pop("random_seed", 11),
        **kw,
    )


F = lambda c: (c["x"] - 0.3) ** 2 + (c["y"] - 0.7) ** 2
G = lambda c: c["x"] + c["y"]


def seed_result(spec, n=12, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        c = {"x": float(rng.random()), "y": float(rng.random())}
        rows.append((c, {"f": F(c), "g": G(c)}))
    return build_result(spec, rows, control=rows[0][0], method="random", metric="f")


# ------------------------------------------------------------------------ strategies & labels
def test_bayesian_recommendation_is_labelled_model_prediction_with_uncertainty():
    s = quad_spec()
    r = OPT.suggest(s, seed_result(s), count=2, method="bayesian")
    assert len(r) == 2 and r[0].configuration != r[1].configuration
    x = r[0]
    assert x.prediction_kind == "model_prediction" and x.strategy == "bayesian" and x.objective == "f"
    assert x.expected_improvement is not None and x.expected_improvement >= 0 and x.prediction_std > 0
    assert x.predicted_value is not None and "Gaussian-process" in x.rationale and "Expected Improvement" in x.rationale
    assert "not guaranteed calibrated" in x.uncertainty and x.feasible is True and x.remaining_budget == 60 - 12 - 2


def test_sequential_is_alias_for_bayesian_and_heuristic_is_honestly_labelled():
    s = quad_spec()
    res = seed_result(s)
    assert OPT.suggest(s, res, method="sequential")[0].prediction_kind == "model_prediction"
    h = OPT.suggest(s, res, method="heuristic")[0]
    assert h.prediction_kind == "heuristic_score" and h.expected_improvement is None and h.predicted_value is None
    assert h.heuristic_score is not None and "HEURISTIC" in h.rationale and "uncalibrated" in h.rationale
    for m in ("grid", "random"):
        r = OPT.suggest(s, res, method=m)[0]
        assert r.prediction_kind == "search" and r.expected_improvement is None and r.predicted_value is None


def test_bayesian_falls_back_transparently_with_insufficient_data():
    s = quad_spec()
    r = OPT.suggest(s, seed_result(s, n=2), method="bayesian")[0]
    assert r.prediction_kind == "search" and r.expected_improvement is None and "needs at least" in r.rationale


def test_bayesian_optimisation_beats_random_search_on_a_known_function():
    def run(method, seed):
        s = quad_spec(random_seed=seed)
        res = seed_result(s, n=6, seed=seed)
        for _ in range(12):
            rec = OPT.suggest(s, res, method=method)[0]
            res = build_result(
                s,
                [(r.configuration, {"f": F(r.configuration), "g": G(r.configuration)}) for r in res.runs]
                + [(rec.configuration, {"f": F(rec.configuration), "g": G(rec.configuration)})],
                control=res.plan.control_configuration,
                method="random",
                metric="f",
            )
        return min(F(r.configuration) for r in res.runs)

    bo = np.mean([run("bayesian", s) for s in (1, 2, 3)])
    rnd = np.mean([run("random", s) for s in (1, 2, 3)])
    assert bo < 0.01 and bo < rnd / 2


def test_constraints_probability_of_feasibility_steers_away_from_infeasible_region():
    s = quad_spec(constraints=[Constraint(metric="g", minimum=1.2)])  # unconstrained optimum (0.3,0.7) has g=1.0 -> infeasible
    res = seed_result(s, n=20)
    recs = OPT.suggest(s, res, count=3, method="bayesian")
    assert all(r.predicted_feasibility_probability is not None for r in recs)
    assert any(e["kind"] == "output" and e["status"] == "predicted" for e in recs[0].constraint_evaluation)
    gs = [G(r.configuration) for r in recs]
    assert np.mean(gs) > 1.0  # pushed toward g >= 1.2 side


def test_input_constraints_never_violated_by_any_strategy():
    s = quad_spec(constraints=[Constraint(metric="x", maximum=0.4), Constraint(kind="input", function="sum_cap")])
    from trialforge.definitions.registry import DEFAULT_PLUGINS

    DEFAULT_PLUGINS.register("constraint", "sum_cap", lambda cfg, out: cfg["x"] + cfg["y"] <= 1.0, replace=True)
    res = seed_result(s, n=8)
    for m in ("grid", "random", "heuristic", "bayesian", "sequential"):
        for rec in OPT.suggest(s, res, count=4, method=m):
            c = rec.configuration
            assert c["x"] <= 0.4 and c["x"] + c["y"] <= 1.0 and rec.feasible is True, m


def test_never_recommends_a_configuration_that_was_already_run():
    s = simple_spec(
        [Factor(name="a", factor_type="discrete", values=[1, 2, 3])],
        metric="f",
        run_budget=10,
        measurement_definitions=[MeasurementDefinition(metric="f", unit="u")],
    )
    res = build_result(s, [({"a": 1}, 3.0), ({"a": 2}, 2.0)], control={"a": 1}, metric="f")
    for m in ("grid", "random", "heuristic", "bayesian"):
        assert [r.configuration for r in OPT.suggest(s, res, count=5, method=m)] == [{"a": 3}], m


def test_budget_exhausted_returns_nothing_and_counts_are_capped():
    s = quad_spec(run_budget=14)
    res = seed_result(s, n=12)
    assert len(OPT.suggest(s, res, count=10, method="random")) == 2
    assert OPT.suggest(quad_spec(run_budget=12).model_copy(update={"experiment_id": s.experiment_id}), res, method="random") == []


def test_multi_objective_bayesian_and_categorical_factors():
    s = simple_spec(
        [
            Factor(name="x", factor_type="continuous", lower=0, upper=1),
            Factor(name="m", factor_type="categorical", values=["a", "b", "c"]),
        ],
        measurement_definitions=[MeasurementDefinition(metric="p", unit="u"), MeasurementDefinition(metric="q", unit="u")],
        objectives=[Objective(metric="p", direction="minimize"), Objective(metric="q", direction="maximize")],
        run_budget=40,
    )
    rng = np.random.default_rng(1)
    rows = []
    for i in range(14):
        c = {"x": float(rng.random()), "m": "abc"[i % 3]}
        rows.append((c, {"p": c["x"] ** 2 + ("abc".index(c["m"])) * 0.1, "q": c["x"]}))
    res = build_result(s, rows, control=rows[0][0], method="random")
    recs = OPT.suggest(s, res, count=3, method="bayesian")
    assert len(recs) == 3 and all(r.prediction_kind == "model_prediction" and r.predicted_value is None for r in recs)
    assert "ParEGO" in recs[0].rationale and all(r.configuration["m"] in "abc" for r in recs)


def test_optuna_strategy_respects_bounds_and_constraints():
    pytest.importorskip("optuna")
    s = quad_spec(constraints=[Constraint(metric="x", maximum=0.5)])
    recs = OPT.suggest(s, seed_result(s, n=10), count=3, method="optuna")
    assert len(recs) == 3 and all(0 <= r.configuration["x"] <= 0.5 and 0 <= r.configuration["y"] <= 1 for r in recs)
    assert recs[0].strategy.startswith("optuna:") and recs[0].expected_improvement is None and recs[0].prediction_kind == "search"


def test_optimizer_plugin_registration_is_explicit():
    reg = PluginRegistry()
    opt = Optimizer(plugins=reg)
    with pytest.raises(OptimizationError, match="unsupported optimizer"):
        opt.suggest(quad_spec(), seed_result(quad_spec()), method="my_strategy")
    reg.register("optimizer", "centre", lambda ctx: [Recommendation(configuration={"x": 0.5, "y": 0.5}, rationale="centre")])
    s = quad_spec()
    r = opt.suggest(s, seed_result(s), method="centre")[0]
    assert r.configuration == {"x": 0.5, "y": 0.5} and r.strategy == "plugin:centre" and r.remaining_budget == 60 - 12 - 1


def test_invalid_requests_rejected():
    s = quad_spec()
    with pytest.raises(OptimizationError):
        OPT.suggest(s, seed_result(s), count=0)
    other = quad_spec()
    with pytest.raises(OptimizationError, match="does not belong"):
        OPT.suggest(other, seed_result(s))


# -------------------------------------------------------------------------- stopping rules
def trace_result(values, budget=50, direction="minimize", feasible=None):
    s = simple_spec(
        [Factor(name="a", factor_type="continuous", lower=0, upper=100)],
        metric="f",
        direction=direction,
        run_budget=budget,
        measurement_definitions=[MeasurementDefinition(metric="f", unit="u"), MeasurementDefinition(metric="g", unit="u")],
        constraints=[Constraint(metric="g", minimum=1)] if feasible else [],
    )
    rows = [({"a": float(i)}, {"f": v, "g": 5.0 if (feasible is None or feasible[i]) else 0.0}) for i, v in enumerate(values)]
    return s, build_result(s, rows, control=rows[0][0], method="random", metric="f")


@pytest.mark.parametrize(
    "values,criteria,iteration,reason",
    [
        ([9, 7, 4], StoppingCriteria(target_value=5), 0, "target_achieved"),
        ([9, 7, 6], StoppingCriteria(target_value=5), 0, "continue"),
        ([9, 8, 7, 6], StoppingCriteria(improvement_threshold=3), 0, "improvement_threshold_met"),
        ([9, 8.9], StoppingCriteria(improvement_threshold=3), 0, "continue"),
        ([9, 8], StoppingCriteria(max_iterations=3), 3, "max_iterations"),
        ([9, 8, 7.99, 7.98, 7.97, 7.96, 7.95], StoppingCriteria(min_improvement=0.1, patience=3), 0, "insufficient_improvement"),
        ([9, 8, 7, 6, 5, 4, 3], StoppingCriteria(min_improvement=0.1, patience=3), 0, "continue"),
    ],
)
def test_stopping_rules(values, criteria, iteration, reason):
    s, res = trace_result(values)
    assert OPT.should_stop(s, res, criteria, iteration=iteration).reason == reason


def test_budget_cancel_and_no_candidate_stops_and_priority():
    s, res = trace_result([3, 2, 1], budget=3)
    assert OPT.should_stop(s, res, StoppingCriteria()).reason == "budget_exhausted"
    s, res = trace_result([3, 2])
    d = OPT.should_stop(s, res, StoppingCriteria(cancelled=lambda: True, target_value=100))
    assert d.stop and d.reason == "user_cancelled"  # cancellation outranks everything
    assert OPT.should_stop(s, res, StoppingCriteria(), candidates_available=False).reason == "no_feasible_candidate"
    assert not OPT.should_stop(s, res, StoppingCriteria()).stop


def test_target_stop_only_counts_feasible_runs_and_respects_direction():
    s, res = trace_result([9, 1, 8], feasible=[True, False, True])  # the "1" violates the constraint
    assert OPT.should_stop(s, res, StoppingCriteria(target_value=2)).reason == "continue"
    s, res = trace_result([1, 5, 9], direction="maximize")
    assert OPT.should_stop(s, res, StoppingCriteria(target_value=9)).reason == "target_achieved"
