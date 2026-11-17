"""Tariff library persisted as a small JSON file (application data, separate from experiment storage)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from wattwise.adapters.household_model import DEFAULT_TARIFFS, validate_tariff


class TariffError(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


class TariffStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write([dict(t) for t in DEFAULT_TARIFFS])

    def _read(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text())

    def _write(self, tariffs: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(tariffs, f, indent=2)
        os.replace(tmp, self.path)  # atomic: never leaves a half-written library

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read()

    def get(self, tariff_id: str) -> dict[str, Any]:
        for t in self.list():
            if t["id"] == tariff_id:
                return t
        raise KeyError(tariff_id)

    @staticmethod
    def _slug(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "tariff"

    def save(self, data: dict[str, Any], tariff_id: str | None = None) -> dict[str, Any]:
        issues = validate_tariff(data)
        if issues:
            raise TariffError(issues)
        with self._lock:
            tariffs = self._read()
            new_id = tariff_id or self._slug(data["name"])
            if tariff_id is None and any(t["id"] == new_id for t in tariffs):
                raise TariffError([f"a tariff with id '{new_id}' already exists; choose another name"])
            if tariff_id is not None and not any(t["id"] == tariff_id for t in tariffs):
                raise KeyError(tariff_id)
            if any(t["name"].lower() == data["name"].strip().lower() and t["id"] != new_id for t in tariffs):
                raise TariffError([f"a tariff named '{data['name']}' already exists"])
            rec = {"id": new_id, "name": data["name"].strip(), "demand_charge_per_kw": float(data.get("demand_charge_per_kw", 0) or 0),
                   "periods": [{"label": p.get("label", ""), "start": float(p["start"]), "end": float(p["end"]), "price": float(p["price"])} for p in data["periods"]]}  # fmt: skip
            tariffs = [rec if t["id"] == new_id else t for t in tariffs] if tariff_id else [*tariffs, rec]
            self._write(tariffs)
            return rec

    def delete(self, tariff_id: str) -> None:
        with self._lock:
            tariffs = self._read()
            if len(tariffs) <= 1:
                raise TariffError(["the last tariff cannot be deleted"])
            if not any(t["id"] == tariff_id for t in tariffs):
                raise KeyError(tariff_id)
            self._write([t for t in tariffs if t["id"] != tariff_id])
