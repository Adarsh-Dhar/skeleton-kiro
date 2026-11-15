"""Constraint-aware optimisation and sequential candidate selection.

Strategies
----------
``grid`` / ``random``   space-filling search over unseen feasible candidates (no model).
``bayesian``            GP surrogate + analytic Expected Improvement; output constraints through
                        probability-of-feasibility; multi-objective through ParEGO scalarisation.
                        ``sequential`` is an alias for ``bayesian`` (0.2 behaviour change).
``heuristic``           the old inverse-distance score, **explicitly an uncalibrated heuristic**.
``optuna``              Optuna samplers (GP for single objective, TPE for multi-objective); optional.
registered plugins      ``PluginRegistry.register('optimizer', name, fn)``;
                        ``fn(OptimizerContext) -> list[Recommendation]``.

Labels: ``Recommendation.prediction_kind`` is ``model_prediction`` only for the GP strategy;
``expected_improvement`` is populated only there.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from trialforge.definitions.registry import DEFAULT_PLUGINS, PluginRegistry
from trialforge.errors import OptimizationError
from trialforge.evaluation import (
    evaluate_constraints,
    is_feasible,
    objective_value,
    oriented,
    oriented_vector,
    run_outcomes,
    scalar_score,
)
from trialforge.models.core import (
    ExperimentResult,
    ExperimentSpec,
    Recommendation,
    RunStatus,
    StopDecision,
)
from trialforge.optimization.surrogate import (
    GaussianProcess,
    expected_improvement,
    prob_in_interval,
)

__all__ = ["Optimizer", "OptimizerContext", "StoppingCriteria"]


@dataclass
class StoppingCriteria:
    """Stopping rules evaluated by :meth:`Optimizer.should_stop`.

    ``target_value`` - stop when the best *feasible* single objective reaches it
    (min: <=, max: >=, target: within ``target_tolerance``).
    ``improvement_threshold`` - stop when the best improved by at least this much over the
    first feasible observation (absolute, objective units).
    ``min_improvement``/``patience`` - stop when the last ``patience`` completed runs improved the
    best feasible value by less than ``min_improvement``.
    """

    target_value: float | None = None
    target_tolerance: float = 0.0
    improvement_threshold: float | None = None
    min_improvement: float | None = None
    patience: int = 5
    max_iterations: int | None = None
    cancelled: Callable[[], bool] | None = None


@dataclass
class OptimizerContext:
    """Everything an optimizer plugin needs."""

    spec: ExperimentSpec
    result: ExperimentResult
    count: int
    observations: list[tuple[dict[str, Any], dict[str, float]]]
    remaining_budget: int
    seed: int
    feasible_candidates: Callable[[int], list[dict[str, Any]]]
    extra: dict[str, Any] = field(default_factory=dict)


class _Encoder:
    """Map configurations to [0,1]^d (numeric scaled, categorical one-hot)."""

    def __init__(self, spec: ExperimentSpec) -> None:
        self.factors = spec.factors
        self.dim = sum(1 if f.is_numeric else len(f.values) for f in self.factors)

    def encode(self, cfg: dict[str, Any]) -> np.ndarray:
        out: list[float] = []
        for f in self.factors:
            v = cfg[f.name]
            if f.is_numeric:
                lo, hi = f.low_high()
                out.append((float(v) - lo) / (hi - lo) if hi > lo else 0.0)
            else:
                out.extend(1.0 if v == lv else 0.0 for lv in f.values)
        return np.array(out)


class Optimizer:
    """Generate the next configurations to run."""

    METHODS = {"grid", "random", "sequential", "bayesian", "heuristic", "optuna"}

    def __init__(self, *, plugins: PluginRegistry | None = None, n_candidates: int = 2000) -> None:
        self.plugins = plugins or DEFAULT_PLUGINS
        self.n_candidates = n_candidates

    # ------------------------------------------------------------ public
    def suggest(
        self, spec: ExperimentSpec, result: ExperimentResult, *, count: int = 1, method: str = "sequential"
    ) -> list[Recommendation]:
        """Recommend up to ``count`` unseen, input-feasible configurations using ``method`` (grid, random,
        bayesian/sequential, heuristic, optuna or a registered plugin). Returns [] when the budget is exhausted.
        """
        plugin = self.plugins.has("optimizer", method)
        if method not in self.METHODS and not plugin:
            raise OptimizationError(
                f"unsupported optimizer '{method}'; use {sorted(self.METHODS | set(self.plugins.names('optimizer')))}"
            )
        if count < 1:
            raise OptimizationError("count must be positive")
        if result.spec.experiment_id != spec.experiment_id:
            raise OptimizationError("result does not belong to the supplied experiment")
        remaining = spec.run_budget - len(result.runs)
        if remaining <= 0:
            return []
        count = min(count, remaining)
        seed = spec.random_seed + len(result.runs)
        seen = {self._key(r.configuration) for r in result.runs}
        obs = self._observations(spec, result)
        rejected: list[dict[str, Any]] = []

        def candidates(n: int) -> list[dict[str, Any]]:
            return self._feasible_candidates(spec, seen, n, seed, rejected, obs)

        ctx = OptimizerContext(
            spec=spec,
            result=result,
            count=count,
            observations=obs,
            remaining_budget=remaining,
            seed=seed,
            feasible_candidates=candidates,
        )
        if plugin:
            recs = self.plugins.get("optimizer", method)(ctx)
            for r in recs:
                r.remaining_budget = remaining - len(recs)
                r.strategy = r.strategy or f"plugin:{method}"
                r.experiment_id = spec.experiment_id
            return recs[:count]
        if method == "grid":
            recs = self._grid(spec, seen, count, rejected)
        elif method == "random":
            recs = [
                self._rec(spec, c, "random", "Unseen feasible candidate drawn by seeded random search.", rejected)
                for c in candidates(count)[:count]
            ]
        elif method == "heuristic":
            recs = self._heuristic(spec, obs, candidates(self.n_candidates), count, rejected)
        elif method == "optuna":
            recs = self._optuna(spec, result, obs, count, seed, seen, rejected)
        else:
            recs = self._bayesian(spec, obs, candidates(self.n_candidates), count, seed, rejected)
        for r in recs:
            r.remaining_budget = remaining - len(recs)
            r.experiment_id = spec.experiment_id
        return recs

    def should_stop(
        self,
        spec: ExperimentSpec,
        result: ExperimentResult,
        criteria: StoppingCriteria,
        *,
        iteration: int = 0,
        candidates_available: bool = True,
    ) -> StopDecision:
        """Evaluate stopping rules in priority order: cancel, target, budget, threshold, iterations, stagnation."""
        if criteria.cancelled is not None and criteria.cancelled():
            return StopDecision(stop=True, reason="user_cancelled", detail="cancellation requested")
        trace = self._best_trace(spec, result)
        single = len(spec.objectives) == 1
        if single and trace and criteria.target_value is not None:
            obj = spec.objectives[0]
            best = trace[-1][1]
            hit = (
                best <= criteria.target_value
                if obj.direction == "minimize"
                else best >= criteria.target_value
                if obj.direction == "maximize"
                else abs(best - obj.target) <= criteria.target_tolerance
            )
            if hit:
                return StopDecision(
                    stop=True,
                    reason="target_achieved",
                    detail=f"best feasible value {best:.6g} reached target {criteria.target_value}",
                )
        if len(result.runs) >= spec.run_budget:
            return StopDecision(stop=True, reason="budget_exhausted", detail=f"{len(result.runs)}/{spec.run_budget} runs used")
        if single and trace and criteria.improvement_threshold is not None:
            gain = abs(trace[-1][1] - trace[0][1])
            if gain >= criteria.improvement_threshold:
                return StopDecision(
                    stop=True,
                    reason="improvement_threshold_met",
                    detail=f"improved by {gain:.6g} >= {criteria.improvement_threshold}",
                )
        if criteria.max_iterations is not None and iteration >= criteria.max_iterations:
            return StopDecision(stop=True, reason="max_iterations", detail=f"{iteration} iterations")
        if single and criteria.min_improvement is not None and len(trace) > criteria.patience:
            recent = abs(trace[-1][1] - trace[-1 - criteria.patience][1])
            if recent < criteria.min_improvement:
                return StopDecision(
                    stop=True,
                    reason="insufficient_improvement",
                    detail=f"improved {recent:.6g} over the last {criteria.patience} runs",
                )
        if not candidates_available:
            return StopDecision(stop=True, reason="no_feasible_candidate", detail="no unseen feasible candidate remains")
        return StopDecision(stop=False, reason="continue")

    # ----------------------------------------------------------- internals
    @staticmethod
    def _key(cfg: dict[str, Any]) -> tuple:
        return tuple(sorted((k, repr(v)) for k, v in cfg.items()))

    def _observations(self, spec, result) -> list[tuple[dict[str, Any], dict[str, float]]]:
        out = []
        for run in result.runs:
            if run.status != RunStatus.COMPLETED:
                continue
            o = run_outcomes(run)
            if o:
                out.append((run.configuration, o))
        return out

    def _best_trace(self, spec, result) -> list[tuple[int, float]]:
        """(run order, best feasible raw objective so far) for single-objective experiments."""
        if len(spec.objectives) != 1:
            return []
        obj = spec.objectives[0]
        best: float | None = None
        trace = []
        for run in sorted((r for r in result.runs if r.status == RunStatus.COMPLETED), key=lambda r: r.order):
            o = run_outcomes(run)
            v = objective_value(obj, o, self.plugins)
            if v is None or not is_feasible(evaluate_constraints(spec, run.configuration, o, plugins=self.plugins)):
                continue
            if best is None or oriented(obj, v) < oriented(obj, best):
                best = v
            trace.append((run.order, best))
        return trace

    def _feasible_candidates(self, spec, seen, n, seed, rejected, obs) -> list[dict[str, Any]]:
        rng = random.Random(seed)
        out: list[dict[str, Any]] = []
        keys = set(seen)
        elites = [c for c, _ in sorted(obs, key=lambda it: self._obs_rank(spec, it[1]))[:3]]
        attempts = 0
        while len(out) < n and attempts < n * 20:
            attempts += 1
            if elites and rng.random() < 0.3:
                base = rng.choice(elites)
                cfg = {}
                for f in spec.factors:
                    if f.factor_type == "continuous":
                        span = f.upper - f.lower
                        cfg[f.name] = min(f.upper, max(f.lower, base[f.name] + rng.gauss(0, 0.1 * span)))
                    else:
                        cfg[f.name] = base[f.name] if rng.random() < 0.7 else rng.choice(f.values)
            else:
                cfg = {
                    f.name: (rng.uniform(f.lower, f.upper) if f.factor_type == "continuous" else rng.choice(f.values))
                    for f in spec.factors
                }
            k = self._key(cfg)
            if k in keys:
                continue
            ev = evaluate_constraints(spec, cfg, None, kinds=("input",), plugins=self.plugins)
            if not is_feasible(ev):
                if len(rejected) < 20:
                    rejected.append(
                        {
                            "configuration": cfg,
                            "reasons": [
                                f"input constraint violated: {e['constraint']}" for e in ev if e["status"] != "satisfied"
                            ],
                        }
                    )
                continue
            if not all(f.accepts(cfg[f.name]) for f in spec.factors):
                continue
            keys.add(k)
            out.append(cfg)
        return out

    def _obs_rank(self, spec, outcomes) -> float:
        v = oriented_vector(spec, outcomes, self.plugins)
        return sum(v) if v else math.inf

    def _grid(self, spec, seen, count, rejected) -> list[Recommendation]:
        levels = [f.candidates(5) if f.factor_type == "continuous" else f.candidates() for f in spec.factors]
        recs = []
        for vals in itertools.product(*levels):
            cfg = dict(zip((f.name for f in spec.factors), vals))
            if self._key(cfg) in seen:
                continue
            ev = evaluate_constraints(spec, cfg, None, kinds=("input",), plugins=self.plugins)
            if not is_feasible(ev):
                if len(rejected) < 20:
                    rejected.append(
                        {
                            "configuration": cfg,
                            "reasons": [
                                f"input constraint violated: {e['constraint']}" for e in ev if e["status"] != "satisfied"
                            ],
                        }
                    )
                continue
            recs.append(
                self._rec(
                    spec, cfg, "grid", "Next unseen feasible point of the grid search (5 levels per continuous factor).", rejected
                )
            )
            if len(recs) == count:
                break
        return recs

    def _rec(self, spec, cfg, strategy, rationale, rejected, **kw) -> Recommendation:
        ev = evaluate_constraints(spec, cfg, None, kinds=("input",), plugins=self.plugins)
        out_c = [
            {
                "constraint": c.metric or c.function,
                "kind": "output",
                "status": "unknown",
                "note": "output constraint is evaluated only after measurement",
            }
            for c in spec.output_constraints()
        ]
        kw.setdefault("objective", ", ".join(o.metric for o in spec.objectives))
        return Recommendation(
            configuration=cfg,
            rationale=rationale,
            strategy=strategy,
            rejected=list(rejected),
            feasible=is_feasible(ev),
            constraint_evaluation=ev + out_c,
            uncertainty=kw.pop("uncertainty", "No predictive model: no uncertainty estimate."),
            **kw,
        )

    # --------------------------------------------------- legacy heuristic
    @staticmethod
    def _distance(spec, left, right) -> float:
        d = []
        for f in spec.factors:
            a, b = left.get(f.name), right.get(f.name)
            if f.factor_type == "continuous":
                d.append(abs(float(a) - float(b)) / (f.upper - f.lower))
            else:
                d.append(0.0 if a == b else 1 / max(1, len(f.values) - 1))
        return sum(d) / len(d) if d else 0.0

    def _heuristic(self, spec, obs, cands, count, rejected) -> list[Recommendation]:
        scored = []
        for c in cands:
            ws = []
            for cfg, o in obs:
                s = scalar_score(spec, o, self.plugins)
                if s is not None:
                    ws.append((1 / (1e-6 + self._distance(spec, cfg, c)) ** 2, s))
            scored.append((sum(w * s for w, s in ws) / sum(w for w, _ in ws) if ws else None, c))
        scored.sort(key=lambda it: (it[0] is not None, it[0] if it[0] is not None else 0), reverse=True)
        return [
            self._rec(
                spec,
                c,
                "heuristic",
                "Inverse-distance-weighted average of observed scores. This is an uncalibrated HEURISTIC, not a "
                "fitted probabilistic model.",
                rejected,
                prediction_kind="heuristic_score",
                heuristic_score=s,
                uncertainty="Heuristic only; no calibrated predictive uncertainty.",
            )
            for s, c in scored[:count]
        ]

    # ------------------------------------------------------------ bayesian
    def _bayesian(self, spec, obs, cands, count, seed, rejected) -> list[Recommendation]:
        enc = _Encoder(spec)
        rows = [(cfg, o, oriented_vector(spec, o, self.plugins)) for cfg, o in obs]
        rows = [r for r in rows if r[2] is not None]
        n_min = max(4, enc.dim + 1)
        if len(rows) < n_min or not cands:
            recs = [
                self._rec(
                    spec,
                    c,
                    "bayesian",
                    f"Surrogate model needs at least {n_min} completed observations "
                    f"(have {len(rows)}); falling back to space-filling random search.",
                    rejected,
                    uncertainty="No model fitted yet.",
                )
                for c in cands[:count]
            ]
            return recs
        k = len(spec.objectives)
        Y = np.array([r[2] for r in rows])
        X = np.array([enc.encode(r[0]) for r in rows])
        multi = k > 1
        rng = np.random.default_rng(seed)
        if multi:
            lows, highs = Y.min(0), Y.max(0)
            span = np.where(highs - lows > 0, highs - lows, 1.0)
            w = rng.dirichlet(np.ones(k) * 1.0) * 0.0 + np.array([o.weight for o in spec.objectives]) / sum(
                o.weight for o in spec.objectives
            )
            w = 0.5 * w + 0.5 * rng.dirichlet(np.ones(k))  # ParEGO: randomised weights around user weights
            Yn = (Y - lows) / span
            scal = (Yn * w).max(1) + 0.05 * (Yn * w).sum(1)  # augmented Chebyshev
        else:
            scal = Y[:, 0]
        # feasibility models for modellable output constraints
        cons = []
        for c in spec.output_constraints():
            if c.function or c.metric == "" or (c.equals is not None) or c.allowed_values or c.validator:
                continue
            vals = [(enc.encode(cfg), o[c.metric]) for cfg, o in obs if c.metric in o]
            if len(vals) >= n_min:
                cons.append((c, GaussianProcess(np.array([v[0] for v in vals]), np.array([v[1] for v in vals]), seed=seed)))
        non_modelled = [c.metric or c.function for c in spec.output_constraints() if not any(c is cc for cc, _ in cons)]
        feas_rows = [
            i
            for i, (cfg, o, _) in enumerate(rows)
            if is_feasible(evaluate_constraints(spec, cfg, o, kinds=("output",), plugins=self.plugins))
        ]
        best = float(min(scal[i] for i in feas_rows)) if feas_rows else None
        Xc = np.array([enc.encode(c) for c in cands])
        chosen: list[Recommendation] = []
        chosen_idx: list[int] = []
        Xfit, yfit = X.copy(), scal.copy()
        fb = best
        gp = GaussianProcess(Xfit, yfit, seed=seed)
        for it in range(count):
            mu, sd = gp.predict(Xc)
            pof = np.ones(len(cands))
            for c, cgp in cons:
                cm, cs = cgp.predict(Xc)
                pof *= prob_in_interval(cm, cs, c.minimum, c.maximum)
            acq = expected_improvement(mu, sd, fb, xi=0.0) * pof if fb is not None else pof * 1e-3 + pof * sd * 1e-6
            order = np.argsort(-acq)
            idx = next((int(i) for i in order if all(np.linalg.norm(Xc[i] - Xc[j]) > 1e-6 for j in chosen_idx)), int(order[0]))
            chosen_idx.append(idx)
            cfg = cands[idx]
            if multi:
                pv, ps, ei_units = None, None, "scalarised, min-max-normalised augmented-Chebyshev units"
            else:
                sign = (
                    1.0
                    if spec.objectives[0].direction == "minimize"
                    else -1.0
                    if spec.objectives[0].direction == "maximize"
                    else 1.0
                )
                pv = float(mu[idx]) * (sign if spec.objectives[0].direction != "target" else 1.0)
                ps = float(sd[idx])
                ei_units = (
                    "absolute deviation from target"
                    if spec.objectives[0].direction == "target"
                    else f"{spec.objectives[0].metric} units"
                )
            rec = self._rec(
                spec,
                cfg,
                "bayesian",
                (
                    f"Maximises Expected Improvement under a Matern-5/2 Gaussian-process surrogate fitted to {len(rows)} runs"
                    + (
                        f"; multiplied by predicted probability of feasibility for {len(cons)} output constraint(s)"
                        if cons
                        else ""
                    )
                    + (f"; multi-objective handled by ParEGO scalarisation (weights {np.round(w, 3).tolist()})" if multi else "")
                    + (
                        "; no feasible run observed yet, so the acquisition is the probability of feasibility"
                        if fb is None
                        else ""
                    )
                    + "."
                ),
                rejected,
                prediction_kind="model_prediction",
                objective=", ".join(o.metric for o in spec.objectives),
                predicted_value=pv,
                prediction_std=ps,
                expected_improvement=float(expected_improvement(mu[idx : idx + 1], sd[idx : idx + 1], fb)[0])
                if fb is not None
                else None,
                predicted_feasibility_probability=float(pof[idx]) if cons else None,
                uncertainty=(
                    "GP posterior standard deviation (assumes smooth stationary response and Gaussian noise; "
                    "not guaranteed calibrated). Expected improvement units: "
                    + ei_units
                    + "."
                    + (
                        f" Output constraints not modelled (checked only after measurement): {non_modelled}."
                        if non_modelled
                        else ""
                    )
                ),
            )
            rec.constraint_evaluation = (
                [e for e in rec.constraint_evaluation if e.get("kind") != "output"]
                + [
                    {
                        "constraint": c.metric,
                        "kind": "output",
                        "status": "predicted",
                        "probability_satisfied": float(
                            prob_in_interval(*cgp.predict(Xc[idx : idx + 1]), c.minimum, c.maximum)[0]
                        ),
                    }
                    for c, cgp in cons
                ]
                + [
                    {"constraint": n, "kind": "output", "status": "unknown", "note": "not modelled; evaluated after measurement"}
                    for n in non_modelled
                ]
            )
            chosen.append(rec)
            if it + 1 < count:  # kriging believer: pretend the prediction was observed
                Xfit = np.vstack([Xfit, Xc[idx]])
                yfit = np.append(yfit, mu[idx])
                fb = min(fb, float(mu[idx])) if fb is not None else None
                gp = GaussianProcess(Xfit, yfit, seed=seed + it + 1, restarts=2)
        return chosen

    # -------------------------------------------------------------- optuna
    def _optuna(self, spec, result, obs, count, seed, seen, rejected) -> list[Recommendation]:
        try:
            import optuna
            from optuna import distributions as D
        except ImportError as exc:
            raise OptimizationError("the 'optuna' strategy needs Optuna: pip install 'trialforge-core[optuna]'") from exc
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        k = len(spec.objectives)
        out_cons = [
            c
            for c in spec.output_constraints()
            if c.function is None and c.metric and (c.minimum is not None or c.maximum is not None)
        ]

        def cfunc(trial):
            return trial.user_attrs.get("constraint", [])

        kwargs: dict[str, Any] = {"seed": seed}
        if out_cons:
            kwargs["constraints_func"] = cfunc
        import importlib.util
        import warnings

        warnings.filterwarnings("ignore", category=FutureWarning, module="optuna")
        if k == 1 and importlib.util.find_spec("torch") is not None:
            sampler, label = optuna.samplers.GPSampler(**kwargs), "GPSampler"
        elif k == 1:
            sampler, label = optuna.samplers.TPESampler(multivariate=True, **kwargs), "TPESampler (GPSampler needs torch)"
        else:
            sampler, label = optuna.samplers.TPESampler(multivariate=True, **kwargs), "TPESampler (multi-objective)"
        study = optuna.create_study(directions=["minimize"] * k, sampler=sampler)
        dists = {
            f.name: (
                D.FloatDistribution(f.lower, f.upper)
                if f.factor_type == "continuous"
                else D.CategoricalDistribution(tuple(f.values))
            )
            for f in spec.factors
        }
        for run in result.runs:
            if run.status != RunStatus.COMPLETED:
                continue
            o = run_outcomes(run)
            vec = oriented_vector(spec, o, self.plugins)
            if vec is None:
                continue
            attrs = {}
            if out_cons:
                attrs["constraint"] = [
                    max(
                        ((c.minimum - o[c.metric]) if c.minimum is not None and c.metric in o else -1.0),
                        ((o[c.metric] - c.maximum) if c.maximum is not None and c.metric in o else -1.0),
                    )
                    for c in out_cons
                ]
            study.add_trial(
                optuna.trial.create_trial(params=dict(run.configuration), distributions=dists, values=vec, user_attrs=attrs)
            )
        recs: list[Recommendation] = []
        keys = set(seen)
        for _ in range(count * 30):
            if len(recs) >= count:
                break
            trial = study.ask(dists)
            cfg = {f.name: trial.params[f.name] for f in spec.factors}
            ev = evaluate_constraints(spec, cfg, None, kinds=("input",), plugins=self.plugins)
            bad = self._key(cfg) in keys or not is_feasible(ev)
            study.tell(trial, state=optuna.trial.TrialState.FAIL)
            if bad:
                continue
            keys.add(self._key(cfg))
            recs.append(
                self._rec(
                    spec,
                    cfg,
                    f"optuna:{label}",
                    f"Proposed by Optuna {label} seeded with {len(obs)} completed runs. The sampler does not expose "
                    "calibrated predictions, so none are reported.",
                    rejected,
                    uncertainty="Optuna sampler: no predictive uncertainty exposed.",
                )
            )
        return recs
