"""Shared run-outcome, objective and constraint evaluation (a mid-level layer used by analysis, optimisation, reporting).

One implementation is used by analysis, optimisation and reporting so that
"best", "feasible" and "Pareto" always mean the same thing.
"""

from __future__ import annotations

import math
from typing import Any

from trialforge.definitions.registry import DEFAULT_PLUGINS, PluginRegistry
from trialforge.measurements.quality import is_usable
from trialforge.models.core import Constraint, ExperimentSpec, Objective, Run, RunStatus


def run_outcomes(run: Run, *, exclude_outliers: bool = False) -> dict[str, float]:
    """Run-level numeric outcome per metric from *usable* processed measurements.

    Multiple processed records for a metric (aggregation 'none' or several windows) are
    averaged. Falls back to raw measurements when no processed ones exist.
    """
    records = run.processed_measurements
    if not records:
        records = [m for m in run.measurements]
    buckets: dict[str, list[float]] = {}
    for rec in records:
        flags = set(rec.quality_flags)
        if not is_usable(flags) or (exclude_outliers and "statistical_outlier" in flags):
            continue
        v = rec.value
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            continue
        buckets.setdefault(rec.metric, []).append(float(v))
    return {k: sum(v) / len(v) for k, v in buckets.items()}


def objective_value(objective: Objective, outcomes: dict[str, float], plugins: PluginRegistry | None = None) -> float | None:
    """Raw objective value (before orientation) or None if unavailable."""
    if objective.function:
        try:
            return float((plugins or DEFAULT_PLUGINS).get("objective", objective.function)(outcomes))
        except Exception:  # noqa: BLE001
            return None
    return outcomes.get(objective.metric)


def oriented(objective: Objective, value: float) -> float:
    """Orient so that *smaller is better* for every direction."""
    if objective.direction == "minimize":
        return value
    if objective.direction == "maximize":
        return -value
    return abs(value - objective.target)


def oriented_vector(
    spec: ExperimentSpec, outcomes: dict[str, float], plugins: PluginRegistry | None = None
) -> list[float] | None:
    vec = []
    for obj in spec.objectives:
        v = objective_value(obj, outcomes, plugins)
        if v is None:
            return None
        vec.append(oriented(obj, v))
    return vec


def scalar_score(spec: ExperimentSpec, outcomes: dict[str, float], plugins: PluginRegistry | None = None) -> float | None:
    """Weighted *larger-is-better* score; None if any objective is unavailable."""
    vec = oriented_vector(spec, outcomes, plugins)
    if vec is None:
        return None
    total_w = sum(o.weight for o in spec.objectives)
    return -sum(o.weight * v for o, v in zip(spec.objectives, vec)) / total_w


def evaluate_constraint(
    spec: ExperimentSpec,
    constraint: Constraint,
    configuration: dict[str, Any],
    outcomes: dict[str, float] | None,
    plugins: PluginRegistry | None = None,
) -> dict[str, Any]:
    """Evaluate one constraint. ``status`` is satisfied / violated / unknown.

    Output constraints with no available outcome are ``unknown`` (never silently
    satisfied).
    """
    kind = spec.constraint_kind(constraint)
    name = constraint.metric or constraint.function
    base = {"constraint": name, "kind": kind}
    try:
        if constraint.function:
            fn = (plugins or DEFAULT_PLUGINS).get("constraint", constraint.function)
            ok = bool(fn(configuration, outcomes or {}))
            if kind == "output" and not outcomes:
                return {**base, "status": "unknown", "value": None}
            return {**base, "status": "satisfied" if ok else "violated", "value": None}
        if kind == "input":
            value = configuration.get(constraint.metric)
            if value is None:
                return {**base, "status": "unknown", "value": None}
        else:
            if outcomes is None or constraint.metric not in outcomes:
                return {**base, "status": "unknown", "value": None}
            value = outcomes[constraint.metric]
        return {**base, "status": "satisfied" if constraint.accepts(value) else "violated", "value": value}
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "unknown", "value": None, "error": str(exc)}


def evaluate_constraints(
    spec: ExperimentSpec,
    configuration: dict[str, Any],
    outcomes: dict[str, float] | None,
    *,
    kinds: tuple[str, ...] = ("input", "output"),
    plugins: PluginRegistry | None = None,
) -> list[dict[str, Any]]:
    return [
        evaluate_constraint(spec, c, configuration, outcomes, plugins)
        for c in spec.constraints
        if spec.constraint_kind(c) in kinds
    ]


def is_feasible(evaluation: list[dict[str, Any]], *, unknown_is_feasible: bool = False) -> bool:
    """Feasible when nothing is violated and (by default) nothing is unknown."""
    return all(e["status"] == "satisfied" or (unknown_is_feasible and e["status"] == "unknown") for e in evaluation)


def completed_runs(runs: list[Run]) -> list[Run]:
    return [r for r in runs if r.status == RunStatus.COMPLETED]


def pareto_front(points: dict[str, list[float]]) -> list[str]:
    """IDs of non-dominated points (all objectives oriented smaller-is-better)."""
    front = []
    for rid, p in points.items():
        dominated = any(
            o != rid and all(a <= b for a, b in zip(q, p)) and any(a < b for a, b in zip(q, p)) for o, q in points.items()
        )
        if not dominated:
            front.append(rid)
    return front


def weighted_normalized(spec: ExperimentSpec, vectors: dict[str, list[float]]) -> dict[str, float]:
    """Weighted sum of min-max-normalised oriented objectives (smaller is better).

    Normalisation makes the weights scale-free across objectives with different units.
    A single objective reduces to the (monotone) raw oriented value.
    """
    if not vectors:
        return {}
    k = len(spec.objectives)
    lows = [min(v[i] for v in vectors.values()) for i in range(k)]
    highs = [max(v[i] for v in vectors.values()) for i in range(k)]
    total_w = sum(o.weight for o in spec.objectives)
    out = {}
    for rid, v in vectors.items():
        s = 0.0
        for i, o in enumerate(spec.objectives):
            span = highs[i] - lows[i]
            s += o.weight * ((v[i] - lows[i]) / span if span > 0 else 0.0)
        out[rid] = s / total_w
    return out
