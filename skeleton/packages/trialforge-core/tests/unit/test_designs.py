import itertools
import math

import pytest

from trialforge import Constraint, DesignError, ExperimentDesigner, ExperimentSpec, Factor, MeasurementDefinition, Objective
from trialforge.designer import classical as cl

D = ExperimentDesigner()


def spec(factors, method="factorial", budget=500, seed=7, options=None, **kw):
    return ExperimentSpec(
        name="d",
        factors=factors,
        objectives=[Objective(metric="y", direction="minimize")],
        measurement_definitions=[MeasurementDefinition(metric="y", unit="u")],
        run_budget=budget,
        design_method=method,
        design_options=options or {},
        random_seed=seed,
        **kw,
    )


def cont(n, lo=0, hi=10):
    return Factor(name=n, factor_type="continuous", lower=lo, upper=hi)


def disc(n, v):
    return Factor(name=n, factor_type="discrete", values=v)


def cat(n, v):
    return Factor(name=n, factor_type="categorical", values=v)


# ---------------------------------------------------------------- full factorial / OFAT
def test_full_factorial_contains_exactly_the_cartesian_product_mixed_types():
    s = spec([cont("t", 100, 200), disc("d", [1, 2]), cat("m", ["x", "y", "z"])])
    plan = D.design(s)
    expected = {(t, d, m) for t in (100, 150.0, 200) for d in (1, 2) for m in "xyz"}
    got = [(r.configuration["t"], r.configuration["d"], r.configuration["m"]) for r in plan.runs]
    assert len(got) == len(set(got)) == 18 and set(got) == expected
    assert plan.design_info["complete"] and plan.design_info["full_grid_size"] == 18
    assert len({r.run_id for r in plan.runs}) == 18


def test_continuous_levels_option_and_replicates_and_center_points():
    s = spec([cont("a"), cont("b")], options={"continuous_levels": 2, "replicates": 3, "center_points": 2})
    plan = D.design(s)
    assert len(plan.runs) == 4 * 3 + 2 * 3 and plan.design_info["replicates"] == 3
    assert sum(bool(r.metadata.get("center_point")) for r in plan.runs) == 6
    assert len({r.run_id for r in plan.runs}) == len(plan.runs)
    assert {r.replicate for r in plan.runs} == {0, 1, 2}


def test_ofat_is_control_plus_single_factor_changes():
    s = spec([disc("a", [1, 2, 3]), disc("b", [10, 20]), cat("c", ["p", "q", "r", "s"])], method="ofat")
    plan = D.design(s)
    control = plan.control_configuration
    assert len(plan.runs) == 1 + 2 + 1 + 3
    for r in plan.runs:
        diffs = [k for k in control if r.configuration[k] != control[k]]
        assert len(diffs) <= 1


def test_no_silent_truncation_budget_errors_and_filter_is_disclosed():
    with pytest.raises(DesignError, match="exceeds budget"):
        D.design(spec([disc("a", [1, 2, 3]), disc("b", [1, 2, 3])], budget=8))
    with pytest.raises(DesignError, match="exceeds budget"):
        D.design(spec([disc("a", [1, 2])], budget=5, options={"replicates": 3}))
    filtered = D.design(spec([disc("a", [1, 2, 3]), disc("b", [1, 2])], constraints=[Constraint(metric="a", maximum=2)]))
    assert len(filtered.runs) == 4 and not filtered.design_info["complete"]
    assert filtered.design_info["constraint_filtered"] == 2
    assert "NOT a complete factorial" in filtered.design_info["limitations"][0]


def test_input_constraints_with_registered_function_filter_designs():
    from trialforge.definitions.registry import DEFAULT_PLUGINS

    DEFAULT_PLUGINS.register("constraint", "a_lt_b", lambda cfg, out: cfg["a"] < cfg["b"], replace=True)
    s = spec([disc("a", [1, 2, 3]), disc("b", [1, 2, 3])], constraints=[Constraint(kind="input", function="a_lt_b")])
    assert sorted((r.configuration["a"], r.configuration["b"]) for r in D.design(s).runs) == [(1, 2), (1, 3), (2, 3)]


