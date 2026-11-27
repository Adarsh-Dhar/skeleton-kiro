"""Household settings in the root database (``ww_household``). Row 1 is the configuration; row 2 is the backup taken when it is cleared.

Zero fabricated input data: an unconfigured household is EMPTY (no example tariff, EV or appliances). Settings saved before source
tracking existed are kept: their values are labelled as the owner's (``legacy``) so they can be confirmed. A legacy EV that lacks a
value this version needs (for example the usual plug-in time) is not completed with an invented value: it is returned as
``ev_draft`` and the household has no usable EV until the owner fills in the gap.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Engine, delete, select

from db.engine import immediate_transaction
from db.models.wattwise import Household
from wattwise.adapters import provenance as PV
from wattwise.api.schemas import HouseholdSettings, validate_charger_capacity

ROW_ID = 1
BACKUP_ROW_ID = 2
def _upgrade_legacy(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    """Stored settings -> (settings that validate now, notices, ev_draft). Adds legacy source labels; never invents a value."""
    raw = json.loads(json.dumps(raw))
    notices: list[str] = []
    for a in raw.get("appliances") or []:
        if not a.get("source"):
            a["source"] = PV.legacy_source(f"appliance '{a.get('name', '')}'")
    if raw.get("base_load_kw") is not None and not raw.get("base_load_source"):
        raw["base_load_source"] = PV.legacy_source("always-on base load")
    if raw.get("charger_power_kw") is not None and not raw.get("charger_source"):
        raw["charger_source"] = PV.legacy_source("charger capacity")
    draft = None
    ev = raw.get("ev")
    if isinstance(ev, dict):
        if not ev.get("source"):
            ev["source"] = PV.legacy_source("EV specification")
        need = [k for k in ("battery_kwh", "efficiency", "initial_soc_pct", "desired_charge_pct", "departure_hour", "plug_in_hour") if ev.get(k) is None]
        if need:
            draft = {**ev, "missing": need}
            raw["ev"] = None
            notices.append(f"the saved EV is incomplete ({', '.join(need)} not set); it is kept as a draft and not used until you complete it")
    return raw, notices, draft
class HouseholdStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
    def _row(self, row_id: int):
        with self.engine.connect() as c:
            return c.execute(select(Household.settings_json, Household.source, Household.updated_at).where(Household.id == row_id)).first()
    def get(self) -> dict[str, Any]:
        """{settings, saved, source, updated_at, backup}. An unconfigured household returns EMPTY settings with ``saved: False``."""
        row = self._row(ROW_ID)
        backup = self._row(BACKUP_ROW_ID)
        backup_info = {"available": backup is not None, "updated_at": str(backup[2]) if backup else None}
        if row is None:
            return {"settings": HouseholdSettings().model_dump(mode="json"), "saved": False, "source": "empty", "updated_at": None,
                    "backup": backup_info}
        raw, notices, draft = _upgrade_legacy(json.loads(row[0]))
        try:
            validate_charger_capacity(raw.get("charger_power_kw"))
        except ValueError as exc:  # stored value is out of range: treat as unset instead of failing every read
            raw["charger_power_kw"] = None
            raw["charger_source"] = None
            notices.append(f"stored charger capacity ignored (treated as not set): {exc}")
        try:
            settings = HouseholdSettings.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:  # keep what is valid; report the rest instead of failing every read
            notices.append(f"part of the saved household could not be read and was left out: {exc.errors()[0]['msg']}")
            keep = {k: raw.get(k) for k in ("tariff_id", "baseline_import_id")}
            settings = HouseholdSettings.model_validate(keep).model_dump(mode="json")
        out = {"settings": settings, "saved": True, "source": row[1], "updated_at": str(row[2]), "backup": backup_info}
        if draft:
            out["ev_draft"] = draft
        if notices:
            out["notice"] = "; ".join(notices)
        return out
    def put(self, settings: HouseholdSettings, source: str = "user") -> dict[str, Any]:
        body = json.dumps(settings.model_dump(mode="json"))
        with immediate_transaction(self.engine) as c:
            c.execute(delete(Household).where(Household.id == ROW_ID))
            c.execute(Household.__table__.insert().values(id=ROW_ID, settings_json=body, source=source, updated_at=datetime.now(UTC)))
        return self.get()
    def migrate_once(self, settings: HouseholdSettings, source: str) -> tuple[dict[str, Any], bool]:
        """Store legacy settings only if nothing was saved yet. Returns (state, applied)."""
        with immediate_transaction(self.engine) as c:
            exists = c.execute(select(Household.id).where(Household.id == ROW_ID)).scalar()
            if exists is None:
                c.execute(Household.__table__.insert().values(id=ROW_ID, settings_json=json.dumps(settings.model_dump(mode="json")), source=source,
                                                              updated_at=datetime.now(UTC)))
        return self.get(), exists is None
    def reset(self) -> dict[str, Any]:
        """Clear the configuration. The current settings become the backup (replacing an older backup); nothing is filled in."""
        with immediate_transaction(self.engine) as c:
            cur = c.execute(select(Household.settings_json, Household.source).where(Household.id == ROW_ID)).first()
            if cur is not None:
                c.execute(delete(Household).where(Household.id == BACKUP_ROW_ID))
                c.execute(Household.__table__.insert().values(id=BACKUP_ROW_ID, settings_json=cur[0], source=cur[1], updated_at=datetime.now(UTC)))
            c.execute(delete(Household).where(Household.id == ROW_ID))
        return self.get()
    def restore(self) -> dict[str, Any]:
        """Bring back the backup taken by the last clear. Raises KeyError when there is none."""
        with immediate_transaction(self.engine) as c:
            bak = c.execute(select(Household.settings_json, Household.source).where(Household.id == BACKUP_ROW_ID)).first()
            if bak is None:
                raise KeyError("household backup")
            c.execute(delete(Household).where(Household.id == ROW_ID))
            c.execute(Household.__table__.insert().values(id=ROW_ID, settings_json=bak[0], source=bak[1], updated_at=datetime.now(UTC)))
        return self.get()
