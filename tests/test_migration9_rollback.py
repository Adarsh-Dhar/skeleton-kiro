"""Migration 9 is atomic: a failing table creation rolls back everything and leaves the database at version 8."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from db import create_db_engine, current_version
from db.base import Base
from db.migrations import MigrationError, game_data, migrate

# The statements of migration 9, in order (see _m009_f1_setup_restored).
STATEMENTS = ("app_experiment_owner", *game_data.DDL)


def _tables(engine) -> set[str]:
    with engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}


# Feature: f1-setup-lab-restoration, Property 3: A failing statement in migration 9 rolls everything back
# Validates: Requirements 3.9
# The input space is the 7 statement positions, and each example builds a database, so max_examples is 7 (every position
# is tried at least once in expectation) instead of the default 100.
@settings(max_examples=7, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(k=st.integers(min_value=0, max_value=len(STATEMENTS) - 1))
def test_failing_statement_rolls_back_migration_9(k: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_db_engine(Path(tmp) / "root.db")
        try:
            migrate(engine, target=8)
            with engine.begin() as c:
                c.execute(text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) "
                               "VALUES ('keep-me', 'f1-setup-lab', CURRENT_TIMESTAMP)"))
            before_tables = _tables(engine)
            with engine.connect() as c:
                before_rows = c.execute(text("SELECT experiment_id, app FROM app_experiment_owner")).fetchall()
            assert current_version(engine) == 8

            failing = STATEMENTS[k]
            if failing in game_data.DDL:  # the game tables are created from frozen DDL: break the statement of the failing table
                inject = mock.patch.dict(game_data.DDL, {failing: "CREATE TABLE injected failure ("})
            else:
                inject = mock.patch.object(Base.metadata.tables[failing], "create", side_effect=RuntimeError("injected failure"))
            with inject:
                with pytest.raises(MigrationError) as info:
                    migrate(engine, target=9)

            assert failing in str(info.value)
            assert current_version(engine) == 8
            assert _tables(engine) == before_tables
            for earlier in STATEMENTS[:k]:
                if earlier not in before_tables:
                    assert earlier not in _tables(engine)
            with engine.connect() as c:
                assert c.execute(text("SELECT experiment_id, app FROM app_experiment_owner")).fetchall() == before_rows
        finally:
            engine.dispose()
