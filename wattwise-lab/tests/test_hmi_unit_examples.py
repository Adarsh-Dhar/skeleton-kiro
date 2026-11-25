"""Task 10.6: example tests for wattwise-household-model-improvements (units, migration, timing).

Feature: wattwise-household-model-improvements.
"""
from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from wattwise.adapters import tariff_model as TM
from wattwise.adapters.household_model import DEFAULT_EV, price_at, simulate
from wattwise.services.baseline import BaselineUnavailable, ModelledLoad, check_overlap

TARIFF_BY_DAY = {"name": "Wk", "periods": [{"label": "wd", "start": 0, "end": 24, "price": 0.1, "days": "weekday"},
                                           {"label": "we", "start": 0, "end": 24, "price": 0.5, "days": "weekend"}]}
TARIFF_BY_MONTH = {"name": "Mo", "periods": [{"label": "jan", "start": 0, "end": 24, "price": 0.1, "months": [1]},
                                             {"label": "rest", "start": 0, "end": 24, "price": 0.3, "months": list(range(2, 13))}]}


# ------------------------------------------------------------------ window start dates (6.4, 6.5, 6.7)
def test_friday_start_date_prices_the_saturday_steps_as_weekend():
    ctx = TM.window_context(TARIFF_BY_DAY, date(2026, 3, 6))  # Friday
    assert price_at(TARIFF_BY_DAY, 23.75, ctx) == 0.1  # Friday 23:45
    assert price_at(TARIFF_BY_DAY, 24.0, ctx) == 0.5  # Saturday 00:00
    assert price_at(TARIFF_BY_DAY, 40.0, ctx) == 0.5


def test_month_end_start_date_prices_the_next_month_after_midnight():
    ctx = TM.window_context(TARIFF_BY_MONTH, date(2026, 1, 31))
    assert price_at(TARIFF_BY_MONTH, 23.0, ctx) == 0.1  # 31 Jan
    assert price_at(TARIFF_BY_MONTH, 25.0, ctx) == 0.3  # 1 Feb


def test_window_context_without_a_start_date_raises_for_date_dependent_tariffs():
    with pytest.raises(TM.MissingStartDate):
        TM.window_context(TARIFF_BY_DAY, None)
    assert TM.window_context({"name": "flat", "periods": [{"label": "a", "start": 0, "end": 24, "price": 0.2}]}, None) is None


def test_household_model_passes_its_start_date_to_the_tariff_model(monkeypatch):
    seen = []
    real = TM.window_context

    def spy(tariff, start_date):
        seen.append(start_date)
        return real(tariff, start_date)

    monkeypatch.setattr(TM, "window_context", spy)
    hh = {"ev": None, "base_load_kw": 0.0, "appliances": [{"name": "d", "power_kw": 1.0, "duration_h": 1.0, "flexible": True, "fixed_start": 19.0}],
          "tariff": TARIFF_BY_DAY, "window_start_date": "2026-03-06"}
    simulate({"appliance_start": 25.0}, hh)
    assert date(2026, 3, 6) in seen or "2026-03-06" in seen


# ------------------------------------------------------------------ simulate without EV (2.3)
def test_simulation_without_ev_has_no_ev_load_or_charge_metric():
    hh = {"ev": None, "base_load_kw": 0.4, "tariff": {"name": "f", "periods": [{"label": "a", "start": 0, "end": 24, "price": 0.2}]},
          "appliances": [{"name": "d", "power_kw": 1.0, "duration_h": 1.0, "flexible": True, "fixed_start": 19.0}]}
    out = simulate({"appliance_start": 20.0}, hh)
    assert all(s["ev"] == 0 for s in out["steps"]) and out["metrics"]["ev_energy_kwh"] == 0 and "departure_charge_pct" not in out["metrics"]
    with_ev = simulate({"charge_start": 20.0, "charge_power_kw": 7.0, "appliance_start": 20.0}, {**hh, "ev": DEFAULT_EV})
    assert with_ev["metrics"]["energy_kwh"] > out["metrics"]["energy_kwh"]


# ------------------------------------------------------------------ overlap check (10.1, 10.7)
def test_empty_series_raises_baseline_unavailable():
    with pytest.raises(BaselineUnavailable):
        check_overlap([], [ModelledLoad("x", 1.0, 4)])
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(BaselineUnavailable):
        check_overlap([(t0 + timedelta(minutes=15 * i), None) for i in range(10)], [ModelledLoad("x", 1.0, 4)])


def test_overlap_check_of_a_year_with_20_appliances_takes_under_5_seconds():
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    n = 35_040
    series = [(t0 + timedelta(minutes=15 * i), 0.3 + (1.5 if 80 <= i % 96 < 84 else 0.0)) for i in range(n)]
    loads = [ModelledLoad(f"a{k}", 0.5 + 0.25 * k, 1 + k % 8) for k in range(20)]
    start = time.perf_counter()
    found = check_overlap(series, loads)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"overlap check took {elapsed:.2f}s"
    assert isinstance(found, list) and len(series) == n


# ------------------------------------------------------------------ migration from version 10 (3.8)
def test_version_10_database_migrates_and_existing_file_reads_unknown(tmp_path: Path):
    from sqlalchemy import text

    from db import create_db_engine, migrate
    from wattwise.services.meter_library import MeterLibrary

    engine = create_db_engine(tmp_path / "trialforge.db")
    try:
        migrate(engine, target=10)
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO ww_meter_import (import_id, file_name, imported_at, source, timezone, reading_kind, unit, interval_minutes, first_ts_utc, "
                "last_ts_utc, rows_total, rows_used, rows_skipped, content_sha256, options_json, warnings_json, row_log_json, gaps_json) VALUES "
                "('mi-v10', 'old.csv', '2023-01-01 00:00:00', 'csv upload', 'UTC', 'interval_energy', 'kWh', 30.0, '2023-01-01T00:00:00', "
                "'2023-01-01T00:30:00', 1, 1, 0, 'abc', '{}', '[]', '[]', '[]')"))
            c.execute(text("INSERT INTO ww_meter_reading (import_id, seq, ts_utc, original_ts, source_row, interval_minutes, power_kw, energy_kwh, flags) "
                           "VALUES ('mi-v10', 0, '2023-01-01T00:00:00', '2023-01-01T00:00:00', 2, 30.0, 2.0, 1.0, '[]')"))
        migrate(engine)
        lib = MeterLibrary(engine)
        old = lib.get("mi-v10")
        assert old["source_label"] == "unknown" and old["file_name"] == "old.csv" and old["rows_used"] == 1
        assert old["total_import_kwh"] == pytest.approx(1.0) and old["total_export_kwh"] == 0
        r = lib.readings("mi-v10")[0]
        assert (r.kwh, r.export_kwh, r.export_kw) == (1.0, 0.0, 0.0)
    finally:
        engine.dispose()
