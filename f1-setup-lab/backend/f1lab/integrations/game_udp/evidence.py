"""Per-run evidence (``f1_stint_evidence``) and decoded stint telemetry (``f1_stint_telemetry``) in the root database.

Evidence = what was asked for, what the game reported, which laps were used, and why a stint was accepted or rejected.
Telemetry is stored one row per measured sample, exactly as decoded (never downsampled).
"""
from __future__ import annotations

import json

from sqlalchemy import Engine, delete, func, select

from db.engine import immediate_transaction
from db.models.f1 import StintEvidence, StintTelemetrySample


def evidence_ref(exp_id: str, run_id: str) -> str:
    """Stable reference stored in measurement metadata (replaces the old file path)."""
    return f"db:f1_stint_evidence/{exp_id}/{run_id}"


def save_telemetry_series(engine: Engine, exp_id: str, run_id: str, records: list[dict]) -> int:
    """Replace the decoded telemetry samples of one run atomically. Returns the number stored."""
    rows = [(exp_id, run_id, i, rec.get("session_time"), json.dumps(rec, separators=(",", ":"))) for i, rec in enumerate(records)]
    with immediate_transaction(engine) as c:
        c.execute(delete(StintTelemetrySample).where(StintTelemetrySample.experiment_id == exp_id,
                                                     StintTelemetrySample.run_id == run_id))
        if rows:
            c.exec_driver_sql("INSERT INTO f1_stint_telemetry (experiment_id, run_id, seq, session_time, sample_json) "
                              "VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def load_telemetry_series(engine: Engine, exp_id: str, run_id: str) -> list[dict] | None:
    q = (select(StintTelemetrySample.sample_json)
         .where(StintTelemetrySample.experiment_id == exp_id, StintTelemetrySample.run_id == run_id)
         .order_by(StintTelemetrySample.seq))
    with engine.connect() as c:
        rows = c.execute(q).scalars().all()
    return [json.loads(r) for r in rows] if rows else None


def telemetry_series_count(engine: Engine, exp_id: str, run_id: str) -> int:
    q = (select(func.count()).select_from(StintTelemetrySample)
         .where(StintTelemetrySample.experiment_id == exp_id, StintTelemetrySample.run_id == run_id))
    with engine.connect() as c:
        return int(c.execute(q).scalar() or 0)


def save_evidence(engine: Engine, exp_id: str, run_id: str, payload: dict) -> str:
    """Upsert the evidence of one run. Returns its reference (see ``evidence_ref``)."""
    body = json.dumps(payload, default=str)
    with immediate_transaction(engine) as c:
        c.execute(delete(StintEvidence).where(StintEvidence.experiment_id == exp_id, StintEvidence.run_id == run_id))
        c.execute(StintEvidence.__table__.insert().values(experiment_id=exp_id, run_id=run_id, payload_json=body))
    return evidence_ref(exp_id, run_id)


def load_evidence(engine: Engine, exp_id: str, run_id: str) -> dict | None:
    q = select(StintEvidence.payload_json).where(StintEvidence.experiment_id == exp_id, StintEvidence.run_id == run_id)
    with engine.connect() as c:
        row = c.execute(q).scalar()
    return json.loads(row) if row else None
