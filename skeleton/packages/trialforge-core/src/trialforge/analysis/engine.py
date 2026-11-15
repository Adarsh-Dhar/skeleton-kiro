"""Statistical analysis of experiment results.

Principles
----------
* The unit of analysis is the **run** (its aggregated outcome), never the raw sample, to avoid
  pseudo-replication.
* Treatments are never pooled: each non-control configuration, and each factor level, is
  compared separately.
* No interval or p-value is produced unless the data support it; otherwise the estimate is
  labelled ``evidence='insufficient'`` with the reason.
* Associations in non-randomised data are not causal; limitations say so explicitly.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any

import numpy as np
from scipy import stats as sps

from trialforge.analysis import stats as st
from trialforge.definitions.registry import DEFAULT_PLUGINS, PluginRegistry
from trialforge.evaluation import (
    evaluate_constraints,
    is_feasible,
    objective_value,
    oriented_vector,
    pareto_front,
    run_outcomes,
    weighted_normalized,
)
from trialforge.measurements.engine import MeasurementEngine
from trialforge.models.core import (
    AnalysisResult,
    AnovaRow,
    AnovaTable,
    Coefficient,
    EffectEstimate,
    ExperimentResult,
    ExperimentSpec,
    Factor,
    InteractionEffect,
    RegressionModel,
    Run,
    RunStatus,
)

ALGORITHM_VERSION = "2.0"


def _key(config: dict[str, Any]) -> tuple:
    return tuple(sorted((k, repr(v)) for k, v in config.items()))


class AnalysisEngine:
    """Descriptive statistics, comparisons, ANOVA, regression, interactions and Pareto analysis."""

    def __init__(
        self,
        *,
        alpha: float = 0.05,
        bootstrap: bool = False,
        n_bootstrap: int = 2000,
        exclude_outliers: bool = False,
        plugins: PluginRegistry | None = None,
        max_configuration_comparisons: int = 500,
    ) -> None:
        if not 0 < alpha < 1:
            raise ValueError("alpha must be in (0, 1)")
        self.alpha, self.bootstrap, self.n_bootstrap = alpha, bootstrap, n_bootstrap
        self.exclude_outliers = exclude_outliers
        self.plugins = plugins or DEFAULT_PLUGINS
        self.max_cmp = max_configuration_comparisons

    # ------------------------------------------------------------ outcomes
    def _outcomes(self, run: Run, spec: ExperimentSpec) -> dict[str, float]:
        r = run
        if not run.processed_measurements and run.measurements:
            eng = MeasurementEngine(
                spec.measurement_definitions,
                derived_metrics=spec.derived_metrics,
                plugins=self.plugins,
                exclude_outliers=self.exclude_outliers,
            )
            _, assessed, _ = eng.assess(run.measurements, run.run_id)
            r = run.model_copy(update={"processed_measurements": eng.process(assessed, run.run_id)})
        return run_outcomes(r, exclude_outliers=self.exclude_outliers)

    # ---------------------------------------------------------------- main
    def analyse(
        self, result: ExperimentResult, *, alpha: float | None = None, bootstrap: bool | None = None, seed: int | None = None
    ) -> AnalysisResult:
        """Analyse an ExperimentResult: descriptive statistics, per-treatment and per-factor comparisons, ANOVA,
        regression, interactions and constraint-aware Pareto analysis. Returns an AnalysisResult recording inputs,
        exclusions, assumptions and warnings.
        """
        spec = result.spec
        alpha = alpha if alpha is not None else self.alpha
        bootstrap = self.bootstrap if bootstrap is None else bootstrap
        seed = spec.random_seed if seed is None else seed
        warnings: list[str] = []
        excluded: list[dict[str, Any]] = []
        outcomes: dict[str, dict[str, float]] = {}
        for run in result.runs:
            if run.status != RunStatus.COMPLETED:
                excluded.append({"run_id": run.run_id, "reason": f"run status is {run.status.value}"})
                continue
            o = self._outcomes(run, spec)
            if not o:
                excluded.append({"run_id": run.run_id, "reason": "no usable numeric measurements"})
                continue
            outcomes[run.run_id] = o
        runs = {r.run_id: r for r in result.runs}

        obj_metrics = [o.metric for o in spec.objectives if o.function is None]
        out_metrics = [c.metric for c in spec.output_constraints() if c.function is None and c.metric]
        inference_metrics = list(dict.fromkeys(obj_metrics + out_metrics))
        all_metrics = list(dict.fromkeys(inference_metrics + [m for o in outcomes.values() for m in o]))

        descriptive: dict[str, dict] = {}
        sample_sizes: dict[str, int] = {}
        for m in all_metrics:
            vals = [o[m] for o in outcomes.values() if m in o]
            descriptive[m] = st.describe(vals)
            sample_sizes[m] = len(vals)
        for m in inference_metrics:
            for rid, run in runs.items():
                if run.status == RunStatus.COMPLETED and rid in outcomes and m not in outcomes[rid]:
                    excluded.append({"run_id": rid, "metric": m, "reason": f"no usable measurement for '{m}'"})

        effects: list[EffectEstimate] = []
        main_effects: list[EffectEstimate] = []
        anova: list[AnovaTable] = []
        regression: list[RegressionModel] = []
        interactions: list[InteractionEffect] = []
        factor_effects_by_metric: dict[str, dict[str, dict[str, float]]] = {}
        outlier_flags: list[dict[str, Any]] = []

        for m in obj_metrics:
            obs = [(runs[rid], o[m]) for rid, o in outcomes.items() if m in o]
            effects.extend(self._configuration_comparisons(spec, result, m, obs, alpha, bootstrap, seed))
            main_effects.extend(self._factor_level_comparisons(spec, result, m, obs, alpha, bootstrap, seed))
            factor_effects_by_metric[m] = self._marginal_means(spec, obs)
            anova.extend(self._anova(spec, result, m, obs))
            reg = self._regression(spec, result, m, obs, alpha)
            if reg:
                regression.append(reg)
                for f in reg.diagnostics.get("outlier_runs", []):
                    outlier_flags.append({"metric": m, **f})
            interactions.extend(self._interactions(spec, m, obs, anova))
        for m in out_metrics:
            if m not in obj_metrics and m not in factor_effects_by_metric:
                obs = [(runs[rid], o[m]) for rid, o in outcomes.items() if m in o]
                effects.extend(self._configuration_comparisons(spec, result, m, obs, alpha, bootstrap, seed))
                factor_effects_by_metric[m] = self._marginal_means(spec, obs)

        # Holm adjustment within each (metric, comparison_type) family
        fam: dict[tuple, list[EffectEstimate]] = defaultdict(list)
        for e in effects + main_effects:
            fam[(e.metric, e.comparison_type, e.factor if e.comparison_type == "factor_level" else None)].append(e)
        for group in fam.values():
            adj = st.holm_adjust([e.p_value for e in group])
            for e, p in zip(group, adj):
                e.p_value_holm, e.n_comparisons_in_family = p, sum(1 for x in group if x.p_value is not None)

        primary = obj_metrics[0] if obj_metrics else None
        factor_effects: dict[str, dict[str, float]] = factor_effects_by_metric.get(primary, {}) if primary else {}

        mo = self._multi_objective(spec, result, outcomes)

        used = sorted(
            set().union(*[{rid for rid, o in outcomes.items() if m in o} for m in obj_metrics]) if obj_metrics else set()
        )
        if not outcomes:
            warnings.append("no completed runs with usable measurements: nothing to analyse")
        if any(n < 3 for m, n in sample_sizes.items() if m in obj_metrics):
            warnings.append("fewer than 3 observations for an objective metric: inference is unreliable or withheld")
        if len(spec.measurement_definitions) and not obj_metrics:
            warnings.append("objectives are defined by custom functions; metric-level inference not run")
        flags_seen = sum(len(r.measurements) for r in result.runs if r.status == RunStatus.COMPLETED)
        limitations = [
            "Summaries use the aggregated run-level outcome of finite numeric measurements without invalid quality flags.",
            "Treatments are compared individually (configuration vs control and factor level vs reference); "
            "they are never pooled into a single 'treatment' group.",
            "Estimates are associational unless the design randomised run order and the adapter controls "
            "uncontrolled variables; causal claims are not made.",
            "p-values are unadjusted unless 'p_value_holm' is shown; many comparisons inflate false positives.",
            "Marginal factor-level comparisons assume balance; use the ANOVA/regression models for adjusted effects.",
        ]
        if result.plan.design_info.get("p", 0):
            limitations.append(
                "Fractional design: main effects are aliased with higher-order interactions "
                f"(resolution {result.plan.design_info.get('resolution')})."
            )
        if flags_seen == 0 and outcomes:
            warnings.append("runs have no raw measurements stored; outcomes come from processed records only")
        return AnalysisResult(
            experiment_id=spec.experiment_id,
            descriptive=descriptive,
            effects=effects,
            factor_effects=factor_effects,
            pareto_run_ids=mo["pareto"],
            method="run-level descriptive statistics; Welch/paired t comparisons; Type II ANOVA; OLS regression; Pareto analysis",
            assumptions=[
                "Run-level outcomes are independent given the design (check randomisation).",
                "t-based intervals assume approximately normal outcomes; checks are reported as warnings.",
                f"Two-sided tests at alpha={alpha}.",
            ],
            limitations=limitations,
            algorithm="trialforge.analysis",
            algorithm_version=ALGORITHM_VERSION,
            input_run_ids=used,
            excluded_runs=excluded,
            sample_sizes=sample_sizes,
            warnings=warnings,
            interactions=interactions,
            anova=anova,
            regression=regression,
            feasible_run_ids=mo["feasible"],
            infeasible_runs=mo["infeasible"],
            tradeoffs=mo["tradeoffs"],
            best_run_id=mo["best"],
            outlier_flags=outlier_flags,
            factor_effects_by_metric=factor_effects_by_metric,
            main_effects=main_effects,
            parameters={"alpha": alpha, "bootstrap": bootstrap, "seed": seed, "exclude_outliers": self.exclude_outliers},
        )

    # -------------------------------------------------- comparisons helpers
    def _compare(
        self,
        metric: str,
        ctl: list[tuple[Run, float]],
        trt: list[tuple[Run, float]],
        *,
        alpha: float,
        bootstrap: bool,
        seed: int,
        base: dict[str, Any],
    ) -> EffectEstimate:
        a = [v for _, v in ctl]
        b = [v for _, v in trt]
        notes: list[str] = []
        paired_pairs = self._pair_by_block(ctl, trt)
        kind = base.get("comparison_type", "treatment_vs_control")
        if paired_pairs is not None:
            kind = "paired"
        e = EffectEstimate(
            metric=metric,
            sample_size_control=len(a),
            sample_size_treatment=len(b),
            control_run_ids=[r.run_id for r, _ in ctl],
            treatment_run_ids=[r.run_id for r, _ in trt],
            **{**base, "comparison_type": kind},
        )
        if a:
            e.control_mean = float(np.mean(a))
        if b:
            e.treatment_mean = float(np.mean(b))
        if not a or not b:
            e.warnings.append("control or treatment observations unavailable; effect not estimated")
            e.limitations = list(e.warnings)
            return e
        e.mean_difference = e.treatment_mean - e.control_mean
        if e.control_mean != 0:
            e.relative_change_percent = e.mean_difference / abs(e.control_mean) * 100
        else:
            e.warnings.append("control mean is zero: relative change undefined")
        if paired_pairs is not None:
            pa, pb = paired_pairs
            res = st.paired(pa, pb, alpha)
            e.assumptions = ["Paired differences across blocks are independent and approximately normal."]
            e.ci_method = "paired t"
            if res:
                e.effect_size, e.effect_size_type = res["dz"], "Cohen's dz"
            e.mean_difference = float(np.mean(np.asarray(pb) - np.asarray(pa)))
            n_eff = len(pa)
            if bootstrap:
                e.bootstrap_ci_95 = st.bootstrap_ci_diff(
                    pa, pb, seed=seed, n_boot=self.n_bootstrap, alpha=alpha, paired_data=True
                )
        else:
            res = st.welch(a, b, alpha)
            e.assumptions = ["Independent observations; Welch t (unequal variances) with Satterthwaite df."]
            e.ci_method = "Welch t"
            e.effect_size, e.effect_size_type = st.hedges_g(a, b), "Hedges' g"
            n_eff = min(len(a), len(b))
            if bootstrap:
                e.bootstrap_ci_95 = st.bootstrap_ci_diff(a, b, seed=seed, n_boot=self.n_bootstrap, alpha=alpha)
                if e.bootstrap_ci_95 is None:
                    notes.append("bootstrap CI requires >=3 observations per group; not reported")
        if res is None:
            e.evidence = "insufficient"
            e.ci_method = None
            notes.append(
                "insufficient replication (need >=2 independent observations per group): "
                "standard error, confidence interval and p-value are withheld"
            )
        elif res.get("degenerate") and res.get("ci") is None:
            e.standard_error, e.degrees_of_freedom = res["se"], res["df"]
            e.p_value = res["p"]
            e.evidence = "insufficient"
            notes.append("zero variance in both groups: interval is undefined")
        else:
            e.standard_error, e.degrees_of_freedom, e.t_statistic = res["se"], res["df"], res["t"]
            e.confidence_interval_95, e.p_value, e.evidence = res["ci"], res["p"], "sufficient"
            if abs(1 - alpha - 0.95) > 1e-9:
                notes.append(f"interval computed at level {1 - alpha:.3f}, field name retained for compatibility")
        if n_eff < 3:
            notes.append("very small sample (<3 per group): assumption checks have no power and intervals are wide")
        elif n_eff < 5:
            notes.append("small sample (<5 per group): treat interval and p-value cautiously")
        for label, vals in (("control", a), ("treatment", b)):
            p = st.shapiro_p(vals)
            if p is not None and p < alpha:
                notes.append(f"{label} group fails Shapiro-Wilk normality check (p={p:.3g}); consider bootstrap")
        e.warnings.extend(notes)
        e.limitations = list(e.warnings)
        return e

    @staticmethod
    def _pair_by_block(ctl, trt) -> tuple[list[float], list[float]] | None:
        def per_block(items):
            d: dict[tuple, list[float]] = defaultdict(list)
            for r, v in items:
                if not r.block:
                    return None
                d[_key(r.block)].append(v)
            return d

        pc, pt = per_block(ctl), per_block(trt)
        if pc is None or pt is None:
            return None
        common = sorted(set(pc) & set(pt))
        if len(common) < 2 or set(pt) - set(pc):
            return None
        return [float(np.mean(pc[k])) for k in common], [float(np.mean(pt[k])) for k in common]

    def _configuration_comparisons(self, spec, result, metric, obs, alpha, bootstrap, seed) -> list[EffectEstimate]:
        control = result.plan.control_configuration
        ckey = _key(control)
        groups: dict[tuple, list[tuple[Run, float]]] = defaultdict(list)
        for r, v in obs:
            groups[_key(r.configuration)].append((r, v))
        ctl = groups.get(ckey, [])
        out: list[EffectEstimate] = []
        if not ctl:
            return [
                EffectEstimate(
                    metric=metric,
                    control_configuration=control,
                    warnings=["no completed control runs for this metric; configuration-vs-control effects not estimated"],
                    limitations=["Control or treatment observations unavailable; effect not estimated."],
                )
            ]
        cfgs = {_key(r.configuration): r.configuration for r, _ in obs}
        for k, trt in groups.items():
            if k == ckey:
                continue
            if len(out) >= self.max_cmp:
                break
            cfg = cfgs[k]
            diff = [n for n in control if control[n] != cfg.get(n)]
            base: dict[str, Any] = {"treatment_configuration": cfg, "control_configuration": control, "differing_factors": diff}
            if len(diff) == 1:
                base.update(
                    comparison_type="treatment_vs_control", factor=diff[0], level=cfg[diff[0]], control_level=control[diff[0]]
                )
            else:
                base["comparison_type"] = "configuration"
            out.append(self._compare(metric, ctl, trt, alpha=alpha, bootstrap=bootstrap, seed=seed, base=base))
        out.sort(key=lambda e: (len(e.differing_factors), str(e.differing_factors), str(e.level)))
        return out

    def _factor_level_comparisons(self, spec, result, metric, obs, alpha, bootstrap, seed) -> list[EffectEstimate]:
        out: list[EffectEstimate] = []
        control = result.plan.control_configuration
        for f in spec.factors:
            by_level: dict[str, list[tuple[Run, float]]] = defaultdict(list)
            for r, v in obs:
                by_level[repr(r.configuration[f.name])].append((r, v))
            if len(by_level) < 2:
                continue
            ref_key = repr(control.get(f.name)) if repr(control.get(f.name)) in by_level else next(iter(by_level))
            ref = by_level[ref_key]
            balanced = self._balanced(spec, f, by_level)
            for lk, items in by_level.items():
                if lk == ref_key:
                    continue
                lvl = items[0][0].configuration[f.name]
                base = {
                    "comparison_type": "factor_level",
                    "factor": f.name,
                    "level": lvl,
                    "control_level": ref[0][0].configuration[f.name],
                    "differing_factors": [f.name],
                    "standardized_note": "marginal comparison pooling runs across other factors' levels",
                }
                e = self._compare(metric, ref, items, alpha=alpha, bootstrap=bootstrap, seed=seed, base=base)
                e.comparison_type = "factor_level"
                if not balanced:
                    e.warnings.append(
                        "design is unbalanced with respect to other factors: this marginal "
                        "comparison may be confounded; prefer the regression/ANOVA estimate"
                    )
                if len(spec.treatment_factors) > 1:
                    e.warnings.append(
                        "marginal comparison ignores variation from other factors, which inflates "
                        "the error term; ANOVA/regression give adjusted inference"
                    )
                e.limitations = list(e.warnings)
                out.append(e)
        return out

    @staticmethod
    def _balanced(spec: ExperimentSpec, factor: Factor, by_level) -> bool:
        for other in spec.factors:
            if other.name == factor.name:
                continue
            counts = []
            for items in by_level.values():
                c: dict[str, int] = defaultdict(int)
                for r, _ in items:
                    c[repr(r.configuration[other.name])] += 1
                counts.append(tuple(sorted((k, v) for k, v in c.items())))
            if len(set(counts)) > 1:
                return False
        return True

    @staticmethod
    def _marginal_means(spec, obs) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for f in spec.factors:
            g: dict[str, list[float]] = defaultdict(list)
            for r, v in obs:
                g[repr(r.configuration[f.name])].append(v)
            out[f.name] = {k: float(np.mean(v)) for k, v in g.items()}
        return out

    # ------------------------------------------------- model construction
    @staticmethod
    def _block_values(run: Run, spec: ExperimentSpec) -> dict[str, Any]:
        b = dict(run.block)
        for f in spec.block_factors:
            b[f.name] = run.configuration[f.name]
        return b

    def _term_blocks(self, spec, obs, *, categorical: bool, interactions: bool, quadratic: bool):
        """Return (names_by_term, blocks_by_term, notes). A 'term' is a tuple of factor names."""
        terms: dict[tuple, np.ndarray] = {}
        colnames: dict[tuple, list[str]] = {}
        notes: list[str] = []
        main: list[tuple[str, np.ndarray, list[str]]] = []
        for f in spec.treatment_factors:
            vals = [r.configuration[f.name] for r, _ in obs]
            levels = sorted(set(vals), key=lambda x: (str(type(x)), x))
            if len(levels) < 2:
                continue
            if categorical or not f.is_numeric:
                cols = [np.array([1.0 if v == lv else 0.0 for v in vals]) for lv in levels[1:]]
                names = [f"{f.name}[{lv}]" for lv in levels[1:]]
                if not f.is_numeric or categorical:
                    pass
            else:
                lo, hi = f.low_high()
                if hi == lo:
                    continue
                cols = [np.array([f.encode(v) for v in vals])]
                names = [f.name]
            main.append((f.name, np.column_stack(cols), names))
        block_names: dict[str, list[Any]] = {}
        for r, _ in obs:
            for k, v in self._block_values(r, spec).items():
                block_names.setdefault(k, [])
        for bname in block_names:
            vals = [self._block_values(r, spec).get(bname) for r, _ in obs]
            levels = sorted(set(vals), key=lambda x: (str(type(x)), str(x)))
            if len(levels) < 2:
                continue
            cols = [np.array([1.0 if v == lv else 0.0 for v in vals]) for lv in levels[1:]]
            main.append((bname, np.column_stack(cols), [f"block:{bname}[{lv}]" for lv in levels[1:]]))
        for fname, blk, names in main:
            terms[(fname,)] = blk
            colnames[(fname,)] = names
        treat = {f.name for f in spec.treatment_factors}
        if interactions:
            for (a, ba, na), (b, bb, nb) in itertools.combinations([m for m in main if m[0] in treat], 2):
                cols, names = [], []
                for i in range(ba.shape[1]):
                    for j in range(bb.shape[1]):
                        cols.append(ba[:, i] * bb[:, j])
                        names.append(f"{na[i]}:{nb[j]}")
                terms[(a, b)] = np.column_stack(cols)
                colnames[(a, b)] = names
        if quadratic and not categorical:
            for f in spec.treatment_factors:
                if f.factor_type == "continuous" and (f.name,) in terms:
                    x = terms[(f.name,)][:, 0]
                    if len(set(np.round(x, 9))) >= 3:
                        terms[(f.name, f.name)] = (x**2).reshape(-1, 1)
                        colnames[(f.name, f.name)] = [f"{f.name}^2"]
        return terms, colnames, notes

    def _fit_plan(self, spec, obs, *, categorical, quadratic):
        """Choose the richest term set (with 2-way interactions) that leaves residual df >= 1."""
        n = len(obs)
        y = np.array([v for _, v in obs], float)
        for interactions in (True, False):
            terms, colnames, notes = self._term_blocks(
                spec, obs, categorical=categorical, interactions=interactions, quadratic=quadratic
            )
            if not terms:
                return None
            X = np.column_stack([np.ones(n)] + [terms[t] for t in terms])
            rank = np.linalg.matrix_rank(X, tol=1e-9 * max(1.0, np.abs(X).max()))
            if n - rank >= 1:
                return terms, colnames, X, y, interactions
        return terms, colnames, X, y, False

    # --------------------------------------------------------------- ANOVA
    def _anova(self, spec, result, metric, obs) -> list[AnovaTable]:
        out: list[AnovaTable] = []
        n = len(obs)
        run_ids = [r.run_id for r, _ in obs]
        y = np.array([v for _, v in obs], float)
        # one-way ANOVA per treatment factor (groups = observed levels)
        for f in spec.treatment_factors:
            groups: dict[str, list[float]] = defaultdict(list)
            for r, v in obs:
                groups[repr(r.configuration[f.name])].append(v)
            k = len(groups)
            if k < 2 or n <= k:
                continue
            grand = y.mean()
            ssb = sum(len(g) * (np.mean(g) - grand) ** 2 for g in groups.values())
            ssw = sum(((np.asarray(g) - np.mean(g)) ** 2).sum() for g in groups.values())
            dfb, dfw = k - 1, n - k
            warn = (
                ["one-way ANOVA ignores other factors; their variation inflates the residual term"]
                if len(spec.treatment_factors) > 1
                else []
            )
            row_f = None
            if ssw > 0:
                fval = (ssb / dfb) / (ssw / dfw)
                row_f = (float(fval), float(sps.f.sf(fval, dfb, dfw)))
            else:
                warn.append("zero within-group variance: F undefined")
            out.append(
                AnovaTable(
                    metric=metric,
                    kind="one_way",
                    factor=f.name,
                    run_ids=run_ids,
                    n_observations=n,
                    residual_df=float(dfw),
                    sum_squares_type="I (single factor)",
                    warnings=warn,
                    rows=[
                        AnovaRow(
                            term=f.name,
                            sum_squares=float(ssb),
                            df=float(dfb),
                            mean_square=float(ssb / dfb),
                            f_statistic=row_f[0] if row_f else None,
                            p_value=row_f[1] if row_f else None,
                        ),
                        AnovaRow(term="Residual", sum_squares=float(ssw), df=float(dfw), mean_square=float(ssw / dfw)),
                    ],
                )
            )
        if len(spec.treatment_factors) < 2:
            return out
        plan = self._fit_plan(spec, obs, categorical=True, quadratic=False)
        if plan is None:
            return out
        terms, colnames, X, y, with_int = plan
        names = list(terms)
        full_rss, full_rank = st.rss_rank(X, y)
        resid_df = n - full_rank
        warn: list[str] = []
        if resid_df < 1:
            out.append(
                AnovaTable(
                    metric=metric,
                    kind="factorial",
                    run_ids=run_ids,
                    n_observations=n,
                    rows=[],
                    residual_df=float(resid_df),
                    warnings=[
                        "model is saturated (no residual degrees of freedom): replicate runs or centre points are needed for "
                        "F tests; ANOVA table withheld"
                    ],
                )
            )
            return out
        if not with_int and len(spec.treatment_factors) > 1:
            warn.append(
                "interaction terms omitted: not enough runs to estimate them with residual degrees of freedom; "
                "the F tests below are valid only if interactions are absent (replicate runs are needed to test them)"
            )
        if result.plan.design_info.get("p", 0):
            warn.append(
                "fractional design: some terms are aliased; dependent columns are dropped in the order main effects first"
            )
        mse = full_rss / resid_df
        rows: list[AnovaRow] = []
        for t in names:
            others = [u for u in names if not set(t) < set(u) and u != t]
            Xo = np.column_stack([np.ones(n)] + [terms[u] for u in others]) if others else np.ones((n, 1))
            Xw = np.column_stack([Xo, terms[t]])
            rss0, r0 = st.rss_rank(Xo, y)
            rss1, r1 = st.rss_rank(Xw, y)
            df = r1 - r0
            ss = max(rss0 - rss1, 0.0)
            label = ":".join(t)
            if df <= 0:
                warn.append(f"term '{label}' is aliased with other terms and has no degrees of freedom")
                continue
            fval = (ss / df) / mse if mse > 0 else None
            rows.append(
                AnovaRow(
                    term=label,
                    sum_squares=float(ss),
                    df=float(df),
                    mean_square=float(ss / df),
                    f_statistic=float(fval) if fval is not None else None,
                    p_value=float(sps.f.sf(fval, df, resid_df)) if fval is not None else None,
                )
            )
        rows.append(AnovaRow(term="Residual", sum_squares=float(full_rss), df=float(resid_df), mean_square=float(mse)))
        out.append(
            AnovaTable(
                metric=metric,
                kind="factorial",
                run_ids=run_ids,
                n_observations=n,
                rows=rows,
                residual_df=float(resid_df),
                warnings=warn,
            )
        )
        return out

    # ---------------------------------------------------------- regression
    def _regression(self, spec, result, metric, obs, alpha) -> RegressionModel | None:
        n = len(obs)
        if n < 3:
            return None
        rsm = result.plan.method in ("central_composite", "box_behnken")
        plan = self._fit_plan(spec, obs, categorical=False, quadratic=rsm)
        if plan is None:
            return None
        terms, colnames, X, y, with_int = plan
        names = ["Intercept"] + [c for t in terms for c in colnames[t]]
        keep = st.independent_columns(X)
        warnings: list[str] = []
        dropped = [names[j] for j in range(len(names)) if j not in keep]
        if dropped:
            warnings.append("aliased/dependent terms dropped (earlier terms win): " + ", ".join(dropped))
            X = X[:, keep]
            names = [names[j] for j in keep]
        fit = st.ols(X, y, alpha)
        df = fit["df"]
        if df < 1:
            warnings.append("no residual degrees of freedom: standard errors, p-values and intervals withheld")
        if not with_int and len(spec.treatment_factors) > 1:
            warnings.append("interaction terms omitted: insufficient runs")
        coefs = []
        for j, nm in enumerate(names):
            if df >= 1:
                coefs.append(
                    Coefficient(
                        term=nm,
                        estimate=float(fit["beta"][j]),
                        std_error=float(fit["se"][j]),
                        t_statistic=None if np.isnan(fit["t"][j]) else float(fit["t"][j]),
                        p_value=None if np.isnan(fit["pvalue"][j]) else float(fit["pvalue"][j]),
                        ci_95=(float(fit["ci"][j][0]), float(fit["ci"][j][1])),
                    )
                )
            else:
                coefs.append(Coefficient(term=nm, estimate=float(fit["beta"][j])))
        diag: dict[str, Any] = {"n": n, "p": X.shape[1], "residual_df": df}
        resid = fit["resid"]
        diag["condition_number"] = float(np.linalg.cond(X)) if X.shape[1] else None
        if df >= 1:
            diag["shapiro_residual_p"] = st.shapiro_p(resid)
            diag["durbin_watson"] = st.durbin_watson(resid[np.argsort([r.order for r, _ in obs])])
            diag["breusch_pagan_p"] = st.breusch_pagan_p(X, resid)
            cooks = fit.get("cooks")
            if cooks is not None and len(cooks):
                diag["max_cooks_distance"] = float(np.max(cooks))
                diag["influential_runs"] = [
                    obs[i][0].run_id for i in range(n) if cooks[i] > 4 / max(n - X.shape[1], 1) and cooks[i] > 1.0
                ]
            diag["high_leverage_runs"] = [obs[i][0].run_id for i in range(n) if fit["leverage"][i] > 2 * X.shape[1] / n]
            sr = fit.get("student_resid")
            diag["outlier_runs"] = [
                {"run_id": obs[i][0].run_id, "studentized_residual": float(sr[i])}
                for i in range(n)
                if sr is not None and abs(sr[i]) > 3
            ]
            sp = diag["shapiro_residual_p"]
            if sp is not None and sp < alpha:
                warnings.append(f"residuals fail the Shapiro-Wilk normality check (p={sp:.3g})")
            bp = diag["breusch_pagan_p"]
            if bp is not None and bp < alpha:
                warnings.append(f"evidence of non-constant residual variance (Breusch-Pagan p={bp:.3g})")
            diag["residuals"] = [
                {"run_id": obs[i][0].run_id, "fitted": float(fit["fitted"][i]), "residual": float(resid[i])} for i in range(n)
            ]
        if diag["condition_number"] and diag["condition_number"] > 30:
            warnings.append(
                f"high design-matrix condition number ({diag['condition_number']:.1f}): coefficients may be strongly correlated"
            )
        coding = {
            "numeric factors": "coded to [-1, +1] (low -> -1, high -> +1)",
            "categorical factors": "dummy coding vs the first level",
            "blocks": "dummy coding vs the first block",
        }
        if rsm:
            coding["quadratic"] = "squared coded terms for continuous factors (response-surface design)"
        return RegressionModel(
            metric=metric,
            terms=names,
            coefficients=coefs,
            n_observations=n,
            n_parameters=X.shape[1],
            residual_df=float(df),
            r_squared=fit["r2"],
            adjusted_r_squared=fit.get("adj_r2"),
            residual_std_error=fit.get("sigma"),
            f_statistic=fit.get("f"),
            f_p_value=fit.get("f_p"),
            diagnostics=diag,
            coding=coding,
            run_ids=[r.run_id for r, _ in obs],
            warnings=warnings,
        )

    # --------------------------------------------------------- interactions
    def _interactions(self, spec, metric, obs, anova_tables) -> list[InteractionEffect]:
        out: list[InteractionEffect] = []
        table = next((t for t in anova_tables if t.metric == metric and t.kind == "factorial"), None)
        rows = {r.term: r for r in table.rows} if table else {}
        for fa, fb in itertools.combinations(spec.treatment_factors, 2):

            def levels(f):
                seen = []
                for r, _ in obs:
                    v = r.configuration[f.name]
                    if v not in seen:
                        seen.append(v)
                return sorted(seen) if f.is_numeric else seen

            la, lb = levels(fa), levels(fb)
            ie = InteractionEffect(metric=metric, factors=(fa.name, fb.name))
            if len(la) < 2 or len(lb) < 2:
                ie.warnings.append("a factor has fewer than 2 observed levels")
                out.append(ie)
                continue
            lo_a, hi_a, lo_b, hi_b = la[0], la[-1], lb[0], lb[-1]
            cell: dict[tuple, list[tuple[str, float]]] = defaultdict(list)
            for r, v in obs:
                cell[(r.configuration[fa.name], r.configuration[fb.name])].append((r.run_id, v))
            need = [(hi_a, hi_b), (lo_a, hi_b), (hi_a, lo_b), (lo_a, lo_b)]
            if any(c not in cell for c in need):
                ie.warnings.append("not all four extreme-level cells were run: interaction contrast not estimable")
            else:
                m = {c: float(np.mean([v for _, v in cell[c]])) for c in need}
                ie.estimate = (m[(hi_a, hi_b)] - m[(lo_a, hi_b)]) - (m[(hi_a, lo_b)] - m[(lo_a, lo_b)])
                ie.run_ids = sorted({rid for c in need for rid, _ in cell[c]})
                ie.description = (
                    f"change in the effect of {fa.name} ({lo_a}->{hi_a}) when {fb.name} goes "
                    f"{lo_b}->{hi_b} (difference of differences of cell means)"
                )
                if len(spec.treatment_factors) > 2:
                    ie.warnings.append("cell means average over the remaining factors")
            row = rows.get(f"{fa.name}:{fb.name}")
            if row and row.p_value is not None:
                ie.f_statistic, ie.p_value = row.f_statistic, row.p_value
                ie.degrees_of_freedom = (row.df, table.residual_df)
            elif row is None:
                ie.warnings.append("interaction not tested: no residual degrees of freedom or aliased term")
            out.append(ie)
        return out

    # --------------------------------------------------------- multi-objective
    def _multi_objective(self, spec, result, outcomes) -> dict[str, Any]:
        vectors: dict[str, list[float]] = {}
        feasible: list[str] = []
        infeasible: list[dict[str, Any]] = []
        values: dict[str, dict[str, float]] = {}
        runs = {r.run_id: r for r in result.runs}
        for rid, o in outcomes.items():
            vec = oriented_vector(spec, o, self.plugins)
            if vec is None:
                continue
            run = runs[rid]
            ev = evaluate_constraints(spec, run.configuration, o, plugins=self.plugins)
            values[rid] = {ob.metric: objective_value(ob, o, self.plugins) for ob in spec.objectives}
            if is_feasible(ev):
                feasible.append(rid)
                vectors[rid] = vec
            else:
                infeasible.append(
                    {
                        "run_id": rid,
                        "configuration": run.configuration,
                        "objectives": values[rid],
                        "violations": [e for e in ev if e["status"] != "satisfied"],
                        "_vec": vec,
                    }
                )
        front = pareto_front(vectors) if vectors else []
        best = None
        if vectors:
            scores = weighted_normalized(spec, vectors)
            best = min(scores, key=lambda r: (scores[r], runs[r].order))
        best_vec = vectors.get(best) if best else None
        for item in infeasible:
            vec = item.pop("_vec")
            item["better_than_best_feasible_on_all_objectives"] = bool(
                best_vec and all(a <= b for a, b in zip(vec, best_vec)) and any(a < b for a, b in zip(vec, best_vec))
            )
        tradeoffs = [
            {
                "run_id": rid,
                "configuration": runs[rid].configuration,
                "objectives": values[rid],
                "feasible": True,
                "pareto": rid in front,
            }
            for rid in feasible
        ]
        return {"pareto": front, "feasible": feasible, "infeasible": infeasible, "best": best, "tradeoffs": tradeoffs}
