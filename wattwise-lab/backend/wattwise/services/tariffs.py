"""Tariff library in the root database (``ww_tariff``). Empty until the household adds a real tariff (zero fabricated input data).
Every tariff carries a ``source`` (bill or tariff order). Earlier versions seeded three example tariffs; any of those still stored
is recognised by its content and labelled as an example, which blocks it from every calculation until it is replaced or deleted.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, func, select, update

from db.engine import immediate_transaction
from db.models.wattwise import Tariff
from wattwise.adapters import provenance as PV
from wattwise.adapters.household_model import validate_tariff
from wattwise.adapters.tariff_model import EXTENSION_KEYS

# The example tariffs earlier versions seeded. Kept only to RECOGNISE them in an existing library; never inserted or used.
LEGACY_EXAMPLE_TARIFFS: dict[str, list[dict[str, Any]]] = {
    "time-of-use": [{"label": "Night", "start": 23, "end": 7, "price": 0.11}, {"label": "Day", "start": 7, "end": 16, "price": 0.22},
                    {"label": "Peak", "start": 16, "end": 21, "price": 0.36}, {"label": "Evening", "start": 21, "end": 23, "price": 0.22}],
    "flat-rate": [{"label": "All day", "start": 0, "end": 24, "price": 0.25}],
    "night-saver-demand": [{"label": "Night", "start": 0, "end": 6, "price": 0.09}, {"label": "Standard", "start": 6, "end": 24, "price": 0.28}],
}  # fmt: skip
SOURCE_KEYS = ("source", "utility", "consumer_category", "currency")
def _periods_key(periods: list[dict[str, Any]]) -> list[tuple]:
    return [(p.get("label", ""), float(p["start"]), float(p["end"]), float(p["price"])) for p in periods]
def with_source(rec: dict[str, Any]) -> dict[str, Any]:
    """A stored tariff with its ``source`` (legacy records get one) and the computed ``source_status``."""
    rec = dict(rec)
    if not rec.get("source"):
        example = LEGACY_EXAMPLE_TARIFFS.get(rec.get("id", ""))
        if example is not None and _periods_key(rec.get("periods", [])) == _periods_key(example):
            rec["source"] = PV.example_source(f"tariff '{rec.get('name', '')}'")
        elif example is not None:
            rec["source"] = {**PV.legacy_source(f"tariff '{rec.get('name', '')}'"), "source_type": "unverified", "verification": "unverified",
                             "notes": "edited from a built-in example before source tracking; add your bill or tariff order as the source"}
        else:
            rec["source"] = PV.legacy_source(f"tariff '{rec.get('name', '')}'")
    rec["source_status"] = PV.status(rec["source"])
    return rec
class TariffError(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


def normalise(data: dict[str, Any], tariff_id: str) -> dict[str, Any]:
    """The stored form of a tariff: original fields plus any optional extension that is set."""
    rec: dict[str, Any] = {"id": tariff_id, "name": str(data["name"]).strip(), "demand_charge_per_kw": float(data.get("demand_charge_per_kw", 0) or 0)}
    periods = []
    for p in data["periods"]:
        q: dict[str, Any] = {"label": p.get("label", "") or "", "start": float(p["start"]), "end": float(p["end"]), "price": float(p["price"])}
        if (p.get("days") or "all") != "all":
            q["days"] = p["days"]
        if p.get("months"):
            q["months"] = sorted(int(m) for m in p["months"])
        if p.get("weekdays"):
            q["weekdays"] = sorted({int(w) for w in p["weekdays"]})
        periods.append(q)
    rec["periods"] = periods
    for k in SOURCE_KEYS:
        if data.get(k) is not None:
            rec[k] = data[k].isoformat() if hasattr(data[k], "isoformat") else data[k]
    if isinstance(rec.get("source"), dict):
        rec["source"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rec["source"].items()}
    for k in EXTENSION_KEYS:
        if data.get(k) is not None:
            rec[k] = data[k]
    return rec


class TariffStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine  # nothing is seeded: a fresh library is empty
    def list(self) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [with_source(json.loads(r)) for r in c.execute(select(Tariff.data_json).order_by(Tariff.position, Tariff.created_at)).scalars()]

    def get(self, tariff_id: str) -> dict[str, Any]:
        with self.engine.connect() as c:
            row = c.execute(select(Tariff.data_json).where(Tariff.tariff_id == tariff_id)).scalar()
        if row is None:
            raise KeyError(tariff_id)
        return with_source(json.loads(row))

    @staticmethod
    def _slug(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "tariff"

    def save(self, data: dict[str, Any], tariff_id: str | None = None) -> dict[str, Any]:
        issues = validate_tariff(data)
        if issues:
            raise TariffError(issues)
        with immediate_transaction(self.engine) as c:
            rows = {tid: name for tid, name in c.execute(select(Tariff.tariff_id, Tariff.name))}
            new_id = tariff_id or self._slug(data["name"])
            if tariff_id is None and new_id in rows:
                raise TariffError([f"a tariff with id '{new_id}' already exists; choose another name"])
            if tariff_id is not None and tariff_id not in rows:
                raise KeyError(tariff_id)
            if any(name.lower() == data["name"].strip().lower() and tid != new_id for tid, name in rows.items()):
                raise TariffError([f"a tariff named '{data['name']}' already exists"])
            rec = normalise(data, new_id)
            now = datetime.now(UTC)
            if tariff_id:
                c.execute(update(Tariff).where(Tariff.tariff_id == tariff_id).values(name=rec["name"], data_json=json.dumps(rec), updated_at=now))
            else:
                pos = (c.execute(select(func.max(Tariff.position))).scalar() or 0) + 1
                c.execute(Tariff.__table__.insert().values(tariff_id=new_id, name=rec["name"], position=pos, data_json=json.dumps(rec),
                                                           created_at=now, updated_at=now))
            return with_source(rec)
    def delete(self, tariff_id: str) -> None:
        with immediate_transaction(self.engine) as c:
            exists = c.execute(select(Tariff.tariff_id).where(Tariff.tariff_id == tariff_id)).scalar()
            if exists is None:
                raise KeyError(tariff_id)
            c.execute(delete(Tariff).where(Tariff.tariff_id == tariff_id))

    def import_records(self, records: list[dict[str, Any]], replace: bool = True) -> int:
        """Load tariffs exactly as given (used by the legacy tariffs.json migration). Returns how many were written."""
        now = datetime.now(UTC)
        with immediate_transaction(self.engine) as c:
            if replace:
                c.execute(delete(Tariff))
            for i, t in enumerate(records):
                c.execute(delete(Tariff).where(Tariff.tariff_id == t["id"]))
                c.execute(Tariff.__table__.insert().values(tariff_id=t["id"], name=t["name"], position=i, data_json=json.dumps(t),
                                                           created_at=now, updated_at=now))
        return len(records)
