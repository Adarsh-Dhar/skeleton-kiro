"""F1 SETUP LAB tables (prefix ``f1_``)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, utcnow

# ---------------------------------------------------------------------------- historical (OpenF1)
# Canonical columns written by f1lab.integrations.historical.normalizer. Columns a frame does not have are stored
# as NULL and the per-frame column list in f1_hist_frame records which columns really existed, so missing
# channels come back missing (never as fabricated zeros/NULL columns).
LAP_COLUMNS: dict[str, type] = {
    "driver": String, "driver_number": Integer, "team": String, "lap_number": Integer, "stint": Integer,
    "lap_time_s": Float, "sector_1_s": Float, "sector_2_s": Float, "sector_3_s": Float, "sector_4_s": Float,
    "compound": String, "tyre_life": Integer, "is_personal_best": Boolean, "is_deleted": Boolean, "is_accurate": Boolean,
    "deleted_reason": String, "is_valid": Boolean, "pits": Boolean,
}  # fmt: skip
WEATHER_COLUMNS: dict[str, type] = {
    "time_s": Float, "air_temp_c": Float, "track_temp_c": Float, "humidity_pct": Float, "wind_speed_ms": Float,
    "rainfall_mm": Float, "pressure_hpa": Float,
}  # fmt: skip
TELEMETRY_COLUMNS: dict[str, type] = {
    "lap_number": Integer, "distance_m": Float, "session_time_s": Float, "speed_kph": Float, "throttle_pct": Float,
    "brake_applied": Float, "brake_pct": Float, "steer_pct": Float, "gear": Float, "rpm": Float, "drs": Float,
}  # fmt: skip
POSITION_COLUMNS: dict[str, type] = {
    "lap_number": Integer, "distance_m": Float, "session_time_s": Float, "x_m": Float, "y_m": Float, "z_m": Float, "speed_kph": Float,
}  # fmt: skip


class HistSession(Base):
    """One imported historical session. ``status='complete'`` is written in the same transaction as all its rows."""

    __tablename__ = "f1_hist_session"
    session_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="openf1")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event: Mapped[str | None] = mapped_column(String(200), nullable=True)
    session_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HistFrame(Base):
    """Column list + pandas dtypes of each stored frame (laps, weather, telemetry/<driver>, position/<driver>)."""

    __tablename__ = "f1_hist_frame"
    session_id: Mapped[str] = mapped_column(String(200), ForeignKey("f1_hist_session.session_id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)  # laps | weather | telemetry | position
    driver: Mapped[str] = mapped_column(String(20), primary_key=True, default="")
    columns_json: Mapped[str] = mapped_column(Text)  # [[name, dtype], ...] in original order
    rows: Mapped[int] = mapped_column(Integer, default=0)


class HistDataset(Base):
    """A small per-session dataset that is not a frame (running order, results, grid, intervals, ...), stored as JSON rows with
    the time it was fetched. Deleted with its session."""

    __tablename__ = "f1_hist_dataset"
    session_id: Mapped[str] = mapped_column(String(200), ForeignKey("f1_hist_session.session_id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    rows_json: Mapped[str] = mapped_column(Text)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


def _frame_table(name: str, columns: dict[str, type], per_driver: bool) -> type:
    attrs: dict = {
        "__tablename__": name,
        "session_id": mapped_column(String(200), ForeignKey("f1_hist_session.session_id", ondelete="CASCADE"), primary_key=True),
        "row_idx": mapped_column(Integer, primary_key=True),
        "extra_json": mapped_column(Text, nullable=True),  # any non-canonical column, preserved verbatim
        "__annotations__": {},
    }
    if per_driver:
        attrs["driver"] = mapped_column(String(20), primary_key=True)
    for col, typ in columns.items():
        attrs[col] = mapped_column(col, typ(), nullable=True)
    if per_driver:
        attrs["__table_args__"] = (Index(f"ix_{name}_session_driver_lap", "session_id", "driver", "lap_number"),)
    return type(name, (Base,), attrs)


HistLap = _frame_table("f1_hist_lap", LAP_COLUMNS, per_driver=False)
HistWeather = _frame_table("f1_hist_weather", WEATHER_COLUMNS, per_driver=False)
HistTelemetry = _frame_table("f1_hist_telemetry", TELEMETRY_COLUMNS, per_driver=True)
HistPosition = _frame_table("f1_hist_position", POSITION_COLUMNS, per_driver=True)


class ImportJob(Base):
    """Durable OpenF1 import job. States: QUEUED DOWNLOADING PROCESSING SAVING
    COMPLETED FAILED CANCELLED. ``dedupe_key`` identifies the requested session + options."""

    __tablename__ = "f1_import_job"
    import_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    request_json: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(300), index=True)
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_of: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------- game UDP
class UdpRecording(Base):
    """One raw UDP capture session (replaces data/sessions/<sid>/raw.f1raw)."""

    __tablename__ = "f1_udp_recording"
    recording_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    host: Mapped[str | None] = mapped_column(String(100), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="live")  # live | probe | import
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class UdpPacket(Base):
    """Every datagram with its receive time, so a session can be re-decoded later."""

    __tablename__ = "f1_udp_packet"
    recording_id: Mapped[str] = mapped_column(String(60), ForeignKey("f1_udp_recording.recording_id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[float] = mapped_column(Float)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class StintEvidence(Base):
    """Per-run evidence: target setup, what the game reported, laps used, acceptance decision."""

    __tablename__ = "f1_stint_evidence"
    experiment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StintTelemetrySample(Base):
    """Decoded telemetry samples of a finished stint, one row per measured sample (never downsampled)."""

    __tablename__ = "f1_stint_telemetry"
    experiment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_json: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------------------- setup probing / verification
class ProbeReport(Base):
    """A setup-probe session: initial setup, ordered changes, per-factor findings. Evidence only; never verifies."""

    __tablename__ = "f1_probe_report"
    report_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    packet_format: Mapped[int | None] = mapped_column(Integer, nullable=True)
    game_variant: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recording_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    report_json: Mapped[str] = mapped_column(Text)


class SetupMappingConfirmation(Base):
    """A manual confirmation (by the user, from the game screen) that a catalog factor maps to a packet field."""

    __tablename__ = "f1_setup_mapping_confirmation"
    __table_args__ = (UniqueConstraint("factor", "game_variant", name="uq_f1_setup_mapping_confirmation_factor_variant"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor: Mapped[str] = mapped_column(String(60))
    game_variant: Mapped[str] = mapped_column(String(40))  # f1-25 | f1-25-2026
    packet_field: Mapped[str] = mapped_column(String(60))
    verified: Mapped[bool] = mapped_column(Boolean)
    low_value_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_value_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    probe_report_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    confirmed_by: Mapped[str] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