# ----------------------------------------------------------------- reproducibility / randomisation
def test_same_seed_reproduces_and_different_seed_reorders_same_set():
    f = [disc("a", [1, 2, 3]), disc("b", [1, 2, 3]), cat("c", ["u", "v"])]
    base = spec(f, seed=5)
    p1, p2 = D.design(base), D.design(base)
    p3 = D.design(base.model_copy(update={"random_seed": 6}))
    assert [(r.run_id, r.configuration) for r in p1.runs] == [(r.run_id, r.configuration) for r in p2.runs]
    s1 = sorted(map(repr, (r.configuration for r in p1.runs)))
    assert s1 == sorted(map(repr, (r.configuration for r in p3.runs)))
    assert [r.configuration for r in p1.runs] != [r.configuration for r in p3.runs]


def test_randomisation_is_recorded_and_actually_shuffles():
    plan = D.design(spec([disc("a", list(range(6))), disc("b", [0, 1])], seed=3))
    std = plan.randomization["standard_order_before"]
    assert plan.randomization["scheme"].startswith("complete randomisation") and plan.random_seed == 3
    assert plan.randomization["run_order_standard_indices"] != std
    assert sorted(plan.randomization["run_order_standard_indices"]) == std
    assert [r.metadata["standard_order"] for r in plan.runs] == plan.randomization["run_order_standard_indices"]


def test_random_design_respects_domains_budget_and_seed():
    s = spec([cont("a", -1, 1), cat("c", ["p", "q"])], method="random", budget=25)
    p = D.design(s)
    assert len(p.runs) == 25 and all(-1 <= r.configuration["a"] <= 1 for r in p.runs)
    assert [r.configuration for r in p.runs] == [r.configuration for r in D.design(s).runs]


# ------------------------------------------------------------------------ fractional factorial
def coded_matrix(plan, names):
    return [[r.coded[n] for n in names] for r in plan.runs]


def test_half_fraction_2_4_1_has_correct_structure():
    fs = [cont("a"), cont("b"), cont("c"), cont("d")]
    plan = D.design(spec(fs, "fractional_factorial", budget=8, options={"generators": ["D=ABC"]}))
    m = coded_matrix(plan, "abcd")
    assert len(m) == 8
    for row in m:  # generator identity D = ABC
        assert row[3] == row[0] * row[1] * row[2]
    for i, j in itertools.combinations(range(4), 2):  # orthogonal columns
        assert sum(r[i] * r[j] for r in m) == 0
    assert all(sum(r[i] for r in m) == 0 for i in range(4))  # balanced
    info = plan.design_info
    assert info["defining_relation"] == ["I=ABCD"] and info["resolution"] == 4 and info["p"] == 1
    assert info["alias_structure"]["A"] == ["BCD"] and info["alias_structure"]["AB"] == ["CD"]
    assert not info["complete"] and "aliased" in info["limitations"][0]
    assert {r.configuration["a"] for r in plan.runs} == {0, 10}  # decoded onto real range


def test_fraction_2_5_2_defining_relation_and_resolution():
    fs = [cont(n) for n in "abcde"]
    plan = D.design(spec(fs, "fractional_factorial", budget=8, options={"generators": ["D=AB", "E=AC"]}))
    assert sorted(plan.design_info["defining_relation"], key=lambda s: (len(s), s)) == ["I=ABD", "I=ACE", "I=BCDE"]
    assert plan.design_info["resolution"] == 3 and len(plan.runs) == 8


def test_fraction_chosen_automatically_from_budget_and_min_resolution_enforced():
    fs = [cont(n) for n in "abcdef"]
    plan = D.design(spec(fs, "fractional_factorial", budget=16))
    assert len(plan.runs) == 16 and plan.design_info["resolution"] == 4
    with pytest.raises(DesignError, match="resolution"):
        D.design(spec(fs, "fractional_factorial", budget=8, options={"min_resolution": 4}))
    with pytest.raises(DesignError):
        D.design(spec(fs, "fractional_factorial", budget=2))


