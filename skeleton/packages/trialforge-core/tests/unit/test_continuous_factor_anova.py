"""Continuous factors are encoded numerically in ANOVA, regression and level comparisons (regression tests for the F1 SETUP LAB
16-run experiment, where every factor had 16 distinct settings and the ANOVA was wrongly reported as unavailable)."""
import itertools

import numpy as np
import pytest
from conftest import build_result, simple_spec

from trialforge import AnalysisEngine, Factor

A = AnalysisEngine()
NAMES = ["front_wing", "rear_wing", "suspension", "tyre_pressure_psi"]
RANGES = {"front_wing": (10, 40), "rear_wing": (10, 40), "suspension": (1, 8), "tyre_pressure_psi": (18, 26)}


def cont(name):
    lo, hi = RANGES[name]
    return Factor(name=name, factor_type="continuous", lower=lo, upper=hi)


def truth(c):
    wing = c["front_wing"] + c["rear_wing"]
    return (34 - 0.12 * wing + 0.0015 * wing**2 + 0.06 * (c["suspension"] - 4.5) ** 2 + 0.05 * (c["tyre_pressure_psi"] - 21) ** 2)


def random_rows(n=16, seed=4):
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        cfg = {k: float(rng.uniform(*RANGES[k])) for k in NAMES}
        rows.append((cfg, truth(cfg) + float(rng.normal(0, 0.05))))
    return rows


def f1_result(n=16):
    spec = simple_spec([cont(k) for k in NAMES])
    return spec, A.analyse(build_result(spec, random_rows(n), method="random"))


def factorial_table(res):
    return next(t for t in res.anova if t.kind == "factorial")


# ---------------------------------------------------------------- A: the 16-run continuous experiment
def test_a_sixteen_run_continuous_experiment_has_anova_with_5_residual_df():
    spec, res = f1_result()
    reg, tab = res.regression[0], factorial_table(res)
    assert reg.n_observations == 16 and reg.n_parameters == 11 and reg.residual_df == 5
    assert tab.rows and tab.residual_df == 5 and tab.model_info["rank"] == 11 and tab.model_info["residual_df"] == 5
    terms = {r.term for r in tab.rows}
    assert set(NAMES) <= terms and len(terms - {"Residual"}) == 10  # 4 main effects + 6 two-factor interactions
    assert all(r.f_statistic is not None for r in tab.rows if r.term != "Residual")
    assert any("only 5 residual degrees of freedom" in w for w in tab.warnings)
    assert not any("saturated" in w for w in tab.warnings)
    assert tab.model_info["unique_values_per_factor"] == dict.fromkeys(NAMES, 16)
    assert tab.model_info["replicated_configurations"] == 0 and tab.model_info["pure_error_estimable"] is False


def test_a_continuous_factors_do_not_generate_level_comparisons():
    _, res = f1_result()
    assert res.main_effects == []
    assert [e for e in res.effects if e.comparison_type == "factor_level"] == []


def test_a_anova_matches_independent_type_ii_computation():
    spec, res = f1_result()
    tab = factorial_table(res)
    rows = random_rows()
    y = np.array([v for _, v in rows])
    X = {k: np.array([2 * (c[k] - RANGES[k][0]) / (RANGES[k][1] - RANGES[k][0]) - 1 for c, _ in rows]) for k in NAMES}
    terms = {(k,): X[k] for k in NAMES} | {(a, b): X[a] * X[b] for a, b in itertools.combinations(NAMES, 2)}

    def rss(cols):
        M = np.column_stack([np.ones(16)] + cols)
        beta, *_ = np.linalg.lstsq(M, y, rcond=None)
        return float(((y - M @ beta) ** 2).sum())

    full = rss(list(terms.values()))
    got = {r.term: r for r in tab.rows}
    assert got["Residual"].sum_squares == pytest.approx(full)
    for t, col in terms.items():
        others = [c for u, c in terms.items() if u != t and not set(t) < set(u)]
        assert got[":".join(t)].sum_squares == pytest.approx(rss(others) - rss(others + [col]), rel=1e-6, abs=1e-9)


# ---------------------------------------------------------------- B: categorical level comparisons
def test_b_categorical_factor_keeps_level_comparisons():
    rng = np.random.default_rng(1)
    spec = simple_spec([Factor(name="c", factor_type="categorical", values=["p", "q", "r"]), cont("front_wing")])
    rows = [({"c": c, "front_wing": float(x)}, {"p": 0, "q": 2, "r": -1}[c] + 0.1 * x + float(rng.normal(0, 0.2)))
            for c, x in zip(["p", "q", "r"] * 6, rng.uniform(10, 40, 18))]
    res = A.analyse(build_result(spec, rows, control={"c": "p", "front_wing": 10.0}, method="random"))
    assert {(e.factor, e.level) for e in res.main_effects} == {("c", "q"), ("c", "r")}


