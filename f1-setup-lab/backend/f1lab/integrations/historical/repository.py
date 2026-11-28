"""Database repository for normalized historical sessions (root database, ``f1_hist_*`` tables).

A session is written in ONE transaction: its rows, its per-frame column lists and finally ``status='complete'``.
A failed or interrupted import therefore leaves nothing behind, and only complete sessions are listed or opened.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
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
    CsvUpload,
    HistColumnMapping,
    HistDataset,
    HistFrame,
    HistLap,
    HistPosition,
    HistSession,
    HistTelemetry,
    HistWeather,
)
from f1lab.integrations.historical.importer import INVALID_REQUEST, HistoricalImportError

_SAFE = re.compile(r"[^a-z0-9]+")
COMPLETE = "complete"
REQUIRED_MANIFEST_FIELDS = ("provider", "source_type", "verification")
_TABLES = {"laps": (HistLap, LAP_COLUMNS), "weather": (HistWeather, WEATHER_COLUMNS),
           "telemetry": (HistTelemetry, TELEMETRY_COLUMNS), "position": (HistPosition, POSITION_COLUMNS)}  # fmt: skip


def slug(text: str) -> str:
    return _SAFE.sub("-", str(text).lower()).strip("-") or "x"


def session_id(year: int, event: str, session_type: str, provider: str = "openf1") -> str:
    """Deterministic, path-safe session id; the provider is part of it so two providers never overwrite each other."""
    return f"{slug(provider)}-{year}-{slug(event)}-{slug(session_type)}"


def _check(sid: str) -> str:
    if not isinstance(sid, str) or "/" in sid or ".." in sid or sid.startswith(".") or not sid:
        raise ValueError(f"unsafe session id: {sid!r}")
    return sid


class HistoricalRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ----------------------------------------------------------------- writes
    def save_session(self, sid: str, manifest: dict, laps: pd.DataFrame, weather: pd.DataFrame,
                     telemetry: dict[str, pd.DataFrame], position: dict[str, pd.DataFrame],
                     datasets: dict[str, tuple[list[dict], str | None]] | None = None, *,
                     column_mapping: dict | None = None, replace_sid: str | None = None) -> str:
        """Replace session ``sid`` atomically. Returns the session id.

        ``datasets`` maps a name to ``(rows, fetched_at)``; empty datasets are not stored. The manifest's ``file_hash`` and
        ``metadata`` (Session_Metadata with its Unavailable_Markers) are stored on the session row. ``column_mapping``
        (``mapping``, ``units``, ``duplicate_policy``, ``order_policy``) is stored with the session in the same transaction, and
        ``replace_sid`` (another session being replaced) is deleted in that transaction too."""
        _check(sid)
        for field in REQUIRED_MANIFEST_FIELDS:
            if not manifest.get(field):
                raise HistoricalImportError(INVALID_REQUEST, f"cannot store session {sid}: manifest is missing required field '{field}'")
        frames: list[tuple[str, str, pd.DataFrame]] = [("laps", "", laps), ("weather", "", weather)]
        frames += [("telemetry", str(code), df) for code, df in telemetry.items()]
        frames += [("position", str(code), df) for code, df in position.items()]
        with immediate_transaction(self.engine) as conn:
            if replace_sid and replace_sid != sid:
                conn.execute(delete(HistSession).where(HistSession.session_id == _check(replace_sid)))
            conn.execute(delete(HistSession).where(HistSession.session_id == sid))  # cascades to frames and rows
            conn.execute(HistSession.__table__.insert().values(
                session_id=sid, status="importing", provider=manifest["provider"],
                source_type=manifest["source_type"], verification=manifest["verification"], year=manifest.get("year"),
                event=manifest.get("event"), session_type=manifest.get("session_type"), manifest_json=json.dumps(manifest, default=str),
                metadata_json=json.dumps(manifest["metadata"], default=str) if manifest.get("metadata") is not None else None,
                file_hash=manifest.get("file_hash")))
            if column_mapping is not None:
                conn.execute(HistColumnMapping.__table__.insert().values(
                    session_id=sid, mapping_json=json.dumps(column_mapping.get("mapping") or {}),
                    units_json=json.dumps(column_mapping.get("units") or {}),
                    duplicate_policy=str(column_mapping.get("duplicate_policy") or "reject"),
                    order_policy=str(column_mapping.get("order_policy") or "reject")))
            for kind, driver, df in frames:
                if df is None or df.empty:
                    continue
                table, canon = _TABLES[kind]
                keys = {"session_id": sid, "driver": driver} if kind in ("telemetry", "position") else {"session_id": sid}
                n = write_frame(conn, table.__table__, df, keys, canon)
                conn.execute(HistFrame.__table__.insert().values(session_id=sid, kind=kind, driver=driver,
                                                                 columns_json=json.dumps(schema_of(df)), rows=n))
            for name, (rows, fetched_at) in (datasets or {}).items():
                if rows:
                    conn.execute(HistDataset.__table__.insert().values(session_id=sid, name=name, rows_json=json.dumps(rows, default=str),
                                                                      rows=len(rows), fetched_at=fetched_at))
            # marked complete last, inside the same transaction
            conn.execute(HistSession.__table__.update().where(HistSession.session_id == sid).values(status=COMPLETE))
        return sid

    # ------------------------------------------------------------- CSV uploads and duplicate detection
    def upload(self, file_hash: str) -> dict | None:
        """The staged-upload record for a file hash, or None."""
        with self.engine.connect() as conn:
            row = conn.execute(select(CsvUpload.original_filename, CsvUpload.size_bytes).where(CsvUpload.file_hash == file_hash)).first()
        return {"file_hash": file_hash, "original_filename": row[0], "size_bytes": row[1]} if row else None

    def record_upload(self, file_hash: str, original_filename: str, size_bytes: int) -> dict:
        """Register (or refresh the filename of) a staged upload. Creates no session and no job."""
        name = (original_filename or "upload.csv")[:255]
        with immediate_transaction(self.engine) as conn:
            changed = conn.execute(CsvUpload.__table__.update().where(CsvUpload.file_hash == file_hash)
                                   .values(original_filename=name, size_bytes=size_bytes)).rowcount
            if not changed:
                conn.execute(CsvUpload.__table__.insert().values(file_hash=file_hash, original_filename=name, size_bytes=size_bytes,
                                                                 uploaded_at=datetime.now(UTC)))
        return {"file_hash": file_hash, "original_filename": name, "size_bytes": size_bytes}

    def find_by_file_hash(self, file_hash: str) -> str | None:
        """Id of a complete stored session imported from a file with this SHA-256, or None."""
        with self.engine.connect() as conn:
            return conn.execute(select(HistSession.session_id).where(HistSession.file_hash == file_hash, HistSession.status == COMPLETE)
                                .order_by(HistSession.session_id)).scalars().first()

    def find_by_key(self, key: dict) -> str | None:
        """Id of a complete CSV session whose Session_Metadata matches provider, event, session_type, driver and year."""
        names = ("provider", "event", "session_type", "driver", "year")
        want = {n: key.get(n) for n in names}
        with self.engine.connect() as conn:
            rows = conn.execute(select(HistSession.session_id, HistSession.metadata_json)
                                .where(HistSession.provider == "csv_upload", HistSession.status == COMPLETE)
                                .order_by(HistSession.session_id)).all()
        for sid, raw in rows:
            meta = json.loads(raw) if raw else {}
            if all(meta.get(n) == want[n] for n in names):
                return sid
        return None

    def delete_session(self, sid: str) -> bool:
        """Remove a session and every dependent row (frames, laps, telemetry, position, weather, datasets, column mapping,
        all ``ON DELETE CASCADE`` from ``f1_hist_session``) in ONE transaction; a failure rolls everything back (Req 10.7, 10.8)."""
        with immediate_transaction(self.engine) as conn:
            return conn.execute(delete(HistSession).where(HistSession.session_id == _check(sid))).rowcount > 0

    def purge_incomplete(self) -> int:
        """Delete sessions left in a non-complete status (never listed or readable); returns how many (Req 8.7)."""
        with immediate_transaction(self.engine) as conn:
            return conn.execute(delete(HistSession).where(HistSession.status != COMPLETE)).rowcount

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

    def list_manifests(self) -> list[dict]:
        """Full manifests of every complete session, newest season first (same order as ``list_sessions``)."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(HistSession.manifest_json).where(HistSession.status == COMPLETE)).scalars().all()
        return sorted((json.loads(r) for r in rows), key=lambda m: (m.get("year") or 0, m.get("event") or ""), reverse=True)

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

    def dataset(self, sid: str, name: str) -> list[dict]:
        """Rows of a stored dataset ([] when the session or dataset is not stored)."""
        _check(sid)
        with self.engine.connect() as conn:
            if conn.execute(select(HistSession.status).where(HistSession.session_id == sid)).scalar() != COMPLETE:
                return []
            raw = conn.execute(select(HistDataset.rows_json).where(HistDataset.session_id == sid, HistDataset.name == name)).scalar()
        return json.loads(raw) if raw else []

    def dataset_info(self, sid: str) -> dict[str, dict]:
        _check(sid)
        with self.engine.connect() as conn:
            rows = conn.execute(select(HistDataset.name, HistDataset.rows, HistDataset.fetched_at).where(HistDataset.session_id == sid)).all()
        return {n: {"rows": r, "fetched_at": f} for n, r, f in rows}

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
