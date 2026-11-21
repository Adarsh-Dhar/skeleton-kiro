"""Database repository for normalized historical sessions (root database, ``f1_hist_*`` tables).

A session is written in ONE transaction: its rows, its per-frame column lists and finally ``status='complete'``.
A failed or interrupted import therefore leaves nothing behind, and only complete sessions are listed or opened.

The FastF1 download cache (FastF1's own HTTP cache, not application data) stays a directory: ``cache_dir``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, delete, select

from db.engine import immediate_transaction
from db.frames import read_frame, schema_of, write_frame
from db.models.f1 import (
    LAP_COLUMNS,
    POSITION_COLUMNS,
    TELEMETRY_COLUMNS,
    WEATHER_COLUMNS,
    HistFrame,
    HistLap,
    HistPosition,
    HistSession,
    HistTelemetry,
    HistWeather,
)

_SAFE = re.compile(r"[^a-z0-9]+")
COMPLETE = "complete"
_TABLES = {"laps": (HistLap, LAP_COLUMNS), "weather": (HistWeather, WEATHER_COLUMNS),
           "telemetry": (HistTelemetry, TELEMETRY_COLUMNS), "position": (HistPosition, POSITION_COLUMNS)}  # fmt: skip


def slug(text: str) -> str:
    return _SAFE.sub("-", str(text).lower()).strip("-") or "x"


def session_id(year: int, event: str, session_type: str) -> str:
    """Deterministic, path-safe session id."""
    return f"fastf1-{year}-{slug(event)}-{slug(session_type)}"


def _check(sid: str) -> str:
    if not isinstance(sid, str) or "/" in sid or ".." in sid or sid.startswith(".") or not sid:
        raise ValueError(f"unsafe session id: {sid!r}")
    return sid


class HistoricalRepository:
    def __init__(self, engine: Engine, cache_dir: str | Path) -> None:
        self.engine = engine
        self.cache_dir = Path(cache_dir)

    # ----------------------------------------------------------------- writes
    def save_session(self, sid: str, manifest: dict, laps: pd.DataFrame, weather: pd.DataFrame,
                     telemetry: dict[str, pd.DataFrame], position: dict[str, pd.DataFrame]) -> str:
        """Replace session ``sid`` atomically. Returns the session id."""
        _check(sid)
        frames: list[tuple[str, str, pd.DataFrame]] = [("laps", "", laps), ("weather", "", weather)]
        frames += [("telemetry", str(code), df) for code, df in telemetry.items()]
        frames += [("position", str(code), df) for code, df in position.items()]
        with immediate_transaction(self.engine) as conn:
            conn.execute(delete(HistSession).where(HistSession.session_id == sid))  # cascades to frames and rows
            conn.execute(HistSession.__table__.insert().values(
                session_id=sid, status="importing", provider=manifest.get("provider", "fastf1"), year=manifest.get("year"),
                event=manifest.get("event"), session_type=manifest.get("session_type"), manifest_json=json.dumps(manifest, default=str)))
            for kind, driver, df in frames:
                if df is None or df.empty:
                    continue
                table, canon = _TABLES[kind]
                keys = {"session_id": sid, "driver": driver} if kind in ("telemetry", "position") else {"session_id": sid}
                n = write_frame(conn, table.__table__, df, keys, canon)
                conn.execute(HistFrame.__table__.insert().values(session_id=sid, kind=kind, driver=driver,
                                                                 columns_json=json.dumps(schema_of(df)), rows=n))
            # marked complete last, inside the same transaction
            conn.execute(HistSession.__table__.update().where(HistSession.session_id == sid).values(status=COMPLETE))
        return sid

    def delete_session(self, sid: str) -> bool:
        with immediate_transaction(self.engine) as conn:
            return conn.execute(delete(HistSession).where(HistSession.session_id == _check(sid))).rowcount > 0

    # ----------------------------------------------------------------- reads
    def exists(self, sid: str) -> bool:
        return self.manifest(sid) is not None

    def manifest(self, sid: str) -> dict | None:
        _check(sid)
        with self.engine.connect() as conn:
            row = conn.execute(select(HistSession.manifest_json).where(HistSession.session_id == sid, HistSession.status == COMPLETE)).scalar()
        return json.loads(row) if row else None

    def list_sessions(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(HistSession.manifest_json).where(HistSession.status == COMPLETE)).scalars().all()
        out = []
        for raw in rows:
            m = json.loads(raw)
            out.append({k: m.get(k) for k in ("session_id", "provider", "year", "event", "circuit", "session",
                                              "session_type", "session_date", "imported_at", "drivers", "channels")})
        return sorted(out, key=lambda m: (m.get("year") or 0, m.get("event") or ""), reverse=True)

    def frame_info(self, sid: str) -> list[dict]:
        """Stored frames of a session: kind, driver, columns, row count (for inspection tools)."""
        _check(sid)
        q = select(HistFrame.kind, HistFrame.driver, HistFrame.columns_json, HistFrame.rows).where(HistFrame.session_id == sid)
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return [{"kind": k, "driver": d or None, "columns": [c for c, _ in json.loads(cj)], "rows": n} for k, d, cj, n in rows]

    def _read(self, sid: str, kind: str, driver: str = "") -> pd.DataFrame:
        _check(sid)
        table, _ = _TABLES[kind]
        with self.engine.connect() as conn:
            complete = conn.execute(select(HistSession.status).where(HistSession.session_id == sid)).scalar() == COMPLETE
            if not complete:
                return pd.DataFrame()
            q = select(HistFrame.driver, HistFrame.columns_json).where(HistFrame.session_id == sid, HistFrame.kind == kind)
            frames = conn.execute(q).all()
            match = next(((d, cj) for d, cj in frames if (d or "").upper() == driver.upper() or slug(d).upper() == slug(driver).upper()), None) \
                if kind in ("telemetry", "position") else next(iter(frames), None)
            if match is None:
                return pd.DataFrame()
            stored_driver, cj = match
            where = [table.__table__.c.session_id == sid]
            if kind in ("telemetry", "position"):
                where.append(table.__table__.c.driver == stored_driver)
            return read_frame(conn, table.__table__, json.loads(cj), where)

    def laps(self, sid: str) -> pd.DataFrame:
        return self._read(sid, "laps")

    def weather(self, sid: str) -> pd.DataFrame:
        return self._read(sid, "weather")

    def telemetry(self, sid: str, driver: str) -> pd.DataFrame:
        return self._read(sid, "telemetry", driver)

    def position(self, sid: str, driver: str) -> pd.DataFrame:
        return self._read(sid, "position", driver)

    @staticmethod
    def records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
        """DataFrame -> JSON-safe records (NaN -> None), optionally capped to the last ``limit`` rows."""
        if df is None or df.empty:
            return []
        if limit is not None and len(df) > limit:
            df = df.tail(limit)
        # astype(object) first so nullable Int64/boolean NA values become None (JSON-safe),
        # not pd.NA which is not serializable.
        obj = df.astype(object)
        return obj.where(pd.notna(obj), None).to_dict(orient="records")
