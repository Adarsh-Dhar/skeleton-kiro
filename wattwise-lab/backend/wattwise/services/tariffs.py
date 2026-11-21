"""Tariff library in the root database (``ww_tariff``). The built-in example tariffs are seeded into an empty library.

The example tariffs are illustrative only; they are not any real utility's prices.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, func, select, update

from db.engine import immediate_transaction
from db.models.wattwise import Tariff
from wattwise.adapters.household_model import DEFAULT_TARIFFS, validate_tariff
from wattwise.adapters.tariff_model import EXTENSION_KEYS


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
        periods.append(q)
    rec["periods"] = periods
    for k in EXTENSION_KEYS:
        if data.get(k) is not None:
            rec[k] = data[k]
    return rec


class TariffStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._seed()

    def _seed(self) -> None:
        with immediate_transaction(self.engine) as c:
            if c.execute(select(func.count()).select_from(Tariff)).scalar():
                return
            now = datetime.now(UTC)
            for i, t in enumerate(DEFAULT_TARIFFS):
                c.execute(Tariff.__table__.insert().values(tariff_id=t["id"], name=t["name"], position=i, data_json=json.dumps(t),
                                                           created_at=now, updated_at=now))

    def list(self) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [json.loads(r) for r in c.execute(select(Tariff.data_json).order_by(Tariff.position, Tariff.created_at)).scalars()]

    def get(self, tariff_id: str) -> dict[str, Any]:
        with self.engine.connect() as c:
            row = c.execute(select(Tariff.data_json).where(Tariff.tariff_id == tariff_id)).scalar()
        if row is None:
            raise KeyError(tariff_id)
        return json.loads(row)

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
            return rec

    def delete(self, tariff_id: str) -> None:
        with immediate_transaction(self.engine) as c:
            n = c.execute(select(func.count()).select_from(Tariff)).scalar()
            exists = c.execute(select(Tariff.tariff_id).where(Tariff.tariff_id == tariff_id)).scalar()
            if n <= 1:
                raise TariffError(["the last tariff cannot be deleted"])
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
