"""App-scoped view of the shared SDK experiment tables.

Both apps open the same database through the unmodified ``SQLiteExperimentRepository``. ``ScopedExperimentRepository``
subclasses it so each app only sees, loads and writes the experiments it created (recorded in
``app_experiment_owner``). Another app's experiment id behaves exactly like an unknown id (``KeyError`` /
``has_experiment() == False``), and an app can never overwrite an experiment owned by the other app.
"""
from __future__ import annotations

import os

from sqlalchemy import text
from trialforge import PersistenceError, SQLiteExperimentRepository

from db.config import resolve_db_url
from db.engine import create_db_engine, immediate_transaction
from db.migrations import migrate
from db.models.common import APPS


class ScopedExperimentRepository(SQLiteExperimentRepository):
    def __init__(self, app: str, target: str | os.PathLike[str] | None = None) -> None:
        if app not in APPS:
            raise ValueError(f"unknown app '{app}' (expected one of {APPS})")
        url = resolve_db_url(target)
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            raise ValueError("the root database must be a file; in-memory databases cannot be shared between the SDK and app tables")
        self.app = app
        self.db_engine = create_db_engine(url)
        migrate(self.db_engine)
        self._owned: set[str] = set()
        super().__init__(url)

    # ---------------------------------------------------------------- ownership
    def owner_of(self, experiment_id: str) -> str | None:
        with self.db_engine.connect() as c:
            return c.execute(text("SELECT app FROM app_experiment_owner WHERE experiment_id=:i"), {"i": experiment_id}).scalar()

    def exists_anywhere(self, experiment_id: str) -> bool:
        """True if the id exists for *any* app (used to report id conflicts before designing)."""
        return super().has_experiment(experiment_id)

    def _owns(self, experiment_id: str) -> bool:
        if experiment_id in self._owned:
            return True
        if self.owner_of(experiment_id) == self.app:
            self._owned.add(experiment_id)
            return True
        return False

    def _require(self, experiment_id: str) -> None:
        if not self._owns(experiment_id):
            raise KeyError(experiment_id)

    def claim(self, experiment_id: str, *, exists_in_sdk: bool = False) -> None:
        """Record ownership of ``experiment_id``; refuse if the other app owns it (or an unowned row already exists)."""
        with immediate_transaction(self.db_engine) as c:
            row = c.execute(text("SELECT app FROM app_experiment_owner WHERE experiment_id=:i"), {"i": experiment_id}).scalar()
            if row == self.app:
                self._owned.add(experiment_id)
                return
            if row is not None:
                raise PersistenceError(f"experiment id '{experiment_id}' belongs to another application")
            if exists_in_sdk:
                raise PersistenceError(f"experiment id '{experiment_id}' already exists and has no owner; run the migration tool")
            c.execute(text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES (:i, :a, CURRENT_TIMESTAMP)"),
                      {"i": experiment_id, "a": self.app})
            self._owned.add(experiment_id)

    # ---------------------------------------------------------------- scoped reads
    def list_ids(self) -> list[str]:
        with self.db_engine.connect() as c:
            owned = {r[0] for r in c.execute(text("SELECT experiment_id FROM app_experiment_owner WHERE app=:a"), {"a": self.app})}
        return [i for i in super().list_ids() if i in owned]

    def has_experiment(self, experiment_id: str) -> bool:
        return self._owns(experiment_id) and super().has_experiment(experiment_id)

    def get_spec(self, experiment_id):
        self._require(experiment_id)
        return super().get_spec(experiment_id)

    def get_status(self, experiment_id):
        self._require(experiment_id)
        return super().get_status(experiment_id)

    def set_status(self, experiment_id, status, reason=""):
        self._require(experiment_id)
        return super().set_status(experiment_id, status, reason)

    def get_plan(self, experiment_id):
        self._require(experiment_id)
        return super().get_plan(experiment_id)

    def list_runs(self, experiment_id, status=None):
        self._require(experiment_id)
        return super().list_runs(experiment_id, status)

    def get_run(self, run_id):
        run = super().get_run(run_id)
        self._require(getattr(run, "experiment_id", None) or self._experiment_of_run(run_id))
        return run

    def _experiment_of_run(self, run_id: str) -> str:
        with self.engine.connect() as c:
            row = c.execute(text("SELECT experiment_id FROM runs WHERE run_id=:r"), {"r": run_id}).scalar()
        return row or ""

    def release_claimed(self, experiment_id: str | None = None) -> int:
        if experiment_id is not None:
            self._require(experiment_id)
            return super().release_claimed(experiment_id)
        return sum(super(ScopedExperimentRepository, self).release_claimed(i) for i in self.list_ids())

    def pending_jobs(self, experiment_id: str | None = None):
        if experiment_id is not None:
            self._require(experiment_id)
            return super().pending_jobs(experiment_id)
        owned = set(self.list_ids())
        return [j for j in super().pending_jobs(None) if j[1] in owned]

    # ---------------------------------------------------------------- scoped write
    def save_experiment(self, spec, status=None) -> None:
        exists = super().has_experiment(spec.experiment_id)  # SDK-level check, ignoring ownership
        self.claim(spec.experiment_id, exists_in_sdk=exists)
        super().save_experiment(spec, status)
