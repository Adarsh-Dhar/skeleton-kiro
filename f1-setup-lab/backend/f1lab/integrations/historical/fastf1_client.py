"""Thin wrapper around FastF1. Synchronous and network-bound: callers must run it in a worker thread."""

from __future__ import annotations

import io
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path


class FastF1Unavailable(RuntimeError):
    pass


def cache_stats(cache_dir: str | Path) -> dict:
    """Files and bytes in FastF1's cache directory (used to show whether an import downloaded or reused data)."""
    root = Path(cache_dir)
    files = n = 0
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                files += 1
                n += p.stat().st_size
    return {"files": files, "bytes": n, "dir": str(root)}


class FastF1Client:
    def __init__(self, cache_dir: str | Path, interactive: bool = False) -> None:
        """``interactive=True`` lets FastF1 prompt for F1TV account credentials on the terminal (command-line use only)."""
        self.cache_dir = Path(cache_dir)
        self.interactive = interactive
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            import fastf1
        except ImportError as exc:  # pragma: no cover - fastf1 is a declared dependency
            raise FastF1Unavailable("fastf1 is not installed") from exc
        self._ff = fastf1
        # every import goes through the same on-disk HTTP cache
        if hasattr(fastf1.Cache, "configure"):  # FastF1 >= 3.9 (enable_cache is deprecated)
            fastf1.Cache.configure(cache_dir=str(self.cache_dir))
        else:  # pragma: no cover - older FastF1
            fastf1.Cache.enable_cache(str(self.cache_dir))

    @property
    def fastf1(self):
        return self._ff

    def get_schedule(self, year: int):
        """Season schedule as a pandas DataFrame (EventName, SessionName, SessionType, EventDate, ...)."""
        try:
            return self._ff.get_event_schedule(year, include_testing=False)
        except Exception as exc:
            raise FastF1Unavailable(f"could not load the {year} schedule: {exc}") from exc

    def get_session(self, year: int, event: str, session_type: str):
        try:
            return self._ff.get_session(year, event, session_type)
        except Exception as exc:
            raise FastF1Unavailable(f"could not load session {year}/{event}/{session_type}: {exc}") from exc

    def load_session(self, year: int, event: str, session_type: str, *, laps=True, telemetry=True, weather=True, messages=True, circuit_info=True):
        session = self.get_session(year, event, session_type)
        with _stdin_policy(self.interactive):
            try:
                session.load(laps=laps, telemetry=telemetry, weather=weather, messages=messages, circuit_info=circuit_info)
            except TypeError:  # older FastF1 without circuit_info kwarg
                session.load(laps=laps, telemetry=telemetry, weather=weather, messages=messages)
        return session


def credentials_configured() -> bool:
    """FastF1 (3.9+) signs in with F1_USERNAME / F1_PASSWORD to read the live-timing archive; the account needs an active
    F1TV Access, Pro or Premium subscription (a free Formula 1 account is not enough). Only presence is checked here."""
    return bool(os.environ.get("F1_USERNAME") and os.environ.get("F1_PASSWORD"))


_STDIN_LOCK = threading.Lock()


@contextmanager
def _stdin_policy(interactive: bool):
    """FastF1 prompts for F1TV account credentials when stdin is a terminal. Inside the web app that prompt would block
    an import thread forever, so non-interactive clients hide the terminal for the duration of the load (FastF1 then
    continues without authentication and the import reports the missing data)."""
    if interactive or sys.stdin is None or not sys.stdin.isatty():
        yield
        return
    with _STDIN_LOCK:
        saved = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            yield
        finally:
            sys.stdin = saved
