"""Request/response schemas for the historical-data API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

SESSION_TYPES = ("FP1", "FP2", "FP3", "Q", "SQ", "S", "R")

IMPORT_STATES = ("QUEUED", "DOWNLOADING", "PROCESSING", "SAVING", "COMPLETED", "FAILED", "CANCELLED")


Provider = Literal["fastf1", "openf1"]
OPENF1_MIN_YEAR = 2023


class HistoricalImportRequest(BaseModel):
    provider: Provider = Field("fastf1", description="Historical data source: 'fastf1' (needs F1TV credentials) or 'openf1' (free, 2023 onwards)")
    year: int = Field(ge=2018, le=2100, description="Season year; FastF1 timing data is available from 2018 onwards")
    event: str = Field(min_length=1, max_length=120, description="Grand Prix name or circuit location, e.g. 'Monza' or 'Italian Grand Prix'")
    session: str = Field(min_length=1, max_length=40, description="Session type code (FP1, FP2, FP3, Q, S, R) or session name")
    drivers: list[str] = Field(default_factory=list, max_length=30, description="Driver codes to import; empty means all drivers in the session")
    include_telemetry: bool = True
    include_position: bool = True
    include_weather: bool = True

    @model_validator(mode="after")
    def _provider_years(self):
        if self.provider == "openf1" and self.year < OPENF1_MIN_YEAR:
            raise ValueError(f"OpenF1 historical data starts in {OPENF1_MIN_YEAR}; use the FastF1 provider for earlier seasons")
        return self


class HistoricalCompareRequest(BaseModel):
    driver_a: str = Field(min_length=1, max_length=10)
    lap_a: int = Field(ge=1)
    driver_b: str = Field(min_length=1, max_length=10)
    lap_b: int = Field(ge=1)
    points: int = Field(100, ge=20, le=2000, description="Size of the common distance grid used for the time delta")
