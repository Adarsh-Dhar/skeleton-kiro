"""Request/response schemas for the historical-data API."""

from __future__ import annotations

from pydantic import BaseModel, Field

SESSION_TYPES = ("FP1", "FP2", "FP3", "Q", "SQ", "S", "R")

IMPORT_STATES = ("QUEUED", "DOWNLOADING", "PROCESSING", "SAVING", "COMPLETED", "FAILED", "CANCELLED")


class HistoricalImportRequest(BaseModel):
    year: int = Field(ge=2018, le=2100, description="Season year; FastF1 timing data is available from 2018 onwards")
    event: str = Field(min_length=1, max_length=120, description="Grand Prix name or circuit location, e.g. 'Monza' or 'Italian Grand Prix'")
    session: str = Field(min_length=1, max_length=40, description="Session type code (FP1, FP2, FP3, Q, S, R) or session name")
    drivers: list[str] = Field(default_factory=list, max_length=30, description="Driver codes to import; empty means all drivers in the session")
    include_telemetry: bool = True
    include_position: bool = True
    include_weather: bool = True


class HistoricalCompareRequest(BaseModel):
    driver_a: str = Field(min_length=1, max_length=10)
    lap_a: int = Field(ge=1)
    driver_b: str = Field(min_length=1, max_length=10)
    lap_b: int = Field(ge=1)
    points: int = Field(100, ge=20, le=2000, description="Size of the common distance grid used for the time delta")
