"""SQLAlchemy persistence (SQLite by default, any SQLAlchemy URL supported).

Tables: experiments (spec), plans, runs, measurements (raw), processed_measurements, analyses,
recommendations, reports, adapters, jobs (durable queue), state_log, schema_info.

Integrity: :meth:`SQLiteExperimentRepository.save_run` writes the run row, its raw and processed
measurements and the experiment status in **one transaction**, so a run can never be stored as
completed without its measurements.

Limitation: Python callables (``Factor.validation_rule``, ``Constraint.validator``) are not
serialisable. Restored specs carry ``metadata['_callables_not_restored']`` naming them; re-supply the
original spec (``ExperimentService.attach_spec``) to re-enable them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from trialforge.errors import PersistenceError
from trialforge.models.core import (
    AnalysisResult,
    DesignPlan,
    ExperimentReport,
    ExperimentResult,
    ExperimentSpec,
    ExperimentStatus,
    Measurement,
    ProcessedMeasurement,
    Recommendation,
    Run,
    RunStatus,
)

SCHEMA_VERSION = 2


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SchemaInfo(Base):
    __tablename__ = "schema_info"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class ExperimentRecord(Base):
    __tablename__ = "experiments"
    experiment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(40), default="created", index=True)
    adapter_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    spec_json: Mapped[str] = mapped_column(Text)
    spec_hash: Mapped[str] = mapped_column(String(64), default="")
    lost_callables: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PlanRecord(Base):
    __tablename__ = "plans"
    experiment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    plan_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RunRecord(Base):
    __tablename__ = "runs"
    run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    order: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), index=True)
    adapter_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    adapter_handle: Mapped[str | None] = mapped_column(String(300), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text)  # run without measurements
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MeasurementRecord(Base):
    __tablename__ = "measurements"
    measurement_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), index=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    metric: Mapped[str] = mapped_column(String(200), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class ProcessedRecord(Base):
    __tablename__ = "processed_measurements"
    processed_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), index=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    metric: Mapped[str] = mapped_column(String(200), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class AnalysisRecord(Base):
    __tablename__ = "analyses"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    payload_json: Mapped[str] = mapped_column(Text)


class RecommendationRecord(Base):
    __tablename__ = "recommendations"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    payload_json: Mapped[str] = mapped_column(Text)


class ReportRecord(Base):
    __tablename__ = "reports"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    report_version: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    payload_json: Mapped[str] = mapped_column(Text)


class AdapterRecord(Base):
    __tablename__ = "adapters"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[str] = mapped_column(String(50))
    metadata_json: Mapped[str] = mapped_column(Text)
    capabilities_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class JobRecord(Base):
    __tablename__ = "jobs"
    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[str] = mapped_column(String(100), index=True)
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|claimed|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StateLogRecord(Base):
    __tablename__ = "state_log"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    from_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_state: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


@runtime_checkable
class ExperimentRepository(Protocol):
    """Minimal snapshot contract (backward compatible with 0.1); implement it to add another storage backend."""

    def save(self, result: ExperimentResult) -> None:
        """Persist a complete ExperimentResult snapshot."""
        ...

    def get(self, experiment_id: str) -> ExperimentResult:
        """Load a stored ExperimentResult; raises KeyError if unknown."""
        ...

    def list_ids(self) -> list[str]:
        """IDs of all stored experiments."""
        ...


def _lost_callables(spec: ExperimentSpec) -> list[str]:
    lost = [f"factor:{f.name}" for f in spec.factors if f.validation_rule is not None]
    lost += [f"constraint:{c.metric}" for c in spec.constraints if c.validator is not None]
    return lost


def spec_hash(spec: ExperimentSpec) -> str:
    """Stable content hash of a spec (excludes volatile timestamps)."""
    data = spec.model_dump(mode="json", exclude={"created_at"})
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


class SQLiteExperimentRepository:
    """Durable repository. ``url`` is any SQLAlchemy URL; default is a local SQLite file."""

    def __init__(self, url: str = "sqlite:///trialforge.db") -> None:
        self.url = url
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite") and (":memory:" in url or url in ("sqlite://", "sqlite:///")):
            kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
        elif url.startswith("sqlite"):
            kwargs.update(connect_args={"check_same_thread": False, "timeout": 30})
        self.engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _pragmas(conn, _):  # noqa: ANN001
                cur = conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                if ":memory:" not in url:
                    cur.execute("PRAGMA journal_mode=WAL")
                cur.close()

        self.migrate()

    @classmethod
    def in_memory(cls) -> SQLiteExperimentRepository:
        """Ephemeral repository (lost when the process exits)."""
        return cls("sqlite:///:memory:")

    # ---------------------------------------------------------- migrations
    def schema_version(self) -> int:
        """Current schema version stored in the database (0 if uninitialised)."""
        insp = inspect(self.engine)
        if "schema_info" not in insp.get_table_names():
            return 0
        with Session(self.engine) as s:
            row = s.get(SchemaInfo, "version")
            return int(row.value) if row else 0

    def migrate(self) -> None:
        """Bring the database to ``SCHEMA_VERSION``; imports legacy 0.1 snapshot databases."""
        insp = inspect(self.engine)
        tables = insp.get_table_names()
        legacy = "experiments" in tables and "payload" in {c["name"] for c in insp.get_columns("experiments")}
        legacy_rows: list[str] = []
        if legacy:
            with self.engine.begin() as conn:
                legacy_rows = [r[0] for r in conn.execute(text("SELECT payload FROM experiments"))]
                conn.execute(text("ALTER TABLE experiments RENAME TO experiments_legacy_v1"))
        version = self.schema_version()
        Base.metadata.create_all(self.engine)
        if version < SCHEMA_VERSION:
            for payload in legacy_rows:
                self.save(ExperimentResult.model_validate_json(payload))
            with Session(self.engine) as s:
                s.merge(SchemaInfo(key="version", value=str(SCHEMA_VERSION)))
                s.commit()

    # -------------------------------------------------------- experiments
    def save_experiment(self, spec: ExperimentSpec, status: ExperimentStatus | None = None) -> None:
        """Upsert a specification. Raises if the id exists with a *different* spec hash and runs exist."""
        h = spec_hash(spec)
        with Session(self.engine) as s, s.begin():
            rec = s.get(ExperimentRecord, spec.experiment_id)
            payload = spec.model_dump_json()
            if rec is None:
                s.add(
                    ExperimentRecord(
                        experiment_id=spec.experiment_id,
                        name=spec.name,
                        status=(status or ExperimentStatus.CREATED).value,
                        adapter_name=spec.adapter_name,
                        spec_json=payload,
                        spec_hash=h,
                        lost_callables=json.dumps(_lost_callables(spec)),
                        created_at=spec.created_at,
                    )
                )
            else:
                if rec.spec_hash != h:
                    has_runs = s.scalar(select(RunRecord.run_id).where(RunRecord.experiment_id == spec.experiment_id).limit(1))
                    if has_runs:
                        raise PersistenceError(
                            "cannot change the specification of an experiment that already has runs; "
                            "create a new experiment (preserves reproducibility)"
                        )
                rec.spec_json, rec.spec_hash, rec.name, rec.adapter_name = payload, h, spec.name, spec.adapter_name
                rec.lost_callables = json.dumps(_lost_callables(spec))
                if status:
                    rec.status = status.value
                rec.updated_at = _now()

    def get_spec(self, experiment_id: str) -> ExperimentSpec:
        """Load a specification (callable validators are not restored; see module docs)."""
        with Session(self.engine) as s:
            rec = s.get(ExperimentRecord, experiment_id)
            if rec is None:
                raise KeyError(experiment_id)
            spec = ExperimentSpec.model_validate_json(rec.spec_json)
            lost = json.loads(rec.lost_callables or "[]")
            if lost:
                spec.metadata = {**spec.metadata, "_callables_not_restored": lost}
            return spec

    def has_experiment(self, experiment_id: str) -> bool:
        """True if an experiment with this id is stored."""
        with Session(self.engine) as s:
            return s.get(ExperimentRecord, experiment_id) is not None

    def get_status(self, experiment_id: str) -> ExperimentStatus:
        """Current experiment status."""
        with Session(self.engine) as s:
            rec = s.get(ExperimentRecord, experiment_id)
            if rec is None:
                raise KeyError(experiment_id)
            return ExperimentStatus(rec.status)

    def set_status(self, experiment_id: str, status: ExperimentStatus, reason: str = "") -> None:
        """Persist an experiment status change and log it."""
        with Session(self.engine) as s, s.begin():
            rec = s.get(ExperimentRecord, experiment_id)
            if rec is None:
                raise KeyError(experiment_id)
            if rec.status != status.value:
                s.add(StateLogRecord(experiment_id=experiment_id, from_state=rec.status, to_state=status.value, reason=reason))
                rec.status, rec.updated_at = status.value, _now()

    def list_ids(self) -> list[str]:
        """IDs of all stored experiments, sorted."""
        with Session(self.engine) as s:
            return list(s.scalars(select(ExperimentRecord.experiment_id).order_by(ExperimentRecord.experiment_id)))

    # -------------------------------------------------------------- plans
    def save_plan(self, plan: DesignPlan) -> None:
        """Upsert the design plan (run measurements are stored separately)."""
        stub = plan.model_copy(deep=True)
        for r in stub.runs:
            r.measurements, r.processed_measurements = [], []
        with Session(self.engine) as s, s.begin():
            rec = s.get(PlanRecord, plan.experiment_id)
            if rec is None:
                s.add(PlanRecord(experiment_id=plan.experiment_id, plan_json=stub.model_dump_json()))
            else:
                rec.plan_json, rec.updated_at = stub.model_dump_json(), _now()

    def get_plan(self, experiment_id: str) -> DesignPlan:
        """Load the stored design plan; raises KeyError if none."""
        with Session(self.engine) as s:
            rec = s.get(PlanRecord, experiment_id)
            if rec is None:
                raise KeyError(f"no plan stored for {experiment_id}")
            return DesignPlan.model_validate_json(rec.plan_json)

    # --------------------------------------------------------------- runs
    def save_runs(self, runs: list[Run], experiment_status: ExperimentStatus | None = None) -> None:
        """Atomically persist runs with their measurements (and optionally the experiment status)."""
        if not runs:
            return
        exp_id = runs[0].experiment_id
        with Session(self.engine) as s, s.begin():
            for run in runs:
                self._write_run(s, run)
            if experiment_status is not None:
                rec = s.get(ExperimentRecord, exp_id)
                if rec is not None and rec.status != experiment_status.value:
                    s.add(
                        StateLogRecord(
                            experiment_id=exp_id,
                            from_state=rec.status,
                            to_state=experiment_status.value,
                            reason="derived from run states",
                        )
                    )
                    rec.status, rec.updated_at = experiment_status.value, _now()

    def save_run(self, run: Run, experiment_status: ExperimentStatus | None = None) -> None:
        """Atomically persist one run with its measurements."""
        self.save_runs([run], experiment_status)

    @staticmethod
    def _write_run(s: Session, run: Run) -> None:
        stub = run.model_copy(update={"measurements": [], "processed_measurements": []})
        rec = s.get(RunRecord, run.run_id)
        prev_status = rec.status if rec else None
        if rec is None:
            s.add(
                RunRecord(
                    run_id=run.run_id,
                    experiment_id=run.experiment_id,
                    order=run.order,
                    status=run.status.value,
                    adapter_name=run.adapter_name,
                    adapter_handle=run.adapter_handle,
                    attempts=run.attempts,
                    idempotency_key=run.idempotency_key,
                    payload_json=stub.model_dump_json(),
                )
            )
        else:
            rec.order, rec.status, rec.adapter_name, rec.adapter_handle = (
                run.order,
                run.status.value,
                run.adapter_name,
                run.adapter_handle,
            )
            rec.attempts, rec.idempotency_key, rec.payload_json, rec.updated_at = (
                run.attempts,
                run.idempotency_key,
                stub.model_dump_json(),
                _now(),
            )
        if prev_status != run.status.value:
            s.add(
                StateLogRecord(
                    experiment_id=run.experiment_id,
                    run_id=run.run_id,
                    from_state=prev_status,
                    to_state=run.status.value,
                    reason=(run.history[-1]["reason"] if run.history else ""),
                )
            )
        for m in {m.measurement_id: m for m in run.measurements}.values():  # last write wins per id
            # merge = upsert: quality flags can be reassessed; raw values are never altered by the engine
            s.merge(
                MeasurementRecord(
                    measurement_id=m.measurement_id,
                    run_id=run.run_id,
                    experiment_id=run.experiment_id,
                    metric=m.metric,
                    payload_json=m.model_dump_json(),
                )
            )
        keep = {p.processed_id for p in run.processed_measurements}
        for old in s.scalars(select(ProcessedRecord).where(ProcessedRecord.run_id == run.run_id)):
            if old.processed_id not in keep:
                s.delete(old)
        for p in run.processed_measurements:
            s.merge(
                ProcessedRecord(
                    processed_id=p.processed_id,
                    run_id=run.run_id,
                    experiment_id=run.experiment_id,
                    metric=p.metric,
                    payload_json=p.model_dump_json(),
                )
            )

    def _load_run(self, s: Session, rec: RunRecord) -> Run:
        run = Run.model_validate_json(rec.payload_json)
        run.measurements = [
            Measurement.model_validate_json(p)
            for p in s.scalars(select(MeasurementRecord.payload_json).where(MeasurementRecord.run_id == rec.run_id))
        ]
        run.processed_measurements = [
            ProcessedMeasurement.model_validate_json(p)
            for p in s.scalars(select(ProcessedRecord.payload_json).where(ProcessedRecord.run_id == rec.run_id))
        ]
        run.measurements.sort(key=lambda m: m.timestamp)
        return run

    def get_run(self, run_id: str) -> Run:
        """Load one run with all measurements; raises KeyError if unknown."""
        with Session(self.engine) as s:
            rec = s.get(RunRecord, run_id)
            if rec is None:
                raise KeyError(run_id)
            return self._load_run(s, rec)

    def list_runs(self, experiment_id: str, status: RunStatus | None = None) -> list[Run]:
        """Runs of an experiment ordered by run order, optionally filtered by status."""
        with Session(self.engine) as s:
            q = select(RunRecord).where(RunRecord.experiment_id == experiment_id).order_by(RunRecord.order)
            if status:
                q = q.where(RunRecord.status == status.value)
            return [self._load_run(s, r) for r in s.scalars(q)]

    def find_run_by_idempotency_key(self, experiment_id: str, key: str) -> Run | None:
        """Find a run by its idempotency key (None if absent)."""
        with Session(self.engine) as s:
            rec = s.scalar(select(RunRecord).where(RunRecord.experiment_id == experiment_id, RunRecord.idempotency_key == key))
            return self._load_run(s, rec) if rec else None

    # ----------------------------------------------- snapshots (0.1 API)
    def save(self, result: ExperimentResult) -> None:
        """Persist a whole result (spec, plan, runs, measurements) in one transaction-per-part."""
        self.save_experiment(result.spec, result.status)
        self.save_plan(result.plan)
        self.save_runs(result.runs, result.status)
        self.set_status(result.spec.experiment_id, result.status, "snapshot save")

    def get(self, experiment_id: str) -> ExperimentResult:
        """Load the full ExperimentResult (spec, plan, runs, status); raises KeyError if unknown."""
        spec = self.get_spec(experiment_id)
        plan = self.get_plan(experiment_id)
        runs = self.list_runs(experiment_id)
        by_id = {r.run_id: r for r in runs}
        plan.runs = [by_id.get(r.run_id, r) for r in plan.runs]
        extra = [r for r in runs if r.run_id not in {p.run_id for p in plan.runs}]
        plan.runs = plan.runs + extra
        return ExperimentResult(spec=spec, plan=plan, runs=plan.runs, status=self.get_status(experiment_id))

    # ------------------------------------------- analyses / recs / reports
    def save_analysis(self, analysis: AnalysisResult) -> None:
        """Append an analysis to the experiment's history."""
        with Session(self.engine) as s, s.begin():
            s.add(AnalysisRecord(experiment_id=analysis.experiment_id, payload_json=analysis.model_dump_json()))

    def list_analyses(self, experiment_id: str) -> list[AnalysisResult]:
        """Stored analyses, oldest first."""
        with Session(self.engine) as s:
            return [
                AnalysisResult.model_validate_json(p)
                for p in s.scalars(
                    select(AnalysisRecord.payload_json)
                    .where(AnalysisRecord.experiment_id == experiment_id)
                    .order_by(AnalysisRecord.seq)
                )
            ]

    def save_recommendations(self, experiment_id: str, recs: list[Recommendation]) -> None:
        """Append recommendations to the experiment's optimisation history."""
        with Session(self.engine) as s, s.begin():
            for r in recs:
                s.add(RecommendationRecord(experiment_id=experiment_id, payload_json=r.model_dump_json()))

    def list_recommendations(self, experiment_id: str) -> list[Recommendation]:
        """Stored recommendations, oldest first."""
        with Session(self.engine) as s:
            return [
                Recommendation.model_validate_json(p)
                for p in s.scalars(
                    select(RecommendationRecord.payload_json)
                    .where(RecommendationRecord.experiment_id == experiment_id)
                    .order_by(RecommendationRecord.seq)
                )
            ]

    def save_report(self, report: ExperimentReport) -> None:
        """Append a report."""
        with Session(self.engine) as s, s.begin():
            s.add(
                ReportRecord(
                    experiment_id=report.experiment_id,
                    report_version=report.report_version,
                    payload_json=report.model_dump_json(),
                )
            )

    def list_reports(self, experiment_id: str) -> list[ExperimentReport]:
        """Stored reports, oldest first."""
        with Session(self.engine) as s:
            return [
                ExperimentReport.model_validate_json(p)
                for p in s.scalars(
                    select(ReportRecord.payload_json)
                    .where(ReportRecord.experiment_id == experiment_id)
                    .order_by(ReportRecord.seq)
                )
            ]

    # ------------------------------------------------------------ adapters
    def record_adapter(self, metadata, capabilities) -> None:
        """Record adapter metadata and capabilities used by this database."""
        with Session(self.engine) as s, s.begin():
            s.merge(
                AdapterRecord(
                    name=metadata.name,
                    version=metadata.version,
                    metadata_json=json.dumps(asdict(metadata), default=list),
                    capabilities_json=json.dumps(asdict(capabilities)),
                    updated_at=_now(),
                )
            )

    def list_adapters(self) -> list[dict[str, Any]]:
        """Recorded adapter metadata and capabilities."""
        with Session(self.engine) as s:
            return [
                {
                    "name": r.name,
                    "version": r.version,
                    "metadata": json.loads(r.metadata_json),
                    "capabilities": json.loads(r.capabilities_json),
                }
                for r in s.scalars(select(AdapterRecord))
            ]

    # ---------------------------------------------------------- job queue
    def enqueue(self, experiment_id: str, run_id: str) -> int:
        """Persist pending work; idempotent per (run, open job)."""
        with Session(self.engine) as s, s.begin():
            open_job = s.scalar(select(JobRecord).where(JobRecord.run_id == run_id, JobRecord.state.in_(["pending", "claimed"])))
            if open_job:
                return open_job.job_id
            job = JobRecord(experiment_id=experiment_id, run_id=run_id)
            s.add(job)
            s.flush()
            return job.job_id

    def claim(self, experiment_id: str | None = None, limit: int = 1) -> list[tuple[int, str, str]]:
        """Atomically claim up to ``limit`` pending jobs -> [(job_id, experiment_id, run_id)]."""
        with Session(self.engine) as s, s.begin():
            q = select(JobRecord).where(JobRecord.state == "pending").order_by(JobRecord.job_id).limit(limit)
            if experiment_id:
                q = q.where(JobRecord.experiment_id == experiment_id)
            jobs = list(s.scalars(q))
            for j in jobs:
                j.state, j.claimed_at, j.attempts = "claimed", _now(), j.attempts + 1
            return [(j.job_id, j.experiment_id, j.run_id) for j in jobs]

    def finish_job(self, job_id: int, state: str = "done") -> None:
        """Mark a queued job done or failed."""
        with Session(self.engine) as s, s.begin():
            s.execute(update(JobRecord).where(JobRecord.job_id == job_id).values(state=state))

    def finish_jobs_for_run(self, run_id: str, state: str = "done") -> None:
        """Close all open jobs of a run."""
        with Session(self.engine) as s, s.begin():
            s.execute(
                update(JobRecord)
                .where(JobRecord.run_id == run_id, JobRecord.state.in_(["pending", "claimed"]))
                .values(state=state)
            )

    def release_claimed(self, experiment_id: str | None = None) -> int:
        """Return claimed (interrupted) jobs to pending, e.g. after a restart."""
        with Session(self.engine) as s, s.begin():
            q = update(JobRecord).where(JobRecord.state == "claimed")
            if experiment_id:
                q = q.where(JobRecord.experiment_id == experiment_id)
            return s.execute(q.values(state="pending", claimed_at=None)).rowcount

    def pending_jobs(self, experiment_id: str | None = None) -> list[tuple[int, str, str, str]]:
        """Open (pending or claimed) jobs as (job_id, experiment_id, run_id, state)."""
        with Session(self.engine) as s:
            q = select(JobRecord).where(JobRecord.state.in_(["pending", "claimed"])).order_by(JobRecord.job_id)
            if experiment_id:
                q = q.where(JobRecord.experiment_id == experiment_id)
            return [(j.job_id, j.experiment_id, j.run_id, j.state) for j in s.scalars(q)]

    # ----------------------------------------------------------- state log
    def state_log(self, experiment_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        """Chronological state transitions for an experiment (or one run)."""
        with Session(self.engine) as s:
            q = select(StateLogRecord).where(StateLogRecord.experiment_id == experiment_id).order_by(StateLogRecord.seq)
            if run_id:
                q = q.where(StateLogRecord.run_id == run_id)
            return [
                {"run_id": r.run_id, "from": r.from_state, "to": r.to_state, "reason": r.reason, "at": r.at.isoformat()}
                for r in s.scalars(q)
            ]


InMemoryExperimentRepository = SQLiteExperimentRepository.in_memory
"""Factory alias: ``InMemoryExperimentRepository()`` returns an ephemeral SQLite repository."""
