"""TEST FIXTURES ONLY: hand-made inputs for the automated tests.

These values are NOT real data and are never shipped with or seeded into the application (the app starts empty). Every record
carries a source that says so (``source_name: "automated test fixture"``). They reuse the numbers earlier versions shipped as
examples so that the tests keep exercising the same model behaviour.
"""
from __future__ import annotations

import copy
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
SRC = {"source_type": "user_provided", "source_name": "automated test fixture", "verification": "user_supplied",
       "notes": "test input, not real data"}
TARIFFS = [
    {"name": "Time-of-use", "source": SRC,
     "periods": [{"label": "Night", "start": 23, "end": 7, "price": 0.11}, {"label": "Day", "start": 7, "end": 16, "price": 0.22},
                 {"label": "Peak", "start": 16, "end": 21, "price": 0.36}, {"label": "Evening", "start": 21, "end": 23, "price": 0.22}]},
    {"name": "Flat rate", "source": SRC, "periods": [{"label": "All day", "start": 0, "end": 24, "price": 0.25}]},
    {"name": "Night saver demand", "source": SRC, "demand_charge_per_kw": 0.60,
     "periods": [{"label": "Night", "start": 0, "end": 6, "price": 0.09}, {"label": "Standard", "start": 6, "end": 24, "price": 0.28}]},
]  # fmt: skip
TARIFF_IDS = ("time-of-use", "flat-rate", "night-saver-demand")
EV = {"battery_kwh": 60.0, "initial_soc_pct": 30.0, "desired_charge_pct": 80.0, "departure_hour": 7.5, "plug_in_hour": 19.0, "efficiency": 0.92,
      "source": SRC}
APPLIANCES = [
    {"name": "Dishwasher", "power_kw": 1.1, "duration_h": 2.0, "flexible": True, "fixed_start": 20.0, "source": SRC},
    {"name": "Washing machine", "power_kw": 0.5, "duration_h": 1.5, "flexible": True, "fixed_start": 19.0, "source": SRC},
    {"name": "Tumble dryer", "power_kw": 2.0, "duration_h": 1.5, "flexible": False, "fixed_start": 18.0, "source": SRC},
]  # fmt: skip
CHARGER_KW = 11.0
# Body fields shared by /api/simulate/day and /api/experiments.
INPUTS = {"tariff_id": "time-of-use", "ev": EV, "appliances": APPLIANCES, "base_load_kw": 0.35, "base_load_source": SRC,
          "charger_capacity_kw": CHARGER_KW, "charger_source": SRC}
# Body of PUT /api/household (the household calls the charger field charger_power_kw).
HOUSEHOLD = {"tariff_id": "time-of-use", "ev": EV, "appliances": APPLIANCES, "base_load_kw": 0.35, "base_load_source": SRC,
             "charger_power_kw": CHARGER_KW, "charger_source": SRC}
def inputs(**over) -> dict:
    """A request body with the test inputs; keyword arguments replace fields (``ev=None`` removes the EV and its charger)."""
    body = copy.deepcopy(INPUTS)
    if over.get("ev", ...) is None and "charger_capacity_kw" not in over:
        body.pop("charger_capacity_kw")
        body.pop("charger_source")
    body.update(copy.deepcopy(over))
    return body
def household(**over) -> dict:
    body = copy.deepcopy(HOUSEHOLD)
    if over.get("ev", ...) is None and "charger_power_kw" not in over:
        body.pop("charger_power_kw")
        body.pop("charger_source")
    body.update(copy.deepcopy(over))
    return body
def appliance(name: str, power_kw: float = 1.0, duration_h: float = 1.0, flexible: bool = True, fixed_start: float = 19.0, **kw) -> dict:
    return {"name": name, "power_kw": power_kw, "duration_h": duration_h, "flexible": flexible, "fixed_start": fixed_start, "source": SRC, **kw}
def ev(**over) -> dict:
    return {**copy.deepcopy(EV), **over}
def tariff(name: str, periods: list[dict], **kw) -> dict:
    return {"name": name, "source": SRC, "periods": periods, **kw}
def seed_tariffs(client) -> None:
    for t in TARIFFS:
        r = client.post("/api/tariffs", json=t)
        assert r.status_code == 201, r.text
def meter_csv(unit: str = "kW") -> str:
    return (FIXTURES / ("test_meter_kw.csv" if unit == "kW" else "test_meter_w.csv")).read_text()
# Model-level fixtures (households passed straight to household_model.simulate). Same test values as above.
TEST_TARIFFS = [{"id": tid, **{k: v for k, v in t.items() if k != "source"}} for tid, t in zip(TARIFF_IDS, TARIFFS, strict=True)]
for _t in TEST_TARIFFS:
    _t.setdefault("demand_charge_per_kw", 0.0)
TEST_EV = {k: v for k, v in EV.items() if k != "source"}
TEST_APPLIANCES = [{k: v for k, v in a.items() if k != "source"} for a in APPLIANCES]
# An explicit schedule for model tests (the values earlier versions used as their fixed baseline).
TEST_SETUP = {"charge_start": 19.0, "charge_power_kw": 7.4, "appliance_start": 19.0}
# A test-only background-load profile (96 quarter-hours from 16:00): the shape earlier versions invented for every household
# (0.35 kW plus 1.0 kW 18:00-21:00 and 0.6 kW 06:30-08:30). The app no longer uses it; regression tests pass it explicitly so
# their pinned costs still check the pricing code.
TEST_PROFILE = [0.35 + (1.0 if 18 <= (16 + i * 0.25) % 24 < 21 else 0.0) + (0.6 if 6.5 <= (16 + i * 0.25) % 24 < 8.5 else 0.0) for i in range(96)]
# Evidence retrieval in tests never touches the network: the store's fetcher is replaced by this stub.
FAKE_DOCUMENT = b"%PDF-1.4 test tariff order (automated test fixture, not a real document)"
def fake_fetch(url: str) -> dict:
    return {"final_url": url, "status": 200, "content_type": "application/pdf", "body": FAKE_DOCUMENT}
def retrieved_source(client, url: str = "https://regulator.example/tariff-order.pdf", **over) -> dict:
    """An official source backed by a document WATTWISE 'retrieved' (via the stub)."""
    client.app.state.lab.evidence.fetch = fake_fetch
    ev = client.post("/api/evidence/retrieve", json={"url": url})
    assert ev.status_code == 201, ev.text
    return {"source_type": "official", "source_name": "State electricity regulatory commission", "document": url, "collected_on": "2026-11-01",
            "effective_from": "2025-04-01", "effective_to": "2099-03-31", "verification": "verified", "evidence_id": ev.json()["evidence_id"], **over}
