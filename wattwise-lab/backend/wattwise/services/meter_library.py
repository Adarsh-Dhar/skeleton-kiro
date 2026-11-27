"""Historical meter-data library (``ww_meter_import`` / ``ww_meter_reading``).

Imports here are the household's MEASURED historical usage. They are kept apart from experiment-run measurements:
a historical reading is never the outcome of an alternative schedule the household did not actually use.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, select

from db.engine import immediate_transaction
from db.models.wattwise import MeterImport, MeterReading
from wattwise.adapters import provenance as PV
from wattwise.adapters.meter_import import ImportOptions, ImportResult, Reading, interpret

PREVIEW_ROWS = 50
PROVENANCE = "historical_measured"  # fixed quantity-kind key (clarification C1); the file's label is reported as ``source_label``
PROVENANCE_LABELS = ("real_meter", "synthetic_demo", "unknown")  # every label a stored import may carry
IMPORT_LABELS = ("real_meter", "unknown")  # labels a NEW import may claim: synthetic data is not accepted (zero fabricated input data)
# A meter export is measured by the meter but supplied by the household: it is never shown as independently verified.
_BADGES = {"real_meter": ("MEASURED · USER SUPPLIED", "prov-m"), "synthetic_demo": ("SYNTHETIC · NOT USABLE", "prov-s"),
           "unknown": ("UNVERIFIED SOURCE", "prov-u")}


class ProvenanceError(ValueError):
    """The supplied provenance label is not one of PROVENANCE_LABELS."""
def _source_record(source: str, label: str, record: dict[str, Any] | None, legacy: bool = False) -> dict[str, Any]:
    """The import's source record. A meter export is the household's own data: at most ``user_supplied``, never ``verified``."""
    if record:
        rec = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in record.items()}
        if label == "unknown":
            rec.update(source_type="unverified", verification="unverified")
        elif rec.get("verification") == "verified":
            rec["verification"] = "user_supplied"
        return rec
    if label == "synthetic_demo":
        return {**PV.example_source("synthetic demo meter file"), "source_name": source}
    if label == "real_meter":
        return {"source_type": "meter_export", "source_name": source, "document": None, "collected_on": None, "effective_from": None,
                "effective_to": None, "verification": "user_supplied", "legacy": legacy, "notes": "meter export supplied by the household"}
    return {"source_type": "unverified", "source_name": source, "document": None, "collected_on": None, "effective_from": None,
            "effective_to": None, "verification": "unverified", "legacy": legacy, "notes": "origin of the file not stated"}


def normalize_provenance(value: Any) -> str:
    """None / empty / whitespace -> 'unknown'; an allowed label -> itself; anything else raises ProvenanceError."""
    if value is None:
        return "unknown"
    if not isinstance(value, str):
        raise ProvenanceError(f"provenance must be one of {', '.join(PROVENANCE_LABELS)} (got {value!r})")
    v = value.strip()
    if not v:
        return "unknown"
    if v not in PROVENANCE_LABELS:
        raise ProvenanceError(f"provenance must be one of {', '.join(PROVENANCE_LABELS)} (got '{v}')")
    return v


def import_label(value: Any) -> str:
    """The label of a new import: like ``normalize_provenance`` but synthetic data is rejected."""
    v = normalize_provenance(value)
    if v not in IMPORT_LABELS:
        raise ProvenanceError("synthetic or demo meter data cannot be imported: WATTWISE accepts only real meter exports "
                              "(provenance 'real_meter') or files of unknown origin ('unknown', labelled unverified)")
    return v
def usable_as_baseline(meta: dict[str, Any]) -> None:
    """Synthetic imports stored by earlier versions cannot be the household's background load."""
    if meta.get("source_label") == "synthetic_demo":
        from wattwise.services.baseline import BaselineUnavailable
        raise BaselineUnavailable(f"meter import {meta.get('import_id')} is synthetic demo data, not real readings; import your own meter export")
def provenance_badge(label: str | None) -> dict[str, str]:
    text, css = _BADGES[label if label in _BADGES else "unknown"]
    return {"text": text, "css": css}


def options_from(d: dict[str, Any] | None) -> ImportOptions:
    allowed = set(ImportOptions.__dataclass_fields__)
    unknown = set(d or {}) - allowed
    if unknown:
        raise ValueError(f"unknown import option(s): {sorted(unknown)}; allowed: {sorted(allowed)}")
    return ImportOptions(**(d or {}))


def _reading_view(r: Reading) -> dict[str, Any]:
    return {"start_utc": r.start_utc.isoformat(), "interval_minutes": r.interval_minutes, "kwh": round(r.kwh, 6), "kw": round(r.kw, 6),
            "export_kwh": round(r.export_kwh, 6), "export_kw": round(r.export_kw, 6), "original_ts": r.original_ts, "source_row": r.source_row, "flags": r.flags}


def preview(text: str, opts: dict[str, Any] | None) -> dict[str, Any]:
    """Exactly how the file would be interpreted. Nothing is stored."""
    res = interpret(text, options_from(opts))
    return {"columns": res.columns, "interpretation": res.options["resolved"], "options": {k: v for k, v in res.options.items() if k != "resolved"},
            "summary": res.summary(), "warnings": res.warnings, "gaps": res.gaps, "row_log": res.row_log,
            "readings_preview": [_reading_view(r) for r in res.readings[:PREVIEW_ROWS]], "provenance": PROVENANCE, "stored": False}  # fmt: skip


