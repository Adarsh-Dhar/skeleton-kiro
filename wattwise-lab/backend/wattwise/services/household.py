"""Household settings in the root database (``ww_household``, one row). Replaces the browser's localStorage copy."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, select

from db.engine import immediate_transaction
from db.models.wattwise import Household
from wattwise.api.schemas import HouseholdSettings

ROW_ID = 1


class HouseholdStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self) -> dict[str, Any]:
        """{settings, saved, source, updated_at}. Unsaved households return the defaults with ``saved: False``."""
        with self.engine.connect() as c:
            row = c.execute(select(Household.settings_json, Household.source, Household.updated_at).where(Household.id == ROW_ID)).first()
        if row is None:
            return {"settings": HouseholdSettings().model_dump(), "saved": False, "source": "defaults", "updated_at": None}
        settings = HouseholdSettings.model_validate_json(row[0]).model_dump()
        return {"settings": settings, "saved": True, "source": row[1], "updated_at": str(row[2])}

    def put(self, settings: HouseholdSettings, source: str = "user") -> dict[str, Any]:
        body = json.dumps(settings.model_dump())
        with immediate_transaction(self.engine) as c:
            c.execute(delete(Household).where(Household.id == ROW_ID))
            c.execute(Household.__table__.insert().values(id=ROW_ID, settings_json=body, source=source, updated_at=datetime.now(UTC)))
        return self.get()

    def migrate_once(self, settings: HouseholdSettings, source: str) -> tuple[dict[str, Any], bool]:
        """Store legacy settings only if nothing was saved yet. Returns (state, applied)."""
        with immediate_transaction(self.engine) as c:
            exists = c.execute(select(Household.id).where(Household.id == ROW_ID)).scalar()
            if exists is None:
                c.execute(Household.__table__.insert().values(id=ROW_ID, settings_json=json.dumps(settings.model_dump()), source=source,
                                                              updated_at=datetime.now(UTC)))
        return self.get(), exists is None

    def reset(self) -> dict[str, Any]:
        with immediate_transaction(self.engine) as c:
            c.execute(delete(Household).where(Household.id == ROW_ID))
        return self.get()
