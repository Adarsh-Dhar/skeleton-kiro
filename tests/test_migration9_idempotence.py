"""Property test: migration 9 is idempotent."""
from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from db import create_db_engine, current_version
from db.migrations import migrate
from db.migrations.game_data import DDL, create_game_tables
from db.migrations.versions import MIGRATIONS

RESTORED = tuple(DDL)  # the six tables migration 9 creates, parents first
_text = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=20)


def _snapshot(engine) -> dict:
    """Schema (sqlite_master) and every row of every table."""
    with engine.connect() as c:
        schema = sorted(tuple(r) for r in c.execute(text("SELECT type, name, tbl_name, sql FROM sqlite_master")))
        tables = [r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))]
        rows = {t: sorted(map(repr, c.execute(text(f'SELECT * FROM "{t}"')).fetchall())) for t in tables}
    return {"schema": schema, "rows": rows}


@st.composite
def _v8_content(draw):
    owners = draw(st.lists(st.tuples(_text, st.sampled_from(["f1-setup-lab", "wattwise-lab"])),
                           unique_by=lambda t: t[0], max_size=5))
    present = draw(st.lists(st.sampled_from(RESTORED), unique=True, max_size=len(RESTORED)))
    reports = draw(st.lists(_text, unique=True, max_size=4)) if "f1_probe_report" in present else []
    return owners, present, reports


# Feature: f1-setup-lab-restoration, Property 2: Migration 9 is idempotent
@settings(max_examples=100, deadline=None)
@given(_v8_content())
def test_migration9_is_idempotent(content):
    """**Validates: Requirements 3.5**"""
    owners, present, reports = content
    m9 = next(m for m in MIGRATIONS if m.version == 9)
    with tempfile.TemporaryDirectory() as d:
        engine = create_db_engine(Path(d) / "t.db")
        try:
            migrate(engine, target=8)
            assert current_version(engine) == 8
            with engine.begin() as c:
                for eid, app in owners:
                    c.execute(text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) "
                                   "VALUES (:e, :a, CURRENT_TIMESTAMP)"), {"e": eid, "a": app})
                if present:
                    create_game_tables(c, *[t for t in RESTORED if t in present])
                for rid in reports:
                    c.execute(text("INSERT INTO f1_probe_report (report_id, created_at, report_json) "
                                   "VALUES (:r, CURRENT_TIMESTAMP, '{}')"), {"r": rid})

            migrate(engine, target=9)
            assert current_version(engine) == 9
            first = _snapshot(engine)

            with engine.begin() as c:  # second application, apply function called directly
                m9.apply(c)
            assert _snapshot(engine) == first

            assert migrate(engine, target=9) == []  # second application through migrate
            assert _snapshot(engine) == first
        finally:
            engine.dispose()
