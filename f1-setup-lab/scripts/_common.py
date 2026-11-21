"""Shared bootstrap for the F1 SETUP LAB command-line scripts (path setup + root database)."""
from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
for p in (APP / "backend", REPO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from db import create_db_engine, migrate, resolve_db_url  # noqa: E402
from db.config import sqlite_file_path  # noqa: E402


def root_engine(db: str | None):
    url = resolve_db_url(db)
    engine = create_db_engine(url)
    migrate(engine)
    return engine, url


def cache_dir_for(url: str) -> Path:
    file = sqlite_file_path(url)
    return (file.parent if file else Path("data")) / "fastf1-cache"
