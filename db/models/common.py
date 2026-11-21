"""Tables shared by both applications."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, utcnow

APP_F1 = "f1-setup-lab"
APP_WATTWISE = "wattwise-lab"
APPS = (APP_F1, APP_WATTWISE)


class ExperimentOwner(Base):
    """Which application owns an SDK experiment. Both apps share the SDK ``experiments`` table; this scopes it."""

    __tablename__ = "app_experiment_owner"
    experiment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    app: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
