"""WATTWISE LAB tables (prefix ``ww_``)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, utcnow


class Household(Base):
    """The household configuration: id=1 is the configuration, id=2 the backup taken when it is cleared. Validated JSON
    (see wattwise.services.household)."""

    __tablename__ = "ww_household"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settings_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40), default="user")  # user | migrated-localstorage | migrated-legacy
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tariff(Base):
    """Tariff library (replaces data/tariffs.json). ``data_json`` is the whole tariff dict (see tariff_model)."""

    __tablename__ = "ww_tariff"
    tariff_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)
    data_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeterImport(Base):
    """One imported historical meter file. Separate from experiment-run measurements: these are measured household
    usage, never an outcome of a schedule the household did not actually run."""

    __tablename__ = "ww_meter_import"
    import_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    file_name: Mapped[str] = mapped_column(String(300))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(120), default="csv upload")
    timezone: Mapped[str] = mapped_column(String(60))
    reading_kind: Mapped[str] = mapped_column(String(20))  # power | interval_energy | cumulative
    unit: Mapped[str] = mapped_column(String(10))  # unit of the source column (kW, W, kWh, Wh)
    interval_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    resample_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_ts_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_ts_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_used: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    options_json: Mapped[str] = mapped_column(Text)  # column mapping, timezone, kind, unit, resampling as requested
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    row_log_json: Mapped[str] = mapped_column(Text, default="[]")  # every skipped/transformed source row (capped)
    gaps_json: Mapped[str] = mapped_column(Text, default="[]")
    provenance: Mapped[str] = mapped_column(String(20), default="unknown", server_default="unknown")  # real_meter | synthetic_demo | unknown
    total_import_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_export_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)


class MeterReading(Base):
    """Normalised readings of one import: interval start (UTC), the original timestamp text, average kW and kWh."""

    __tablename__ = "ww_meter_reading"
    __table_args__ = (Index("ix_ww_meter_reading_import_ts", "import_id", "ts_utc"),)
    import_id: Mapped[str] = mapped_column(String(40), ForeignKey("ww_meter_import.import_id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_utc: Mapped[str] = mapped_column(String(40))  # ISO-8601 UTC, interval start
    original_ts: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_minutes: Mapped[float] = mapped_column(Float)
    power_kw: Mapped[float] = mapped_column(Float)
    energy_kwh: Mapped[float] = mapped_column(Float)
    flags: Mapped[str | None] = mapped_column(String(200), nullable=True)
    export_kwh: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    export_kw: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
