"""Versioned JSON / CSV reports. No plotting dependencies: applications render their own visuals.

``ExperimentReport.report_version`` follows semantic meaning: new *optional* fields bump the minor
version, removals/renames bump the major version.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import platform
from typing import Any

from trialforge.definitions.registry import DEFAULT_PLUGINS, PluginRegistry
from trialforge.evaluation import evaluate_constraints, run_outcomes
from trialforge.measurements.quality import INVALID_FLAGS, quality_status
from trialforge.models.core import (
    AnalysisResult,
    ExperimentReport,
    ExperimentResult,
    Recommendation,
    RunStatus,
)
from trialforge.persistence.repository import spec_hash

REPORT_VERSION = "2.0"


def _safe(v: Any) -> Any:
    """Neutralise spreadsheet formula injection in string cells."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


class ReportEngine:
    """Assemble :class:`ExperimentReport` from a result and its analysis."""

    def __init__(self, *, plugins: PluginRegistry | None = None, extensions: list[str] | None = None) -> None:
        self.plugins = plugins or DEFAULT_PLUGINS
        self.extensions = list(extensions or [])  # explicit opt-in; nothing runs implicitly

    def generate(
        self,
        result: ExperimentResult,
        analysis: AnalysisResult,
        suggestions: list[Recommendation] | None = None,
        *,
        adapters: list | None = None,
    ) -> ExperimentReport:
        """Build an ExperimentReport from a result and analysis (``suggestions`` become the optimisation history)."""
        suggestions = suggestions or []
        spec, plan = result.spec, result.plan
        runs = {r.run_id: r for r in result.runs}
        best_run = runs.get(analysis.best_run_id) if analysis.best_run_id else None
        violations: list[dict[str, Any]] = []
        quality: list[dict[str, Any]] = []
        invalid_meas: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        invalid_runs = 0
        for run in result.runs:
            if run.status == RunStatus.FAILED:
                failed.append(
                    {
                        "run_id": run.run_id,
                        "order": run.order,
                        "error": run.error,
                        "attempts": run.attempts,
                        "configuration": run.configuration,
                    }
                )
            if run.status == RunStatus.COMPLETED:
                outcomes = run_outcomes(run)
                for e in evaluate_constraints(spec, run.configuration, outcomes, plugins=self.plugins):
                    if e["status"] == "violated":
                        violations.append(
                            {"run_id": run.run_id, "metric": e["constraint"], "kind": e["kind"], "value": e.get("value")}
                        )
                    elif e["status"] == "unknown":
                        quality.append({"run_id": run.run_id, "issue": f"constraint '{e['constraint']}' could not be evaluated"})
            warns = run.metadata.get("quality_warnings") or run.metadata.get("data_quality_warnings") or []
            if warns:
                invalid_runs += 1
                quality.extend({"run_id": run.run_id, "issue": w} for w in warns)
            for m in run.measurements:
                s = quality_status(m.quality_flags)
                if s == "invalid":
                    invalid_meas.append(
                        {
                            "run_id": run.run_id,
                            "measurement_id": m.measurement_id,
                            "metric": m.metric,
                            "value": repr(m.value),
                            "flags": sorted(set(m.quality_flags) & INVALID_FLAGS),
                        }
                    )
                elif s in ("outlier", "sensor_warning"):
                    quality.append({"run_id": run.run_id, "measurement_id": m.measurement_id, "metric": m.metric, "issue": s})
        limitations = list(analysis.limitations)
        if best_run is None and result.runs:
            limitations.append("No feasible observed run: no best configuration is reported.")
        for a in adapters or []:
            if getattr(a, "synthetic", False):
                limitations.append(f"Adapter '{a.name}' produces SYNTHETIC data; results do not describe real systems.")
        report = ExperimentReport(
            experiment_id=spec.experiment_id,
            name=spec.name,
            objective=spec.objectives,
            hypothesis=spec.hypothesis,
            variables=spec.factors,
            design_method=plan.method,
            planned_runs=len(plan.runs),
            completed_runs=sum(r.status == RunStatus.COMPLETED for r in result.runs),
            failed_runs=len(failed),
            invalid_runs=invalid_runs,
            measurement_summaries=analysis.descriptive,
            treatment_effects=analysis.effects,
            best_configuration=best_run.configuration if best_run else None,
            constraint_violations=violations,
            optimisation_history=suggestions,
            suggested_next_experiment=suggestions[-1] if suggestions else None,
            limitations=limitations,
            report_version=REPORT_VERSION,
            description=spec.description,
            constraints=spec.constraints,
            design_info=plan.design_info,
            planned_run_details=[
                {
                    "run_id": r.run_id,
                    "order": r.order,
                    "status": r.status.value,
                    "configuration": r.configuration,
                    "block": r.block,
                    "replicate": r.replicate,
                }
                for r in plan.runs
            ],
            failed_run_details=failed,
            invalid_measurements=invalid_meas,
            interactions=analysis.interactions,
            anova=analysis.anova,
            regression=analysis.regression,
            model_diagnostics=[{"metric": m.metric, **m.diagnostics, "warnings": m.warnings} for m in analysis.regression],
            best_observed_run_id=analysis.best_run_id,
            pareto_run_ids=analysis.pareto_run_ids,
            infeasible_runs=analysis.infeasible_runs,
            data_quality_issues=quality,
            analysis_warnings=analysis.warnings,
            reproducibility=self._repro(result, analysis, adapters),
        )
        for name in self.extensions:
            report.extensions[name] = self.plugins.get("report", name)(report, result)
        return report

    def build(self, result, analysis, *, recommendations=None, adapters=None):
        """Service-facing wrapper around :meth:`generate`."""
        return self.generate(result, analysis, recommendations, adapters=adapters)

    @staticmethod
    def _repro(result: ExperimentResult, analysis: AnalysisResult, adapters) -> dict[str, Any]:
        from trialforge import __version__

        plan = result.plan
        raw = hashlib.sha256(
            json.dumps(
                [
                    [m.model_dump(mode="json", include={"measurement_id", "metric", "value", "unit"}) for m in r.measurements]
                    for r in sorted(result.runs, key=lambda r: r.order)
                ],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return {
            "trialforge_version": __version__,
            "python": platform.python_version(),
            "specification_hash": spec_hash(result.spec),
            "random_seed": result.spec.random_seed,
            "design_method": plan.method,
            "design_options": result.spec.design_options,
            "run_order": [r.run_id for r in sorted(plan.runs, key=lambda r: r.order)],
            "raw_measurement_hash": raw,
            "analysis_algorithm": analysis.algorithm,
            "analysis_version": analysis.algorithm_version,
            "analysis_parameters": analysis.parameters,
            "adapters": [{"name": a.name, "version": a.version, "synthetic": a.synthetic} for a in (adapters or [])],
        }

    # ------------------------------------------------------------- exports
    @staticmethod
    def to_json(report: ExperimentReport) -> str:
        """Stable, human-readable JSON."""
        return report.model_dump_json(indent=2)

    @staticmethod
    def to_csv(result: ExperimentResult) -> str:
        """One row per run: status, error, configuration, block, feasibility and run-level outcomes."""
        outcomes = {r.run_id: run_outcomes(r) for r in result.runs}
        metrics = sorted({m for o in outcomes.values() for m in o} | result.spec.metric_names)  # stable schema
        fields = [
            "run_id",
            "order",
            "status",
            "attempts",
            "error",
            "feasible",
            "block",
            *[f.name for f in result.spec.factors],
            *metrics,
        ]
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=fields)
        w.writeheader()
        for run in result.runs:
            row: dict[str, Any] = {
                "run_id": run.run_id,
                "order": run.order,
                "status": run.status.value,
                "attempts": run.attempts,
                "error": _safe(run.error or ""),
                "feasible": run.metadata.get("feasible", ""),
                "block": json.dumps(run.block) if run.block else "",
                **{k: _safe(v) for k, v in run.configuration.items()},
            }
            if run.status == RunStatus.COMPLETED:
                row.update(outcomes[run.run_id])
            w.writerow(row)
        return out.getvalue()

    @staticmethod
    def measurements_csv(result: ExperimentResult) -> str:
        """Raw measurements with provenance and quality status (invalid ones included and labelled)."""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(
            [
                "run_id",
                "measurement_id",
                "metric",
                "value",
                "unit",
                "timestamp",
                "channel",
                "source",
                "quality_status",
                "quality_flags",
                "raw_value",
                "raw_unit",
            ]
        )
        for run in result.runs:
            for m in run.measurements:
                w.writerow(
                    [
                        run.run_id,
                        m.measurement_id,
                        m.metric,
                        _safe(m.value),
                        m.unit,
                        m.timestamp.isoformat(),
                        m.channel or "",
                        m.source or "",
                        quality_status(m.quality_flags),
                        ";".join(m.quality_flags),
                        m.raw_value if m.raw_value is not None else "",
                        m.raw_unit or "",
                    ]
                )
        return out.getvalue()

    @staticmethod
    def effects_csv(analysis: AnalysisResult) -> str:
        """CSV of all treatment and factor-level comparisons with intervals, p-values and evidence labels."""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(
            [
                "metric",
                "comparison",
                "factor",
                "level",
                "control_level",
                "differing_factors",
                "n_control",
                "n_treatment",
                "control_mean",
                "treatment_mean",
                "difference",
                "ci_low",
                "ci_high",
                "p_value",
                "p_holm",
                "effect_size",
                "evidence",
                "warnings",
            ]
        )
        for e in [*analysis.effects, *analysis.main_effects]:
            ci = e.confidence_interval_95 or ("", "")
            w.writerow(
                [
                    e.metric,
                    e.comparison_type,
                    e.factor or "",
                    e.level if e.level is not None else "",
                    e.control_level if e.control_level is not None else "",
                    "|".join(e.differing_factors),
                    e.sample_size_control,
                    e.sample_size_treatment,
                    e.control_mean,
                    e.treatment_mean,
                    e.mean_difference,
                    ci[0],
                    ci[1],
                    e.p_value,
                    e.p_value_holm,
                    e.effect_size,
                    e.evidence,
                    " | ".join(e.warnings),
                ]
            )
        return out.getvalue()
