"""Shared fixtures. Adds the workspace root so ``examples`` is importable when pytest runs inside the package."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if (ROOT / "examples").is_dir() and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from trialforge import (  # noqa: E402
    DesignPlan,
    ExperimentResult,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
    ProcessedMeasurement,
    Run,
    RunStatus,
)


def build_result(spec: ExperimentSpec, rows, *, control=None, method="factorial", blocks=None, metric="y"):
    """Create a completed ExperimentResult from ``rows = [(config, value), ...]`` (run-level outcomes).

    ``blocks`` optionally gives a block dict per row. Used to feed analyses with *known* data.
    """
    runs = []
    for i, (cfg, val) in enumerate(rows):
        values = val if isinstance(val, dict) else {metric: val}
        pm = [
            ProcessedMeasurement(run_id=f"r{i}", metric=m, value=v, unit="u", source_measurement_ids=[f"s{i}{m}"])
            for m, v in values.items()
        ]
        runs.append(
            Run(
                run_id=f"r{i}",
                experiment_id=spec.experiment_id,
                configuration=cfg,
                order=i,
                status=RunStatus.COMPLETED,
                processed_measurements=pm,
                block=(blocks[i] if blocks else {}),
            )
        )
    plan = DesignPlan(
        experiment_id=spec.experiment_id, method=method, runs=runs, control_configuration=control or rows[0][0], random_seed=0
    )
    return ExperimentResult(spec=spec, plan=plan, runs=runs, status="completed")


def simple_spec(factors, *, metric="y", direction="minimize", **kw):
    defs = kw.pop("measurement_definitions", [MeasurementDefinition(metric=metric, unit="u")])
    objectives = kw.pop("objectives", [Objective(metric=metric, direction=direction)])
    return ExperimentSpec(
        name="t", factors=factors, objectives=objectives, measurement_definitions=defs, run_budget=kw.pop("run_budget", 500), **kw
    )


@pytest.fixture
def two_level_spec():
    return simple_spec(
        [Factor(name="a", factor_type="discrete", values=[0, 1]), Factor(name="b", factor_type="discrete", values=[0, 1])]
    )
