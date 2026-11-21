"""Where the root database lives."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_ENV = "TRIALFORGE_DB"
REPO_ROOT = Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    """``$TRIALFORGE_DB`` (a file path) or ``<repo root>/data/trialforge.db``."""
    env = os.environ.get(DEFAULT_DB_ENV)
    if env and "://" not in env:
        return Path(env).expanduser().resolve()
    return REPO_ROOT / "data" / "trialforge.db"


def sqlite_file_path(url: str) -> Path | None:
    """The file behind a ``sqlite:///path`` URL (None for in-memory or non-SQLite URLs)."""
    if not url.startswith("sqlite:///") or ":memory:" in url or url == "sqlite:///":
        return None
    return Path(url.split("///", 1)[1])


def resolve_db_url(target: str | os.PathLike[str] | None = None) -> str:
    """Return a SQLAlchemy URL for ``target`` (URL, file path, or None for the default root database)."""
    if target is None:
        env = os.environ.get(DEFAULT_DB_ENV)
        if env and "://" in env:
            return env
        return f"sqlite:///{default_db_path()}"
    t = str(target)
    if "://" in t:
        return t
    return f"sqlite:///{Path(t).expanduser().resolve()}"
