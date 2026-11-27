"""Optuna availability, tariff period overlap (incl. across midnight) and per-weekday tariff periods."""
from __future__ import annotations

from datetime import datetime

import pytest
from conftest import wait_done  # noqa: F401  (kept for parity with the other API tests)

from wattwise.adapters.household_model import validate_tariff
from wattwise.adapters.tariff_model import WEEKDAY_NAMES, applies, bill, contexts, date_dependent, period_for
from wattwise.api import app as app_module


def P(start, end, price, **kw):
    return {"label": f"{start}-{end}", "start": start, "end": end, "price": price, **kw}


def T(*periods, **kw):
    return {"name": "t", "periods": list(periods), **kw}


# ------------------------------------------------------------------ Optuna
def test_optuna_is_offered_and_suggests_when_installed(run_experiment, client):
    pytest.importorskip("optuna")
    assert "optuna" in [o["id"] for o in client.get("/api/meta").json()["optimisers"]]
    d = run_experiment()
    r = client.post(f"/api/experiments/{d['id']}/optimise/suggest", json={"count": 2, "method": "optuna"})
    assert r.status_code == 200, r.text
    recs = r.json()["recommendations"]
    assert recs and all(x["feasible"] is True for x in recs)


def test_optuna_is_hidden_when_not_installed(client, monkeypatch):
    monkeypatch.setattr(app_module, "optuna_available", lambda: False)
    ids = [o["id"] for o in client.get("/api/meta").json()["optimisers"]]
    assert "optuna" not in ids and ids == ["bayesian", "random", "grid"]


def test_project_declares_the_optuna_extra():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "trialforge-core[optuna]" in text


# ------------------------------------------------------------------ overlap
@pytest.mark.parametrize(
    "periods",
    [
        [P(23, 7, 0.1), P(6, 8, 0.2), P(8, 23, 0.3)],  # overlap across midnight: 06:00-07:00
        [P(0, 24, 0.1), P(6, 8, 0.2)],
        [P(22, 6, 0.1), P(5, 22, 0.2)],
    ],
)
def test_overlapping_periods_are_rejected(periods):
    assert any("overlap" in i for i in validate_tariff(T(*periods)))


def test_adjacent_periods_including_across_midnight_are_accepted():
    assert validate_tariff(T(P(23, 7, 0.1), P(7, 23, 0.3))) == []


def test_overlap_is_rejected_through_the_api(client):
    body = T(P(23, 7, 0.1), P(6, 8, 0.2), P(8, 23, 0.3))
    assert any("overlap" in i for i in client.post("/api/tariffs/validate", json=body).json()["issues"])
    assert client.post("/api/tariffs", json=body).status_code in (400, 422)


# ------------------------------------------------------------------ weekdays
MON_FRI_PEAK = T(
    P(0, 24, 0.10, weekdays=[5, 6]),
    P(0, 17, 0.20, weekdays=[0, 1, 2, 3, 4]),
    P(17, 21, 0.50, weekdays=[0, 1, 2, 3, 4]),
    P(21, 24, 0.20, weekdays=[0, 1, 2, 3, 4]),
)


def test_weekday_periods_validate_and_price_by_real_weekday():
    assert validate_tariff(MON_FRI_PEAK) == [] and date_dependent(MON_FRI_PEAK)
    assert len(contexts(MON_FRI_PEAK)) == 7
    assert period_for(MON_FRI_PEAK, 18.0, None, "fri")["price"] == 0.50
    assert period_for(MON_FRI_PEAK, 18.0, None, "sat")["price"] == 0.10
    assert period_for(MON_FRI_PEAK, 18.0, None, "weekday")["price"] == 0.50  # coarse day types still resolve
    assert period_for(MON_FRI_PEAK, 18.0, None, "weekend")["price"] == 0.10


def test_single_weekday_differs_from_the_rest():
    t = T(P(0, 24, 0.30, weekdays=[2]), P(0, 24, 0.10, weekdays=[0, 1, 3, 4, 5, 6]))
    assert validate_tariff(t) == []
    assert applies(t["periods"][0], None, "wed") and not applies(t["periods"][0], None, "thu")
    wed, thu = datetime(2025, 6, 4, 12), datetime(2025, 6, 5, 12)  # a Wednesday and a Thursday
    assert WEEKDAY_NAMES[wed.weekday()] == "wed"
    assert bill(t, [(wed, 1.0, 1.0)])["total"] == pytest.approx(0.30)
    assert bill(t, [(thu, 1.0, 1.0)])["total"] == pytest.approx(0.10)


def test_missing_weekday_coverage_and_overlap_are_reported():
    gap = T(P(0, 24, 0.1, weekdays=[0, 1, 2, 3, 4]))
    assert any("gaps" in i for i in validate_tariff(gap))
    both = T(P(0, 24, 0.1, weekdays=[0, 1, 2, 3, 4, 5, 6]), P(0, 24, 0.2, weekdays=[6]))
    assert any("overlap" in i for i in validate_tariff(both))
    midnight = T(P(23, 7, 0.1, weekdays=[0]), P(6, 8, 0.2, weekdays=[0]), P(7, 23, 0.3, weekdays=[0]), P(0, 24, 0.1, weekdays=[1, 2, 3, 4, 5, 6]))
    assert any("overlap" in i for i in validate_tariff(midnight))


@pytest.mark.parametrize("bad", [[], [7], [-1], ["mon"], [True]])
def test_invalid_weekdays_are_rejected(bad):
    assert any("weekdays" in i for i in validate_tariff(T(P(0, 24, 0.1, weekdays=bad))))


def test_weekdays_combine_with_days_and_months():
    t = T(P(0, 24, 0.1, days="weekday", weekdays=[0, 1]), P(0, 24, 0.2, days="weekday", weekdays=[2, 3, 4]), P(0, 24, 0.3, days="weekend"))
    assert validate_tariff(t) == []


def test_old_tariffs_without_weekdays_still_work():
    old = T(P(0, 7, 0.1, days="weekday"), P(7, 24, 0.3, days="weekday"), P(0, 24, 0.2, days="weekend"))
    assert validate_tariff(old) == []
    fri, sat = datetime(2025, 6, 6, 8), datetime(2025, 6, 7, 8)
    assert bill(old, [(fri, 1.0, 1.0)])["total"] == pytest.approx(0.3) and bill(old, [(sat, 1.0, 1.0)])["total"] == pytest.approx(0.2)
    assert validate_tariff({**old, "simulation_day": {"month": 6, "day_type": "weekday"}}) == []


def test_weekday_tariff_round_trips_through_the_api_and_simulates(client):
    from inputs import SRC
    r = client.post("/api/tariffs", json={**MON_FRI_PEAK, "name": "week split", "source": SRC, "simulation_day": {"month": 6, "day_type": "fri"}})
    assert r.status_code == 201, r.text
    got = client.get("/api/tariffs").json()
    saved = next(t for t in (got if isinstance(got, list) else got.get("tariffs", [])) if t["name"] == "week split")
    assert saved["periods"][1]["weekdays"] == [0, 1, 2, 3, 4] and saved["simulation_day"]["day_type"] == "fri"
    assert client.post("/api/tariffs", json={**T(P(0, 24, 0.1, weekdays=[9])), "source": SRC}).status_code in (400, 422)
