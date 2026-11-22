"""A fake OpenF1 server: ``fetch(url, timeout)`` answers like api.openf1.org for one synthetic race (session 99).

HAM (#44) has car data and location. VER (#1) has car data but no location data (OpenF1 answers 404 "No results found").
Nothing here contacts the network.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

from f1lab.integrations.historical.openf1_client import OpenF1Client, RateLimiter

T0 = datetime(2025, 12, 7, 13, 0, 0, tzinfo=UTC)  # session date_start
LAP1 = T0 + timedelta(seconds=600)
LAP_S = 90.0
MEETING = {"meeting_key": 1001, "meeting_name": "Abu Dhabi Grand Prix", "meeting_official_name": "FORMULA 1 ETIHAD AIRWAYS ABU DHABI GRAND PRIX 2025",
           "location": "Yas Island", "circuit_short_name": "Yas Marina Circuit", "country_name": "United Arab Emirates",
           "date_start": "2025-12-05T09:30:00+00:00", "year": 2025}
MEETINGS = [
    {"meeting_key": 900, "meeting_name": "Pre-Season Testing", "meeting_official_name": "FORMULA 1 PRE-SEASON TESTING 2025", "location": "Sakhir",
     "date_start": "2025-02-26T07:00:00+00:00", "year": 2025},
    {"meeting_key": 1000, "meeting_name": "Bahrain Grand Prix", "meeting_official_name": "FORMULA 1 GULF AIR BAHRAIN GRAND PRIX 2025", "location": "Sakhir",
     "circuit_short_name": "Sakhir", "country_name": "Bahrain", "date_start": "2025-04-11T11:30:00+00:00", "year": 2025},
    MEETING,
]
SESSIONS = [
    {"session_key": 98, "meeting_key": 1001, "session_name": "Qualifying", "session_type": "Qualifying", "date_start": "2025-12-06T14:00:00+00:00", "year": 2025},
    {"session_key": 99, "meeting_key": 1001, "session_name": "Race", "session_type": "Race", "date_start": T0.isoformat(), "year": 2025},
    {"session_key": 50, "meeting_key": 1000, "session_name": "Race", "session_type": "Race", "date_start": "2025-04-13T15:00:00+00:00", "year": 2025},
]
DRIVERS = [
    {"driver_number": 44, "name_acronym": "HAM", "full_name": "Lewis HAMILTON", "team_name": "Ferrari", "session_key": 99},
    {"driver_number": 1, "name_acronym": "VER", "full_name": "Max VERSTAPPEN", "team_name": "Red Bull Racing", "session_key": 99},
]


def _laps() -> list[dict]:
    out = []
    for number, base in ((44, 90.0), (1, 89.0)):
        for n in (1, 2, 3):
            start = LAP1 + timedelta(seconds=LAP_S * (n - 1))
            out.append({"session_key": 99, "driver_number": number, "lap_number": n, "date_start": start.isoformat(),
                        "lap_duration": None if n == 1 else base + n * 0.1, "duration_sector_1": 30.0, "duration_sector_2": 30.0,
                        "duration_sector_3": 29.0, "is_pit_out_lap": n == 1})
    return out


def _samples(endpoint: str, number: int) -> list[dict]:
    rows = []
    for i in range(int(LAP_S * 3 * 4)):  # three laps at 4 Hz
        t = LAP1 + timedelta(seconds=i * 0.25)
        base = {"session_key": 99, "driver_number": number, "date": t.isoformat()}
        if endpoint == "car_data":
            rows.append({**base, "speed": 200 + (i % 50), "throttle": 100 if i % 20 < 10 else 0, "brake": 100 if i % 20 >= 15 else 0,
                         "n_gear": 5, "rpm": 11000, "drs": 0})
        else:
            rows.append({**base, "x": 100 + i, "y": 200 + i, "z": 5})
    return rows


DATA = {
    "meetings": MEETINGS, "sessions": SESSIONS, "drivers": DRIVERS, "laps": _laps(),
    "stints": [{"session_key": 99, "driver_number": 44, "stint_number": 1, "lap_start": 1, "lap_end": 3, "compound": "MEDIUM", "tyre_age_at_start": 2},
               {"session_key": 99, "driver_number": 1, "stint_number": 1, "lap_start": 1, "lap_end": 3, "compound": "HARD", "tyre_age_at_start": 0}],
    "pit": [{"session_key": 99, "driver_number": 44, "lap_number": 3, "date": (LAP1 + timedelta(seconds=250)).isoformat(),
             "lane_duration": 22.5, "stop_duration": 2.4}],
    "weather": [{"session_key": 99, "date": (T0 + timedelta(seconds=60 * i)).isoformat(), "air_temperature": 26.0 + i, "track_temperature": 31.0,
                 "humidity": 55, "pressure": 1013.2, "rainfall": 0, "wind_direction": 120, "wind_speed": 2.1} for i in range(5)],
    "race_control": [{"session_key": 99, "date": (T0 + timedelta(seconds=300)).isoformat(), "category": "Flag", "flag": "GREEN", "scope": "Track",
                      "lap_number": 1, "message": "GREEN LIGHT - PIT EXIT OPEN"}],
}
_COND = re.compile(r"^(\w+)(>=|<=|>|<|=)(.*)$")


class FakeOpenF1:
    """Callable ``fetch`` with a call log and switches for failure scenarios."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_next: list[tuple[int, dict[str, str], bytes]] = []  # answered first, in order
        self.fail_endpoint: dict[str, tuple[int, bytes]] = {}  # endpoint -> permanent failure
        self.drop_laps = False

    def __call__(self, url: str, timeout: float):
        self.calls.append(url)
        if self.fail_next:
            return self.fail_next.pop(0)
        path, _, query = url.partition("?")
        endpoint = path.rsplit("/", 1)[-1]
        if endpoint in self.fail_endpoint:
            status, body = self.fail_endpoint[endpoint]
            return status, {}, body
        conds = [m.groups() for part in query.split("&") if part and (m := _COND.match(unquote(part)))]
        eq = {k: v for k, op, v in conds if op == "="}
        if endpoint in ("car_data", "location"):
            number = int(eq["driver_number"])
            rows = _samples(endpoint, number) if not (endpoint == "location" and number == 1) else []
            for k, op, v in conds:
                if k == "date":
                    cut = datetime.fromisoformat(v).replace(tzinfo=UTC)
                    rows = [r for r in rows if (datetime.fromisoformat(r["date"]) >= cut if op == ">=" else datetime.fromisoformat(r["date"]) < cut)]
        elif endpoint == "laps" and self.drop_laps:
            rows = []
        else:
            rows = [r for r in DATA.get(endpoint, []) if all(str(r.get(k)) == v for k, v in eq.items() if k in r)]
        if not rows:
            return 404, {}, json.dumps({"detail": "No results found."}).encode()
        return 200, {}, json.dumps(rows).encode()


def fake_client(cache_dir, fake: FakeOpenF1 | None = None, **kw) -> tuple[OpenF1Client, FakeOpenF1]:
    fake = fake or FakeOpenF1()
    kw.setdefault("limiter", RateLimiter(per_second=10_000, per_minute=10_000_000))
    kw.setdefault("sleep", lambda s: None)
    return OpenF1Client(cache_dir, fetch=fake, **kw), fake