def test_fractional_invalid_inputs():
    with pytest.raises(DesignError):  # dependent generators
        cl.defining_relation(["D=ABC", "D=ABC"])
    with pytest.raises(DesignError):
        cl.fractional_factorial(4, ["E=ABC"])
    with pytest.raises(DesignError, match="at least 3"):
        D.design(spec([cont("a"), cont("b")], "fractional_factorial"))
    with pytest.raises(DesignError, match="exactly 2 levels"):
        D.design(spec([cont("a"), cont("b"), cat("c", ["x", "y", "z"])], "fractional_factorial"))


def test_fractional_handles_categorical_two_level_and_center_points():
    fs = [cont("a"), cat("m", ["lo", "hi"]), disc("n", [5, 15])]
    plan = D.design(spec(fs, "fractional_factorial", options={"center_points": 2}))
    assert len(plan.runs) == 8 + 2
    centers = [r for r in plan.runs if r.metadata.get("center_point")]
    assert all(r.configuration["a"] == 5 for r in centers)
    assert {r.configuration["m"] for r in plan.runs} == {"lo", "hi"}


def test_structured_design_with_violated_input_constraint_is_rejected_not_filtered():
    fs = [cont("a"), cont("b"), cont("c")]
    with pytest.raises(DesignError, match="balance"):
        D.design(spec(fs, "fractional_factorial", constraints=[Constraint(metric="a", maximum=5)]))


# ------------------------------------------------------------------------------------ blocked
def test_blocked_design_with_block_factor_distinguishes_roles():
    fs = [cont("t", 0, 1), disc("n", [1, 2]), cat("day", ["mon", "tue", "wed"])]
    fs[2] = Factor(name="day", factor_type="categorical", values=["mon", "tue", "wed"], role="block")
    plan = D.design(spec(fs, "blocked_factorial", options={"continuous_levels": 2}, seed=2))
    assert plan.block_variables == ["day"] and plan.treatment_factors == ["t", "n"]
    assert len(plan.runs) == 3 * 4
    by_block = {}
    for r in plan.runs:
        by_block.setdefault(r.block["day"], []).append((r.configuration["t"], r.configuration["n"]))
    assert all(sorted(v) == sorted(itertools.product((0, 1), (1, 2))) for v in by_block.values())
    seq = [r.block["day"] for r in plan.runs]  # randomised within blocks, blocks contiguous
    assert seq == sorted(seq, key=["mon", "tue", "wed"].index)
    assert "within each block" in plan.randomization["scheme"]
    assert plan.design_info["complete"]


def test_confounded_blocking_2_3_in_two_blocks_confounds_abc():
    fs = [cont(n) for n in "abc"]
    plan = D.design(spec(fs, "blocked_factorial", options={"n_blocks": 2, "confound_blocks": True, "continuous_levels": 2}))
    assert plan.design_info["confounded_effects"] == ["ABC"] and plan.block_variables == ["block"]
    for blk in (0, 1):
        runs = [r for r in plan.runs if r.block["block"] == blk]
        assert len(runs) == 4
        assert len({r.coded["a"] * r.coded["b"] * r.coded["c"] for r in runs}) == 1  # ABC constant within block
    assert {r.coded["a"] * r.coded["b"] * r.coded["c"] for r in plan.runs} == {-1, 1}
    # main effects remain unconfounded: balanced within each block
    for blk in (0, 1):
        assert all(sum(r.coded[n] for r in plan.runs if r.block["block"] == blk) == 0 for n in "abc")


def test_replicated_complete_blocks_with_n_blocks():
    plan = D.design(spec([disc("a", [1, 2]), disc("b", [1, 2])], "blocked_factorial", options={"n_blocks": 3}))
    assert len(plan.runs) == 12 and sorted({r.block["block"] for r in plan.runs}) == [0, 1, 2]


def test_blocked_requires_blocks():
    with pytest.raises(DesignError, match="block"):
        D.design(spec([disc("a", [1, 2])], "blocked_factorial"))


