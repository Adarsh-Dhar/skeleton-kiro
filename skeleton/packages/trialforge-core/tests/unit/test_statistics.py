"""Statistical correctness against independent references.

References: scipy.stats (t-tests, ANOVA), numpy.linalg.lstsq (OLS), closed-form formulas for balanced
factorials, statsmodels (when installed), and analytically constructed datasets with known effects.
"""

import itertools
import math

import numpy as np
import pytest
from conftest import build_result, simple_spec
from scipy import stats as sps

from trialforge import AnalysisEngine, Constraint, Factor, MeasurementDefinition, Objective
from trialforge.analysis import stats as st

A = AnalysisEngine()


def fac(n, v):
    return Factor(name=n, factor_type="discrete", values=v)


# ------------------------------------------------------------------ descriptive
def test_descriptive_statistics_match_numpy():
    x = [2.0, 4.0, 4.0, 5.0, 9.0, 11.5]
    d = st.describe(x)
    assert d["mean"] == pytest.approx(np.mean(x)) and d["median"] == pytest.approx(np.median(x))
    assert d["variance"] == pytest.approx(np.var(x, ddof=1)) and d["std_dev"] == pytest.approx(np.std(x, ddof=1))
    assert d["std_error"] == pytest.approx(np.std(x, ddof=1) / math.sqrt(6))
    assert (d["min"], d["max"], d["range"], d["count"]) == (2.0, 11.5, 9.5, 6)
    assert st.describe([3.0])["std_dev"] is None and st.describe([])["count"] == 0


# ------------------------------------------------------------------ Welch / paired / CI
def test_welch_matches_scipy_including_df_and_ci():
    a, b = [5.1, 4.9, 5.3, 5.0], [6.0, 6.4, 5.8, 6.1, 6.3]
    w = st.welch(a, b)
    r = sps.ttest_ind(b, a, equal_var=False)
    ci = r.confidence_interval(0.95)
    assert w["t"] == pytest.approx(r.statistic) and w["p"] == pytest.approx(r.pvalue) and w["df"] == pytest.approx(r.df)
    assert w["ci"] == pytest.approx((ci.low, ci.high))


def test_paired_matches_scipy():
    a, b = [10, 12, 9, 11, 13], [12, 15, 10, 14, 15]
    p = st.paired(a, b)
    r = sps.ttest_rel(b, a)
    ci = r.confidence_interval(0.95)
    assert p["t"] == pytest.approx(r.statistic) and p["p"] == pytest.approx(r.pvalue)
    assert p["ci"] == pytest.approx((ci.low, ci.high))


def test_t_interval_is_wider_than_normal_for_small_samples_and_has_nominal_coverage():
    """A 1.96-based interval would cover ~88% at n=4; the t/Welch interval must cover ~95%."""
    rng = np.random.default_rng(12345)
    cover_t = cover_z = 0
    N = 3000
    for _ in range(N):
        a, b = rng.normal(0, 1, 4), rng.normal(0.5, 1, 4)
        w = st.welch(a, b)
        cover_t += w["ci"][0] <= 0.5 <= w["ci"][1]
        h = 1.96 * w["se"]
        cover_z += w["diff"] - h <= 0.5 <= w["diff"] + h
    assert 0.935 <= cover_t / N <= 0.965
    assert cover_z / N < 0.92  # demonstrates the defect the old implementation had


def test_insufficient_replication_withholds_inference():
    assert st.welch([1.0], [2.0, 3.0]) is None and st.paired([1.0], [2.0]) is None
    s = simple_spec([fac("x", [0, 1])])
    res = build_result(s, [({"x": 0}, 5.0), ({"x": 1}, 8.0)], control={"x": 0})
    e = A.analyse(res).effects[0]
    assert e.mean_difference == 3.0 and e.evidence == "insufficient"
    assert e.confidence_interval_95 is None and e.p_value is None and e.standard_error is None
    assert any("insufficient replication" in w for w in e.warnings)


def test_zero_variance_groups_do_not_fabricate_intervals():
    w = st.welch([5, 5, 5], [7, 7, 7])
    assert w["ci"] is None and w["degenerate"]


