"""Request/response schemas for the historical-data API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from f1lab.api.schemas import CsvImportOptions

SESSION_TYPES = ("FP1", "FP2", "FP3", "Q", "SQ", "S", "R")

IMPORT_STATES = ("QUEUED", "DOWNLOADING", "PROCESSING", "SAVING", "COMPLETED", "FAILED", "CANCELLED")


Provider = Literal["openf1", "csv_upload"]
OPENF1_MIN_YEAR = 2023


class HistoricalImportRequest(BaseModel):
    provider: Provider = Field("openf1", description="Historical data source: OpenF1 (free, no account, 2023 onwards)")
    year: int | None = Field(None, ge=OPENF1_MIN_YEAR, le=2100, description="Season year; OpenF1 historical data is available from 2023 onwards "
                             "(required for openf1; csv_upload takes it from csv.metadata)")
    event: str | None = Field(None, min_length=1, max_length=120, description="Grand Prix name or circuit location, e.g. 'Monza' or "
                              "'Italian Grand Prix' (required for openf1)")
    session: str | None = Field(None, min_length=1, max_length=40, description="Session type code (FP1, FP2, FP3, Q, S, R) or session name "
                                "(required for openf1)")
    csv: CsvImportOptions | None = Field(None, description="csv_upload only: staged file hash, Column_Mapping, units, policies and Session_Metadata")
    drivers: list[str] = Field(default_factory=list, max_length=30, description="Driver codes to import; empty means all drivers in the session")
    include_telemetry: bool = True
    include_position: bool = True
    include_weather: bool = True
    include_race_data: bool = Field(True, description="OpenF1 only: also fetch running order, results, grid, gaps, overtakes, team radio and championships")
    race_datasets: list[str] | None = Field(None, max_length=11, description="OpenF1 only: which optional datasets to fetch "
                                            "(race_position, session_result, starting_grid, intervals, overtakes, team_radio, drivers_championship, "
                                            "teams_championship, stints, pit, race_control); omitted means all")
    refresh_cache: bool = Field(False, description="OpenF1 only: ignore cached answers and download everything again")

    @model_validator(mode="after")
    def _provider_fields(self):
        if self.provider == "csv_upload":
            if self.csv is None:
                raise ValueError("csv is required for provider csv_upload: give the file_hash returned by the preview endpoint")
        else:
            missing = [n for n in ("year", "event", "session") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required for provider {self.provider}")
            if self.csv is not None:
                raise ValueError("csv is only valid for provider csv_upload")
        return self

    @model_validator(mode="after")
    def _datasets_known(self):
        known = ("race_position", "session_result", "starting_grid", "intervals", "overtakes", "team_radio", "drivers_championship",
                 "teams_championship", "stints", "pit", "race_control")
        bad = [n for n in self.race_datasets or [] if n not in known]
        if bad:
            raise ValueError(f"unknown dataset(s): {', '.join(bad)}; choose from {', '.join(known)}")
        return self


class HistoricalCompareRequest(BaseModel):
    driver_a: str = Field(min_length=1, max_length=10)
    lap_a: int = Field(ge=1)
    driver_b: str = Field(min_length=1, max_length=10)
    lap_b: int = Field(ge=1)
    points: int = Field(100, ge=20, le=2000, description="Size of the common distance grid used for the time delta")


CompareMetric = Literal["best_lap", "median_lap", "finish_position", "grid_position", "points"]


class SeasonCompareRequest(BaseModel):
    sessions: list[str] = Field(min_length=2, max_length=24, description="Stored session ids to compare (2 to 24)")
    drivers: list[str] = Field(min_length=1, max_length=6, description="Driver codes (1 to 6)")
    metric: CompareMetric = Field(description="best_lap, median_lap, finish_position, grid_position or points")

    @model_validator(mode="after")
    def _items_not_blank(self):
        if any(not s.strip() for s in self.sessions):
            raise ValueError("sessions must not contain empty ids")
        if any(not d.strip() or len(d) > 10 for d in self.drivers):
            raise ValueError("drivers must be non-empty driver codes of at most 10 characters")
        return self