# ----------------------------------------------------------------------- response surface
def test_central_composite_face_centered_counts_and_bounds():
    fs = [cont("a", 0, 10), cont("b", 100, 200), cont("c", -5, 5)]
    plan = D.design(spec(fs, "central_composite", options={"center_points": 4}))
    assert len(plan.runs) == 8 + 6 + 4
    kinds = [r.metadata["point_kind"] for r in plan.runs]
    assert kinds.count("factorial") == 8 and kinds.count("axial") == 6 and kinds.count("center") == 4
    for r in plan.runs:
        for f in fs:
            assert f.lower - 1e-9 <= r.configuration[f.name] <= f.upper + 1e-9


def test_central_composite_rotatable_is_inscribed_within_bounds():
    fs = [cont("a"), cont("b"), cont("c")]
    plan = D.design(spec(fs, "central_composite", options={"alpha": "rotatable"}))
    alpha = plan.design_info["alpha"]
    assert math.isclose(alpha, 8**0.25) and plan.design_info["inscribed"]
    axial = [r for r in plan.runs if r.metadata["point_kind"] == "axial"]
    assert all(max(abs(v) for v in r.coded.values()) == pytest.approx(1.0) for r in axial)  # axial on bounds
    fact = [r for r in plan.runs if r.metadata["point_kind"] == "factorial"]
    assert all(abs(v) == pytest.approx(1 / alpha) for r in fact for v in r.coded.values())  # ratio axial:factorial = alpha


def test_box_behnken_structure():
    fs = [cont("a"), cont("b"), cont("c")]
    plan = D.design(spec(fs, "box_behnken", options={"center_points": 3}))
    assert len(plan.runs) == 12 + 3
    edge = [r for r in plan.runs if r.metadata["point_kind"] == "edge"]
    assert all(sorted(abs(v) for v in r.coded.values()) == [0, 1, 1] for r in edge)  # no corner points
    assert len(D.design(spec([cont(n) for n in "abcd"], "box_behnken", options={"center_points": 3})).runs) == 24 + 3


def test_response_surface_designs_reject_incompatible_factors_and_budget():
    with pytest.raises(DesignError, match="continuous"):
        D.design(spec([cont("a"), disc("b", [1, 2]), cont("c")], "box_behnken"))
    with pytest.raises(DesignError, match="3 to 5"):
        D.design(spec([cont("a"), cont("b")], "box_behnken"))
    with pytest.raises(DesignError, match="exceeds budget"):
        D.design(spec([cont("a"), cont("b"), cont("c")], "central_composite", budget=10))
    assert D.check(
        spec(
            [cont("a"), cat("b", ["x", "y"])],
            "factorial",
        ),
        "central_composite",
    )


# ------------------------------------------------------------------------------- sequential
def test_sequential_extension_respects_budget_domains_and_constraints():
    s = spec([cont("a", 0, 1), disc("b", [1, 2])], budget=3, constraints=[Constraint(metric="b", equals=1)])
    from conftest import build_result

    res = build_result(s, [({"a": 0.1, "b": 1}, 1.0), ({"a": 0.2, "b": 1}, 2.0)])
    plan = D.sequential(s, res, [{"a": 0.5, "b": 1}], iteration=2)
    assert plan.method == "sequential" and plan.runs[0].order == 2 and plan.design_info["remaining_budget_after"] == 0
    with pytest.raises(DesignError, match="remaining budget"):
        D.sequential(s, res, [{"a": 0.5, "b": 1}, {"a": 0.6, "b": 1}])
    with pytest.raises(DesignError, match="violates"):
        D.sequential(s, res, [{"a": 0.5, "b": 2}])
    with pytest.raises(DesignError, match="exactly"):
        D.sequential(s, res, [{"a": 0.5}])


def test_unsupported_method_listed():
    with pytest.raises(DesignError, match="unsupported design method"):
        D.design(spec([disc("a", [1, 2])]), method="latin_hypercube")