class MeterLibrary:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def import_csv(self, text: str, file_name: str, opts: dict[str, Any] | None, source: str = "csv upload", force: bool = False,
                   provenance: str | None = None, source_record: dict[str, Any] | None = None) -> dict[str, Any]:
        label = import_label(provenance)  # validate before anything is written
        opt = options_from(opts)  # export_column / signed travel in the options
        res: ImportResult = interpret(text, opt)
        if not res.readings:
            raise ValueError("no readings could be imported (see the preview for the reasons)")
        digest = hashlib.sha256(text.encode() + json.dumps(asdict(opt), sort_keys=True).encode()).hexdigest()
        iid = "mi-" + uuid.uuid4().hex[:10]
        with immediate_transaction(self.engine) as c:
            dup = c.execute(select(MeterImport.import_id).where(MeterImport.content_sha256 == digest)).scalar()
            if dup and not force:
                from wattwise.services.lab import Conflict

                raise Conflict(f"this file was already imported with the same options as {dup}")
            first, last = res.readings[0].start_utc.isoformat(), res.readings[-1].start_utc.isoformat()
            c.execute(MeterImport.__table__.insert().values(
                import_id=iid, file_name=file_name[:300], imported_at=datetime.now(UTC), source=source[:120], timezone=opt.timezone,
                reading_kind=res.options["resolved"]["reading_kind"], unit=res.options["resolved"]["unit"], interval_minutes=res.interval_minutes,
                resample_minutes=opt.resample_minutes, first_ts_utc=first, last_ts_utc=last, rows_total=res.rows_total, rows_used=len(res.readings),
                rows_skipped=res.rows_skipped, content_sha256=digest,
                options_json=json.dumps({**res.options, "source_record": _source_record(source, label, source_record)}), warnings_json=json.dumps(res.warnings),
                row_log_json=json.dumps(res.row_log), gaps_json=json.dumps(res.gaps), provenance=label,
                total_import_kwh=sum(r.kwh for r in res.readings), total_export_kwh=sum(r.export_kwh for r in res.readings)))
            c.exec_driver_sql(
                "INSERT INTO ww_meter_reading (import_id, seq, ts_utc, original_ts, source_row, interval_minutes, power_kw, energy_kwh, flags, export_kwh, export_kw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(iid, i, r.start_utc.isoformat(), r.original_ts, r.source_row, r.interval_minutes, r.kw, r.kwh, ",".join(r.flags) or None,
                  r.export_kwh, r.export_kw)
                 for i, r in enumerate(res.readings)])
        return self.get(iid)

    @staticmethod
    def _view(m: MeterImport, full: bool = False) -> dict[str, Any]:
        opts = json.loads(m.options_json)
        record = opts.pop("source_record", None) or _source_record(m.source, m.provenance or "unknown", None, legacy=True)
        out = {"import_id": m.import_id, "file_name": m.file_name, "imported_at": str(m.imported_at), "source": m.source, "timezone": m.timezone,
               "reading_kind": m.reading_kind, "unit": m.unit, "interval_minutes": m.interval_minutes, "resample_minutes": m.resample_minutes,
               "first_utc": m.first_ts_utc, "last_utc": m.last_ts_utc, "rows_total": m.rows_total, "rows_used": m.rows_used,
               "rows_skipped": m.rows_skipped, "warnings": json.loads(m.warnings_json), "provenance": PROVENANCE,
               "source_label": m.provenance or "unknown", "total_import_kwh": m.total_import_kwh, "total_export_kwh": m.total_export_kwh or 0.0,
               "source_record": record, "source_status": PV.status(record)}  # fmt: skip
        if full:
            out.update(options=opts, row_log=json.loads(m.row_log_json), gaps=json.loads(m.gaps_json))
        return out

    def list(self) -> list[dict[str, Any]]:
        from sqlalchemy.orm import Session

        with Session(self.engine) as s:
            return [self._view(m) for m in s.scalars(select(MeterImport).order_by(MeterImport.imported_at.desc()))]

    def get(self, import_id: str) -> dict[str, Any]:
        from sqlalchemy.orm import Session

        with Session(self.engine) as s:
            m = s.get(MeterImport, import_id)
            if m is None:
                from wattwise.services.lab import NotFound

                raise NotFound(f"meter import {import_id}")
            return self._view(m, full=True)

    def readings(self, import_id: str, offset: int = 0, limit: int | None = None) -> list[Reading]:
        self.get(import_id)
        q = (select(MeterReading.ts_utc, MeterReading.interval_minutes, MeterReading.energy_kwh, MeterReading.power_kw,
                    MeterReading.original_ts, MeterReading.source_row, MeterReading.flags, MeterReading.export_kwh, MeterReading.export_kw)
             .where(MeterReading.import_id == import_id).order_by(MeterReading.seq).offset(offset))
        if limit is not None:
            q = q.limit(limit)
        with self.engine.connect() as c:
            return [Reading(datetime.fromisoformat(t), m, kwh, kw, ots or "", row or 0, (fl or "").split(",") if fl else [], ekwh or 0.0, ekw or 0.0)
                    for t, m, kwh, kw, ots, row, fl, ekwh, ekw in c.execute(q)]

    def readings_view(self, import_id: str, offset: int = 0, limit: int = 5000) -> dict[str, Any]:
        rows = self.readings(import_id, offset, limit)
        meta = self.get(import_id)
        return {"import_id": import_id, "offset": offset, "count": len(rows), "total": meta["rows_used"], "provenance": PROVENANCE,
                "note": "measured readings exactly as stored (not downsampled)", "readings": [_reading_view(r) for r in rows]}

    def delete(self, import_id: str) -> dict[str, Any]:
        self.get(import_id)
        with immediate_transaction(self.engine) as c:
            c.execute(delete(MeterImport).where(MeterImport.import_id == import_id))
        return {"deleted": import_id}