# ---------------------------------------------------------------- C: discrete numeric factor
def test_c_discrete_numeric_factor_encoding_is_documented_and_consistent():
    rng = np.random.default_rng(2)
    spec = simple_spec([Factor(name="d", factor_type="discrete", values=[1, 2, 4]), cont("front_wing")])
    rows = [({"d": d, "front_wing": float(x)}, 2 * d + 0.05 * x + float(rng.normal(0, 0.1)))
            for d, x in zip([1, 2, 4] * 6, rng.uniform(10, 40, 18))]
    res = A.analyse(build_result(spec, rows, control={"d": 1, "front_wing": 10.0}, method="random"))
    tab, reg = factorial_table(res), res.regression[0]
    assert {r.term: r.df for r in tab.rows}["d"] == 2  # ANOVA tests the declared levels as a whole
    assert "d" in reg.terms and not any(t.startswith("d[") for t in reg.terms)  # regression: one coded trend column
    assert "discrete factors" in reg.coding
    assert {(e.factor, e.level) for e in res.main_effects} == {("d", 2), ("d", 4)}  # level comparisons use the declared levels


# ---------------------------------------------------------------- D: mixed continuous and categorical
def test_d_mixed_factors_have_correct_rank_and_interaction_columns():
    rng = np.random.default_rng(3)
    spec = simple_spec([cont("front_wing"), Factor(name="c", factor_type="categorical", values=["p", "q", "r"])])
    rows = [({"front_wing": float(x), "c": c}, 1 + 0.1 * x + {"p": 0, "q": 1, "r": 3}[c] + float(rng.normal(0, 0.3)))
            for x, c in zip(rng.uniform(10, 40, 24), ["p", "q", "r"] * 8)]
    res = A.analyse(build_result(spec, rows, control={"front_wing": 10.0, "c": "p"}, method="random"))
    tab, reg = factorial_table(res), res.regression[0]
    assert reg.n_parameters == 6 and reg.residual_df == 18 and tab.residual_df == 18
    assert {r.term: r.df for r in tab.rows if r.term != "Residual"} == {"front_wing": 1, "c": 2, "front_wing:c": 2}
    assert [t for t in reg.terms if ":" in t] == ["front_wing:c[q]", "front_wing:c[r]"]


# ---------------------------------------------------------------- E: saturated
def test_e_saturated_model_withholds_f_tests_and_says_why():
    spec = simple_spec([cont("front_wing"), cont("rear_wing")])
    rows = [({"front_wing": 10.0, "rear_wing": 10.0}, 1.0), ({"front_wing": 40.0, "rear_wing": 10.0}, 2.0),
            ({"front_wing": 10.0, "rear_wing": 40.0}, 4.0)]
    tab = factorial_table(A.analyse(build_result(spec, rows, method="random")))
    assert tab.rows == [] and tab.residual_df == 0 and tab.model_info["saturated"] is True
    assert any("saturated" in w and "3 observations" in w for w in tab.warnings)


# ---------------------------------------------------------------- F: central composite
def ccd_rows(seed=5):
    rng = np.random.default_rng(seed)
    pts = [list(p) for p in itertools.product([-1, 1], repeat=4)]
    for i in range(4):
        for s in (-2, 2):
            p = [0] * 4
            p[i] = s
            pts.append(p)
    pts += [[0] * 4] * 4
    rows = []
    for p in pts:
        cfg = {k: (RANGES[k][0] + RANGES[k][1]) / 2 + v * (RANGES[k][1] - RANGES[k][0]) / 4 for k, v in zip(NAMES, p)}
        rows.append((cfg, truth(cfg) + float(rng.normal(0, 0.05))))
    return rows


def test_f_central_composite_includes_four_quadratic_terms_in_regression_and_anova():
    spec = simple_spec([Factor(name=k, factor_type="continuous", lower=RANGES[k][0] - (RANGES[k][1] - RANGES[k][0]) / 2,
                               upper=RANGES[k][1] + (RANGES[k][1] - RANGES[k][0]) / 2) for k in NAMES])
    res = A.analyse(build_result(spec, ccd_rows(), method="central_composite"))
    reg, tab = res.regression[0], factorial_table(res)
    assert len(reg.terms) == 15 and reg.residual_df == 13 and tab.residual_df == 13
    assert {f"{k}^2" for k in NAMES} <= set(reg.terms) and {f"{k}^2" for k in NAMES} <= {r.term for r in tab.rows}
    assert tab.model_info["quadratic_terms_included"] and tab.model_info["pure_error_df"] == 3
    assert tab.model_info["replicated_configurations"] == 1 and tab.model_info["rank"] == reg.diagnostics["model_info"]["rank"] == 15
    assert not any("no squared terms" in w for w in tab.warnings + reg.warnings)
    # Type II: a main effect is tested without its squared term and interactions
    assert {r.term: r.df for r in tab.rows}["front_wing"] == 1


def test_f_random_design_states_that_curvature_is_not_modelled():
    _, res = f1_result()
    assert any("no squared terms" in w for w in factorial_table(res).warnings + res.regression[0].warnings)


# ---------------------------------------------------------------- G: low residual df
def test_g_positive_but_low_residual_df_gives_anova_with_caution():
    spec = simple_spec([cont("front_wing"), cont("rear_wing"), cont("suspension")])
    rng = np.random.default_rng(9)
    rows = [({k: float(rng.uniform(*RANGES[k])) for k in ("front_wing", "rear_wing", "suspension")}, float(rng.normal())) for _ in range(9)]
    tab = factorial_table(A.analyse(build_result(spec, rows, method="random")))
    assert tab.residual_df == 2 and tab.rows
    assert any("only 2 residual degrees of freedom" in w for w in tab.warnings)
