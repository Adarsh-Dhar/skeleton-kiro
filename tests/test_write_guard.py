"""Property test: the write guard rejects tables outside the domain (requirement 6.8)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine, text

from db.guard import PERMITTED, TableNotPermitted, install_guard

ALL_TABLES = sorted(PERMITTED["historical"] | PERMITTED["setup"])
EXTRA = ["users", "audit_log", "Other_Table", "f1_hist_session_x", "experiments2"]
DOMAINS = ("historical", "setup")

_quote = st.sampled_from(['{}', '"{}"', '`{}`', '[{}]'])
_case = st.sampled_from(["upper", "lower", "title"])
_ws = st.sampled_from([" ", "  ", "\n", "\t", " /* c */ ", " -- note\n "])
_lead = st.sampled_from(["", " ", "\n", "/* lead */ ", "-- lead\n"])


def _kw(word: str, case: str) -> str:
    return word.upper() if case == "upper" else word.lower() if case == "lower" else word.title()


@st.composite
def _statement(draw, table: str):
    verb = draw(st.sampled_from(["INSERT", "INSERT OR REPLACE", "INSERT OR IGNORE", "REPLACE", "UPDATE", "DELETE"]))
    case, q, ws, lead = draw(_case), draw(_quote), draw(_ws), draw(_lead)
    name = q.format(table)
    k = lambda w: " ".join(_kw(p, case) for p in w.split())  # noqa: E731
    if verb == "UPDATE":
        sql = f"{k('UPDATE')}{ws}{name}{ws}{k('SET')} v = 'x'"
    elif verb == "DELETE":
        sql = f"{k('DELETE FROM')}{ws}{name}"
    else:
        sql = f"{k(verb)}{ws}{k('INTO')}{ws}{name} (id, v) {k('VALUES')} (99, 'x')"
    return lead + sql


def _setup_db(path: Path, tables: list[str]):
    raw = create_engine(f"sqlite:///{path}")
    with raw.begin() as c:
        for t in tables:
            c.execute(text(f'CREATE TABLE "{t}" (id INTEGER PRIMARY KEY, v TEXT)'))
            c.execute(text(f'INSERT INTO "{t}" (id, v) VALUES (1, \'seed\')'))
    return raw


def _snapshot(engine) -> dict:
    with engine.connect() as c:
        return {t: c.execute(text(f'SELECT id, v FROM "{t}" ORDER BY id')).fetchall() for t in ALL_TABLES + EXTRA}


# Feature: f1-setup-lab-restoration, Property 14: The write guard rejects tables outside the domain
# **Validates: Requirements 6.8**
@settings(max_examples=100, deadline=None)
@given(data=st.data(), domain=st.sampled_from(DOMAINS))
def test_guard_rejects_tables_outside_domain(data, domain):
    outside = sorted((set(ALL_TABLES) | set(EXTRA)) - PERMITTED[domain])
    table = data.draw(st.sampled_from(outside))
    stmt = data.draw(_statement(table))
    with tempfile.TemporaryDirectory() as tmp:
        raw = _setup_db(Path(tmp) / "g.db", ALL_TABLES + EXTRA)
        engine = create_engine(f"sqlite:///{Path(tmp) / 'g.db'}")
        install_guard(engine, domain)
        before = _snapshot(raw)
        with pytest.raises(Exception) as exc:
            with engine.begin() as c:
                c.execute(text(stmt))
        err = exc.value
        while not isinstance(err, TableNotPermitted) and err.__cause__ is not None:
            err = err.__cause__
        assert isinstance(err, TableNotPermitted), repr(exc.value)
        assert table in str(err)
        assert _snapshot(raw) == before
        raw.dispose()
        engine.dispose()


@settings(max_examples=100, deadline=None)
@given(data=st.data(), domain=st.sampled_from(DOMAINS))
def test_guard_allows_permitted_writes_and_reads(data, domain):
    table = data.draw(st.sampled_from(sorted(PERMITTED[domain])))
    stmt = data.draw(_statement(table))
    with tempfile.TemporaryDirectory() as tmp:
        raw = _setup_db(Path(tmp) / "g.db", ALL_TABLES + EXTRA)
        engine = create_engine(f"sqlite:///{Path(tmp) / 'g.db'}")
        install_guard(engine, domain)
        with engine.begin() as c:
            c.execute(text(stmt))
        with engine.connect() as c:
            for t in ALL_TABLES + EXTRA:  # reads are unrestricted
                c.execute(text(f'SELECT * FROM "{t}"')).fetchall()
        raw.dispose()
        engine.dispose()
