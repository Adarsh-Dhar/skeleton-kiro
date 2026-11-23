"""Durable import jobs (``f1_import_job``): progress survives restarts; interrupted jobs are marked as such."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from db.engine import immediate_transaction
from db.models.f1 import ImportJob

ACTIVE = ("QUEUED", "DOWNLOADING", "PROCESSING", "SAVING")
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


def dedupe_key(payload: dict) -> str:
    drivers = ",".join(sorted({d.strip().upper() for d in payload.get("drivers") or []})) or "*"
    flags = "".join("1" if payload.get(k, True) else "0" for k in ("include_telemetry", "include_position", "include_weather"))
    prefix = f"{payload.get('provider') or 'openf1'}|"
    flags += f"|race{int(bool(payload.get('include_race_data', True)))}"
    if payload.get("race_datasets") is not None:
        flags += "|ds=" + ",".join(sorted(set(payload["race_datasets"])))
    return f"{prefix}{int(payload['year'])}|{str(payload['event']).strip().lower()}|{str(payload['session']).strip().upper()}|{drivers}|{flags}"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.isoformat()


class ImportJobStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _view(j: ImportJob) -> dict[str, Any]:
        return {"import_id": j.import_id, "state": j.state, "detail": j.detail, "error": j.error, "error_code": j.error_code,
                "session_id": j.session_id, "attempts": j.attempts, "retry_of": j.retry_of, "cancel_requested": bool(j.cancel_requested),
                "created_at": _iso(j.created_at), "updated_at": _iso(j.updated_at), "finished_at": _iso(j.finished_at),
                "request": json.loads(j.request_json)}  # fmt: skip

    def create(self, payload: dict, *, retry_of: str | None = None, attempts: int = 1) -> dict:
        jid = "imp-" + uuid.uuid4().hex[:10]
        with immediate_transaction(self.engine) as c:
            c.execute(ImportJob.__table__.insert().values(
                import_id=jid, state="QUEUED", detail="queued", request_json=json.dumps(payload), dedupe_key=dedupe_key(payload),
                attempts=attempts, retry_of=retry_of, cancel_requested=False, created_at=_now(), updated_at=_now()))
        return self.get(jid)

    def get(self, import_id: str) -> dict | None:
        with Session(self.engine) as s:
            j = s.get(ImportJob, import_id)
            return self._view(j) if j else None

    def list(self, limit: int = 50) -> list[dict]:
        with Session(self.engine) as s:
            rows = s.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit)).all()
            return [self._view(j) for j in rows]

    def find_active(self, key: str) -> dict | None:
        with Session(self.engine) as s:
            j = s.scalars(select(ImportJob).where(ImportJob.dedupe_key == key, ImportJob.state.in_(ACTIVE))
                          .order_by(ImportJob.created_at.desc())).first()
            return self._view(j) if j else None

    def find_completed(self, key: str) -> dict | None:
        with Session(self.engine) as s:
            j = s.scalars(select(ImportJob).where(ImportJob.dedupe_key == key, ImportJob.state == "COMPLETED")
                          .order_by(ImportJob.created_at.desc())).first()
            return self._view(j) if j else None

    def update(self, import_id: str, **values: Any) -> None:
        values["updated_at"] = _now()
        if values.get("state") in TERMINAL:
            values.setdefault("finished_at", _now())
        with immediate_transaction(self.engine) as c:
            c.execute(update(ImportJob).where(ImportJob.import_id == import_id).values(**values))

    def progress(self, import_id: str, state: str, detail: str) -> None:
        """Progress update that never overwrites a terminal state (e.g. a cancel that already finished the job)."""
        with immediate_transaction(self.engine) as c:
            c.execute(update(ImportJob).where(ImportJob.import_id == import_id, ImportJob.state.in_(ACTIVE))
                      .values(state=state, detail=detail, updated_at=_now()))

    def cancel_requested(self, import_id: str) -> bool:
        with self.engine.connect() as c:
            return bool(c.execute(select(ImportJob.cancel_requested).where(ImportJob.import_id == import_id)).scalar())

    def request_cancel(self, import_id: str) -> None:
        self.update(import_id, cancel_requested=True)

    def mark_interrupted(self) -> int:
        """Called at startup: jobs that were running when the process stopped can never finish."""
        with immediate_transaction(self.engine) as c:
            return c.execute(update(ImportJob).where(ImportJob.state.in_(ACTIVE)).values(
                state="FAILED", error_code="INTERRUPTED", error="the application stopped while this import was running; nothing was stored. Retry it.",
                detail="interrupted by a restart", updated_at=_now(), finished_at=_now())).rowcount