def test_bootstrap_ci_is_seeded_and_brackets_true_difference():
    rng = np.random.default_rng(1)
    a, b = rng.normal(10, 1, 30), rng.normal(12, 1, 30)
    c1, c2 = st.bootstrap_ci_diff(a, b, seed=4), st.bootstrap_ci_diff(a, b, seed=4)
    assert c1 == c2 and c1[0] < 2.0 < c1[1]
    assert st.bootstrap_ci_diff([1, 2], [3, 4, 5]) is None


def test_hedges_g_against_formula():
    a, b = [1.0, 2.0, 3.0, 4.0], [3.0, 4.0, 5.0, 8.0]
    sp = math.sqrt((3 * np.var(a, ddof=1) + 3 * np.var(b, ddof=1)) / 6)
    d = (np.mean(b) - np.mean(a)) / sp
    assert st.hedges_g(a, b) == pytest.approx(d * (1 - 3 / (4 * 8 - 9)))


def test_holm_adjustment_reference_values():
    adj = st.holm_adjust([0.01, 0.04, 0.03, None, 0.005])
    # sorted valid p: .005,.01,.03,.04 -> x4,x3,x2,x1 = .02,.03,.06,.04 -> running max => .02,.03,.06,.06
    assert adj[3] is None and adj[4] == pytest.approx(0.02) and adj[0] == pytest.approx(0.03)
    assert adj[2] == pytest.approx(0.06) and adj[1] == pytest.approx(0.06)


# ------------------------------------------------------------- treatments are never pooled
def test_each_treatment_is_compared_separately_with_the_control():
    """Level 1 raises y by +5, level 2 lowers it by -3. A pooled 'treatment' mean would show +1."""
    s = simple_spec([fac("a", [0, 1, 2])])
    base = [10.0, 10.4, 9.6]
    rows = [({"a": 0}, v) for v in base] + [({"a": 1}, v + 5) for v in base] + [({"a": 2}, v - 3) for v in base]
    effects = A.analyse(build_result(s, rows, control={"a": 0})).effects
    by_level = {e.level: e for e in effects}
    assert set(by_level) == {1, 2}
    assert by_level[1].mean_difference == pytest.approx(5.0) and by_level[2].mean_difference == pytest.approx(-3.0)
    assert by_level[1].factor == "a" and by_level[1].control_level == 0
    assert by_level[1].sample_size_treatment == 3 == by_level[1].sample_size_control
    assert set(by_level[1].treatment_run_ids).isdisjoint(by_level[2].treatment_run_ids)
    assert by_level[1].comparison_type == "treatment_vs_control"
    t = sps.ttest_ind([v + 5 for v in base], base, equal_var=False)
    assert by_level[1].p_value == pytest.approx(t.pvalue)


def test_multi_factor_comparisons_identify_exactly_which_factor_differs():
    s = simple_spec([fac("a", [0, 1]), fac("b", [0, 1])])
    rows = []
    for (a, b), shift in {(0, 0): 0, (1, 0): 2, (0, 1): -1, (1, 1): 7}.items():
        rows += [({"a": a, "b": b}, 10 + shift + d) for d in (-0.2, 0.0, 0.2)]
    res = A.analyse(build_result(s, rows, control={"a": 0, "b": 0}))
    cmp_ = {tuple(e.differing_factors): e for e in res.effects}
    assert cmp_[("a",)].mean_difference == pytest.approx(2) and cmp_[("a",)].comparison_type == "treatment_vs_control"
    assert cmp_[("b",)].mean_difference == pytest.approx(-1)
    assert cmp_[("a", "b")].mean_difference == pytest.approx(7) and cmp_[("a", "b")].comparison_type == "configuration"
    assert len(res.effects) == 3


