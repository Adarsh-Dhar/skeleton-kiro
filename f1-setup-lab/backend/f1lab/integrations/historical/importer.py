"""Pieces shared by the OpenF1 import: error codes, the import error, session codes and event-name resolution."""

from __future__ import annotations

import socket
from collections.abc import Callable

import pandas as pd

Progress = Callable[[str, str], None]  # (state, detail)

SESSION_CODE = {"FP1": "FP1", "FP2": "FP2", "FP3": "FP3", "Q": "Q", "QUALIFYING": "Q",
                "SQ": "SQ", "SPRINT QUALIFYING": "SQ", "S": "S", "SPRINT": "S", "R": "R", "RACE": "R"}

# error codes reported to the UI/CLI (also stored on the import job)
INVALID_REQUEST = "INVALID_REQUEST"
EVENT_NOT_FOUND = "EVENT_NOT_FOUND"
SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"
DRIVER_NOT_FOUND = "DRIVER_NOT_FOUND"
NO_LAP_DATA = "NO_LAP_DATA"
NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
CANCELLED = "CANCELLED"
INTERRUPTED = "INTERRUPTED"
INTERNAL = "INTERNAL"


class HistoricalImportError(ValueError):
    """An import failure with a stable ``code`` (see the constants above)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ImportCancelled(Exception):
    pass


def classify(exc: BaseException) -> str:
    """Map any exception raised during an import to an error code."""
    if isinstance(exc, HistoricalImportError):
        return exc.code
    if isinstance(exc, ImportCancelled):
        return CANCELLED
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(chain) < 10:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    text = " ".join(str(e).lower() for e in chain)
    if any(isinstance(e, (TimeoutError, socket.timeout)) for e in chain) or "timed out" in text or "timeout" in text:
        return NETWORK_TIMEOUT
    if any(isinstance(e, ConnectionError) for e in chain) or "nodename nor servname" in text or "failed to resolve" in text:
        return NETWORK_ERROR
    if isinstance(exc, ValueError):
        return INVALID_REQUEST
    return INTERNAL


def _session_code(session: str) -> str:
    code = SESSION_CODE.get(session.strip().upper(), session.strip().upper())
    if code not in ("FP1", "FP2", "FP3", "Q", "SQ", "S", "R"):
        raise HistoricalImportError(INVALID_REQUEST, f"unsupported session '{session}'; use one of FP1, FP2, FP3, Q, SQ, S, R")
    return code


def resolve_event(schedule: pd.DataFrame, event: str) -> str:
    """Map a user-supplied event string ('Monza', 'Italian Grand Prix') to a schedule EventName."""
    want = event.strip().lower()
    names = schedule["EventName"].astype(str).str.lower().tolist() if "EventName" in schedule.columns else []
    locs = schedule["Location"].astype(str).str.lower().tolist() if "Location" in schedule.columns else []
    for i, v in enumerate(names):
        if v == want:
            return schedule.iloc[i]["EventName"]
    for col in (names, locs):
        for i, v in enumerate(col):
            if v == want or want in v or v in want:
                return schedule.iloc[i]["EventName"]
    raise HistoricalImportError(EVENT_NOT_FOUND, f"no Grand Prix matching '{event}' in the season schedule")
