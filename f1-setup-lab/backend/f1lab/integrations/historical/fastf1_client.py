"""Thin wrapper around FastF1. Synchronous and network-bound: callers must run it in a worker thread."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class FastF1Unavailable(RuntimeError):
    pass


class FastF1Client:
    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            import fastf1
        except ImportError as exc:  # pragma: no cover - fastf1 is a declared dependency
            raise FastF1Unavailable("fastf1 is not installed") from exc
        self._ff = fastf1
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
        try:
            session.load(laps=laps, telemetry=telemetry, weather=weather, messages=messages, circuit_info=circuit_info)
        except TypeError:  # older FastF1 without circuit_info kwarg
            session.load(laps=laps, telemetry=telemetry, weather=weather, messages=messages)
        return session