def test_metrics_are_never_mixed_in_effects_or_factor_effects():
    s = simple_spec(
        [fac("a", [0, 1])],
        measurement_definitions=[MeasurementDefinition(metric="y", unit="u"), MeasurementDefinition(metric="w", unit="u")],
        objectives=[Objective(metric="y", direction="minimize"), Objective(metric="w", direction="maximize")],
    )
    rows = [({"a": a}, {"y": 1.0 + a + d, "w": 100.0 - 10 * a + d}) for a in (0, 1) for d in (0.0, 0.1, -0.1)]
    res = A.analyse(build_result(s, rows, control={"a": 0}))
    assert {e.metric: round(e.mean_difference, 6) for e in res.effects} == {"y": 1.0, "w": -10.0}
    assert res.factor_effects_by_metric["y"]["a"]["1"] == pytest.approx(2.0)
    assert res.factor_effects_by_metric["w"]["a"]["1"] == pytest.approx(90.0)


# ------------------------------------------------------- balanced factorial: effects, ANOVA, interactions
def factorial_rows(noise_pattern=(-0.3, 0.0, 0.3)):
    """y = 10 + 2A + 3B + 4AB in coded +-1 units, three replicates with mean-zero noise."""
    rows = []
    for a, b in itertools.product((-1, 1), repeat=2):
        for d in noise_pattern:
            rows.append(({"a": (a + 1) // 2, "b": (b + 1) // 2}, 10 + 2 * a + 3 * b + 4 * a * b + d))
    return rows


def test_main_and_interaction_effects_recover_the_known_model():
    s = simple_spec([fac("a", [0, 1]), fac("b", [0, 1])])
    res = A.analyse(build_result(s, factorial_rows(), control={"a": 0, "b": 0}))
    reg = res.regression[0]
    coef = {c.term: c for c in reg.coefficients}
    assert coef["Intercept"].estimate == pytest.approx(10.0)
    assert coef["a"].estimate == pytest.approx(2.0) and coef["b"].estimate == pytest.approx(3.0)
    assert coef["a:b"].estimate == pytest.approx(4.0)
    assert reg.n_observations == 12 and reg.residual_df == 8
    ie = res.interactions[0]
    assert ie.factors == ("a", "b") and ie.estimate == pytest.approx(16.0)  # (19-7) - (5-9)
    assert ie.p_value < 1e-10 and ie.degrees_of_freedom == (1, 8)
    me = {e.level: e for e in res.main_effects if e.factor == "a"}
    assert me[1].mean_difference == pytest.approx((19 + 5) / 2 - (7 + 9) / 2)  # marginal level means


def test_type2_anova_balanced_2x2_matches_closed_form():
    s = simple_spec([fac("a", [0, 1]), fac("b", [0, 1])])
    rows = factorial_rows()
    res = A.analyse(build_result(s, rows, control={"a": 0, "b": 0}))
    tab = next(t for t in res.anova if t.kind == "factorial")
    N = len(rows)
    y = np.array([v for _, v in rows])
    a = np.array([2 * c["a"] - 1 for c, _ in rows])
    b = np.array([2 * c["b"] - 1 for c, _ in rows])
    ss = {"a": (a @ y) ** 2 / N, "b": (b @ y) ** 2 / N, "a:b": ((a * b) @ y) ** 2 / N}  # orthogonal contrasts
    cell_means = {(i, j): y[(a == i) & (b == j)].mean() for i in (-1, 1) for j in (-1, 1)}
    sse = sum((v - cell_means[(i, j)]) ** 2 for v, i, j in zip(y, a, b))
    got = {r.term: r for r in tab.rows}
    for term in ss:
        assert got[term].sum_squares == pytest.approx(ss[term]) and got[term].df == 1
        assert got[term].f_statistic == pytest.approx(ss[term] / (sse / 8))
        assert got[term].p_value == pytest.approx(sps.f.sf(ss[term] / (sse / 8), 1, 8))
    assert got["Residual"].sum_squares == pytest.approx(sse) and tab.residual_df == 8


def test_one_way_anova_matches_scipy_f_oneway():
    rng = np.random.default_rng(3)
    groups = {0: rng.normal(5, 1, 6), 1: rng.normal(6, 1, 6), 2: rng.normal(8, 1, 6)}
    s = simple_spec([fac("a", [0, 1, 2])])
    rows = [({"a": k}, float(v)) for k, g in groups.items() for v in g]
    tab = next(t for t in A.analyse(build_result(s, rows, control={"a": 0})).anova if t.kind == "one_way")
    ref = sps.f_oneway(*groups.values())
    assert tab.rows[0].f_statistic == pytest.approx(ref.statistic) and tab.rows[0].p_value == pytest.approx(ref.pvalue)
    assert tab.residual_df == 15


def test_regression_matches_numpy_lstsq_with_continuous_and_categorical_factors():
    rng = np.random.default_rng(8)
    xs = rng.uniform(0, 10, 24)
    cats = ["p", "q", "r"] * 8
    y = 3 + 0.8 * xs + np.array([{"p": 0, "q": 2, "r": -1}[c] for c in cats]) + rng.normal(0, 0.3, 24)
    s = simple_spec(
        [
            Factor(name="x", factor_type="continuous", lower=0, upper=10),
            Factor(name="c", factor_type="categorical", values=["p", "q", "r"]),
        ]
    )
    rows = [({"x": float(x), "c": c}, float(v)) for x, c, v in zip(xs, cats, y)]
    res = A.analyse(build_result(s, rows, control={"x": 0.0, "c": "p"}, method="random"))
    reg = res.regression[0]
    # independent design matrix: intercept, coded x, dummies q, r, and x:q, x:r interactions (engine fits 2-way)
    xc = 2 * xs / 10 - 1
    dq = np.array([c == "q" for c in cats], float)
    dr = np.array([c == "r" for c in cats], float)
    X = np.column_stack([np.ones(24), xc, dq, dr, xc * dq, xc * dr])
    beta, res_, *_ = np.linalg.lstsq(X, y, rcond=None)
    got = np.array([c.estimate for c in reg.coefficients])
    assert got == pytest.approx(beta, abs=1e-8)
    resid = y - X @ beta
    assert reg.r_squared == pytest.approx(1 - resid @ resid / ((y - y.mean()) ** 2).sum())
    sigma2 = resid @ resid / (24 - 6)
    se = np.sqrt(np.diag(sigma2 * np.linalg.inv(X.T @ X)))
    assert [c.std_error for c in reg.coefficients] == pytest.approx(se)
    assert reg.diagnostics["shapiro_residual_p"] is not None and "durbin_watson" in reg.diagnostics


def test_statsmodels_cross_check_if_available():
    sm = pytest.importorskip("statsmodels.api")
    smf = pytest.importorskip("statsmodels.formula.api")
    pd = pytest.importorskip("pandas")
    rng = np.random.default_rng(1)
    rows = []
    for rep in range(3):
        for a, b, c in itertools.product((0, 1), (0, 1), "xyz"):
            rows.append(
                (
                    {"a": a, "b": b, "c": c},
                    10 + 2 * a - 1.5 * b + {"x": 0, "y": 1, "z": 0.5}[c] + 1.2 * a * b + rng.normal(0, 0.4),
                )
            )
    s = simple_spec([fac("a", [0, 1]), fac("b", [0, 1]), Factor(name="c", factor_type="categorical", values=list("xyz"))])
    tab = next(t for t in A.analyse(build_result(s, rows, control={"a": 0, "b": 0, "c": "x"})).anova if t.kind == "factorial")
    df = pd.DataFrame([{**c, "y": v} for c, v in rows])
    ref = sm.stats.anova_lm(smf.ols("y ~ (C(a)+C(b)+C(c))**2", df).fit(), typ=2)
    names = {"C(a)": "a", "C(b)": "b", "C(c)": "c", "C(a):C(b)": "a:b", "C(a):C(c)": "a:c", "C(b):C(c)": "b:c"}
    got = {r.term: r for r in tab.rows}
    for k, v in names.items():
        assert got[v].sum_squares == pytest.approx(ref.loc[k, "sum_sq"]) and got[v].f_statistic == pytest.approx(ref.loc[k, "F"])
        assert got[v].p_value == pytest.approx(ref.loc[k, "PR(>F)"])


def test_unreplicated_factorial_drops_interaction_with_explicit_assumption_warning():
    s = simple_spec([fac("a", [0, 1]), fac("b", [0, 1])])
    rows = [({"a": a, "b": b}, 1.0 + 2 * a + 3 * b + 4 * a * b) for a, b in itertools.product((0, 1), repeat=2)]
    res = A.analyse(build_result(s, rows, control={"a": 0, "b": 0}))
    tab = next(t for t in res.anova if t.kind == "factorial")
    assert {r.term for r in tab.rows} == {"a", "b", "Residual"} and tab.residual_df == 1
    assert any("valid only if interactions are absent" in w for w in tab.warnings)
    assert res.interactions[0].p_value is None and any("not tested" in w for w in res.interactions[0].warnings)


def test_truly_saturated_model_withholds_all_inference():
    s = simple_spec([fac("a", [0, 1]), fac("b", [0, 1])])
    rows = [({"a": 0, "b": 0}, 1.0), ({"a": 1, "b": 0}, 3.0), ({"a": 0, "b": 1}, 4.0)]
    res = A.analyse(build_result(s, rows, control={"a": 0, "b": 0}))
    tab = next(t for t in res.anova if t.kind == "factorial")
    assert tab.rows == [] and any("saturated" in w for w in tab.warnings)
    reg = res.regression[0]
    assert reg.residual_df == 0 and all(c.std_error is None and c.p_value is None for c in reg.coefficients)
    assert any("no residual degrees of freedom" in w for w in reg.warnings)


def test_fractional_design_aliasing_is_reported_and_dependent_terms_dropped():
    from trialforge import ExperimentDesigner

    fs = [Factor(name=n, factor_type="continuous", lower=-1, upper=1) for n in "abcd"]
    s = simple_spec(fs, design_method="fractional_factorial", design_options={"generators": ["D=ABC"], "replicates": 2})
    plan = ExperimentDesigner().design(s)
    rows = [(r.configuration, 5 + 2 * r.coded["a"] - r.coded["b"] + (0.1 if r.replicate else -0.1)) for r in plan.runs]
    res = A.analyse(build_result(s, rows, control=plan.control_configuration, method="fractional_factorial"))
    res.limitations  # noqa
    reg = res.regression[0]
    assert any("aliased" in w for w in reg.warnings)
    coef = {c.term: c.estimate for c in reg.coefficients}
    assert coef["a"] == pytest.approx(2.0) and coef["b"] == pytest.approx(-1.0) and coef["c"] == pytest.approx(0.0, abs=1e-9)


def test_paired_comparison_used_when_runs_are_blocked():
    s = simple_spec([fac("a", [0, 1])])
    ctl, trt = [10.0, 12.0, 9.0, 11.0], [12.5, 14.0, 10.5, 13.5]
    rows, blocks = [], []
    for i, (c, t) in enumerate(zip(ctl, trt)):
        rows += [({"a": 0}, c), ({"a": 1}, t)]
        blocks += [{"day": i}, {"day": i}]
    res = A.analyse(build_result(s, rows, control={"a": 0}, method="blocked_factorial", blocks=blocks))
    e = res.effects[0]
    ref = sps.ttest_rel(trt, ctl)
    assert e.comparison_type == "paired" and e.ci_method == "paired t"
    assert e.p_value == pytest.approx(ref.pvalue) and e.t_statistic == pytest.approx(ref.statistic)
    assert e.confidence_interval_95 == pytest.approx(tuple(ref.confidence_interval(0.95)))


def test_block_effects_are_modelled_additively():
    s = simple_spec([fac("a", [0, 1])])
    rows, blocks = [], []
    for blk, shift in enumerate([0, 10, -5]):
        for a in (0, 1):
            for d in (-0.1, 0.1):
                rows.append(({"a": a}, 5 + 2 * a + shift + d))
                blocks.append({"day": blk})
    reg = A.analyse(build_result(s, rows, control={"a": 0}, method="blocked_factorial", blocks=blocks)).regression[0]
    coef = {c.term: c.estimate for c in reg.coefficients}
    assert coef["a"] == pytest.approx(1.0)  # coded: low -> -1, high -> +1 so slope = effect / 2
    assert any(t.startswith("block:day") for t in coef)
    assert reg.residual_df == 12 - 4


def test_outlier_run_flagged_via_studentised_residual_not_removed():
    rng = np.random.default_rng(2)
    s = simple_spec([Factor(name="x", factor_type="continuous", lower=0, upper=10)])
    xs = np.linspace(0, 10, 20)
    ys = 1 + 0.5 * xs + rng.normal(0, 0.05, 20)
    ys[7] += 6.0
    rows = [({"x": float(x)}, float(y)) for x, y in zip(xs, ys)]
    res = A.analyse(build_result(s, rows, control={"x": 0.0}, method="random"))
    assert "r7" in [f["run_id"] for f in res.outlier_flags]
    assert res.sample_sizes["y"] == 20  # nothing deleted


def test_response_surface_regression_includes_quadratic_terms():
    from trialforge import ExperimentDesigner

    fs = [
        Factor(name="a", factor_type="continuous", lower=0, upper=10),
        Factor(name="b", factor_type="continuous", lower=0, upper=10),
    ]
    s = simple_spec(fs, design_method="central_composite", design_options={"center_points": 4})
    plan = ExperimentDesigner().design(s)
    rows = [
        (r.configuration, 20 - 3 * r.coded["a"] ** 2 + 2 * r.coded["b"] + 0.5 * r.coded["a"] * r.coded["b"]) for r in plan.runs
    ]
    reg = A.analyse(build_result(s, rows, control=plan.control_configuration, method="central_composite")).regression[0]
    coef = {c.term: c.estimate for c in reg.coefficients}
    assert coef["a^2"] == pytest.approx(-3.0) and coef["b"] == pytest.approx(2.0) and coef["a:b"] == pytest.approx(0.5)
    assert "quadratic" in reg.coding


# -------------------------------------------------------- constraint-aware multi-objective analysis
def mo_spec(**kw):
    return simple_spec(
        [fac("a", list(range(6)))],
        measurement_definitions=[
            MeasurementDefinition(metric="time", unit="s"),
            MeasurementDefinition(metric="cost", unit="u"),
            MeasurementDefinition(metric="stab", unit="u"),
        ],
        objectives=[Objective(metric="time", direction="minimize"), Objective(metric="cost", direction="minimize")],
        **kw,
    )


def test_pareto_front_matches_hand_computation():
    pts = {"A": [1, 5], "B": [2, 3], "C": [3, 4], "D": [4, 1], "E": [4, 4], "F": [2, 3]}
    from trialforge.evaluation import pareto_front

    assert sorted(pareto_front(pts)) == ["A", "B", "D", "F"]  # C dominated by B, E by B; B and F tie


def test_infeasible_run_is_never_best_or_on_the_front_even_if_better_on_all_objectives():
    s = mo_spec(constraints=[Constraint(metric="stab", minimum=50)])
    data = {
        0: (10, 10, 60),
        1: (8, 12, 55),
        2: (12, 6, 70),
        3: (5, 4, 10),  # run 3 dominates everything but is unstable
        4: (11, 11, 80),
    }
    rows = [({"a": k}, {"time": t, "cost": c, "stab": st_}) for k, (t, c, st_) in data.items()]
    res = A.analyse(build_result(s, rows, control={"a": 0}))
    assert "r3" not in res.pareto_run_ids and res.best_run_id != "r3"
    assert sorted(res.pareto_run_ids) == ["r0", "r1", "r2"]  # r4 (11, 11) is dominated by r0 (10, 10)
    assert set(res.feasible_run_ids) == {"r0", "r1", "r2", "r4"}
    inf = res.infeasible_runs[0]
    assert inf["run_id"] == "r3" and inf["violations"][0]["constraint"] == "stab"
    assert inf["better_than_best_feasible_on_all_objectives"] is True
    assert all(t["feasible"] for t in res.tradeoffs)


def test_unknown_constraint_outcome_is_not_treated_as_feasible():
    s = mo_spec(constraints=[Constraint(metric="stab", minimum=50)])
    rows = [({"a": 0}, {"time": 1.0, "cost": 1.0}), ({"a": 1}, {"time": 2.0, "cost": 2.0, "stab": 60.0})]
    res = A.analyse(build_result(s, rows, control={"a": 0}))
    assert res.feasible_run_ids == ["r1"] and res.best_run_id == "r1"


def test_no_feasible_run_gives_no_best():
    s = mo_spec(constraints=[Constraint(metric="stab", minimum=100)])
    rows = [({"a": k}, {"time": 1.0 + k, "cost": 1.0, "stab": 5.0}) for k in range(3)]
    res = A.analyse(build_result(s, rows, control={"a": 0}))
    assert res.best_run_id is None and res.pareto_run_ids == [] and len(res.infeasible_runs) == 3


def test_target_and_maximize_objectives_are_oriented_correctly():
    s = simple_spec([fac("a", [0, 1, 2, 3])], objectives=[Objective(metric="y", direction="target", target=5)])
    rows = [({"a": k}, v) for k, v in enumerate([1.0, 4.8, 9.0, 5.6])]
    assert A.analyse(build_result(s, rows, control={"a": 0})).best_run_id == "r1"
    s2 = simple_spec([fac("a", [0, 1, 2])], direction="maximize")
    rows2 = [({"a": k}, v) for k, v in enumerate([1.0, 9.0, 4.0])]
    assert A.analyse(build_result(s2, rows2, control={"a": 0})).best_run_id == "r1"


# ----------------------------------------------------------------------- metadata & exclusions
def test_analysis_records_inputs_exclusions_assumptions_and_warnings():
    from trialforge import RunStatus

    s = simple_spec([fac("a", [0, 1])])
    res = build_result(s, [({"a": 0}, 1.0), ({"a": 0}, 1.2), ({"a": 1}, 2.0), ({"a": 1}, 2.2), ({"a": 1}, 9.0)], control={"a": 0})
    res.runs[4].status = RunStatus.FAILED
    res.runs[0].processed_measurements = []  # no data at all
    out = A.analyse(res)
    reasons = {e["run_id"]: e["reason"] for e in out.excluded_runs}
    assert "failed" in reasons["r4"] and "no usable" in reasons["r0"]
    assert out.input_run_ids == ["r1", "r2", "r3"] and out.sample_sizes["y"] == 3
    assert out.algorithm == "trialforge.analysis" and out.algorithm_version
    assert out.assumptions and out.limitations and out.parameters["alpha"] == 0.05
    assert any("causal" in l for l in out.limitations)
    assert out.effects[0].sample_size_control == 1 and out.effects[0].evidence == "insufficient"


def test_analysis_is_reproducible_from_raw_measurements_alone():
    """Run outcomes are rebuilt from raw samples (incl. a millisecond sample converted to seconds)."""
    from trialforge import DesignPlan, ExperimentResult, Measurement, Run, RunStatus

    s = simple_spec([fac("a", [0, 1])], measurement_definitions=[MeasurementDefinition(metric="y", unit="s")])
    raw = [(0, [10, 11]), (0, [10.5, 11.5]), (1, [13, 14]), (1, [12, 15])]
    runs = []
    for i, (a, vals) in enumerate(raw):
        ms = [Measurement(run_id=f"r{i}", metric="y", value=v, unit="s") for v in vals]
        ms.append(Measurement(run_id=f"r{i}", metric="y", value=vals[0] * 1000, unit="ms"))  # same instant value, other unit
        runs.append(
            Run(
                run_id=f"r{i}",
                experiment_id=s.experiment_id,
                configuration={"a": a},
                order=i,
                status=RunStatus.COMPLETED,
                measurements=ms,
            )
        )
    plan = DesignPlan(experiment_id=s.experiment_id, method="factorial", runs=runs, control_configuration={"a": 0}, random_seed=0)
    res = ExperimentResult(spec=s, plan=plan, runs=runs, status="completed")
    a1, a2 = A.analyse(res), A.analyse(res)
    assert a1.model_dump(exclude={"created_at"}) == a2.model_dump(exclude={"created_at"})
    expected = [(v0 + v1 + v0) / 3 for _, (v0, v1) in raw]  # mean of the 2 samples + the converted ms sample
    assert a1.descriptive["y"]["mean"] == pytest.approx(np.mean(expected)) and a1.descriptive["y"]["count"] == 4
    e = a1.effects[0]
    assert e.mean_difference == pytest.approx(np.mean(expected[2:]) - np.mean(expected[:2]))
    assert runs[0].measurements[-1].unit == "ms" and runs[0].processed_measurements == []  # raw data untouched
