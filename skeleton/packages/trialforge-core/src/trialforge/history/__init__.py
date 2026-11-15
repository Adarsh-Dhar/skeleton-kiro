"""Read-only views over persisted experiment history."""

from __future__ import annotations

from typing import Any

from trialforge.evaluation import run_outcomes
from trialforge.models.core import RunStatus


class ExperimentHistory:
    """Configurations, outcomes, analyses, recommendations and state transitions of one experiment."""

    def __init__(self, repository, experiment_id: str) -> None:
        self.repo, self.experiment_id = repository, experiment_id

    def runs(self, status: RunStatus | None = None):
        """Runs of the experiment, optionally filtered by status."""
        return self.repo.list_runs(self.experiment_id, status)

    def configurations(self) -> list[dict[str, Any]]:
        """Every run in order with status, configuration and aggregated outcomes (failed runs are labelled)."""
        return [
            {
                "run_id": r.run_id,
                "order": r.order,
                "status": r.status.value,
                "configuration": r.configuration,
                "outcomes": run_outcomes(r) if r.status == RunStatus.COMPLETED else {},
                "error": r.error,
                "attempts": r.attempts,
                "block": r.block,
            }
            for r in self.runs()
        ]

    def best_so_far(self) -> list[dict[str, Any]]:
        """Trace of the best feasible run (by normalised weighted objective) after each completed run."""
        from trialforge.evaluation import evaluate_constraints, is_feasible, oriented_vector, weighted_normalized

        spec = self.repo.get_spec(self.experiment_id)
        done, trace, best = [], [], None
        for r in sorted((r for r in self.runs() if r.status == RunStatus.COMPLETED), key=lambda r: r.order):
            o = run_outcomes(r)
            v = oriented_vector(spec, o)
            if v is None or not is_feasible(evaluate_constraints(spec, r.configuration, o)):
                continue
            done.append((r, v))
            scores = weighted_normalized(spec, {x.run_id: vv for x, vv in done})
            best = min(scores, key=scores.get)
            trace.append({"after_run": r.run_id, "order": r.order, "best_run_id": best})
        return trace

    def analyses(self):
        """Persisted analyses, oldest first."""
        return self.repo.list_analyses(self.experiment_id)

    def recommendations(self):
        """Persisted recommendations, oldest first."""
        return self.repo.list_recommendations(self.experiment_id)

    def reports(self):
        """Persisted reports, oldest first."""
        return self.repo.list_reports(self.experiment_id)

    def state_log(self, run_id: str | None = None):
        """Chronological state transitions for the experiment (or one run)."""
        return self.repo.state_log(self.experiment_id, run_id)


__all__ = ["ExperimentHistory"]
