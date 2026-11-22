"""Download small, real OpenF1 responses into tests/fixtures/openf1/ (the fixtures the schema tests run on).

    python scripts/refresh_openf1_fixtures.py [--out tests/fixtures/openf1]

Needs network access to api.openf1.org; respects the client's 3/s and 30/min limits, so it takes a few minutes. Large endpoints are
trimmed to their first rows plus a few rows that contain nulls; small endpoints are kept whole. Nothing else is modified or invented.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from f1lab.integrations.historical import openf1_normalizer as N  # noqa: E402
from f1lab.integrations.historical.openf1_client import OpenF1Client, OpenF1Error  # noqa: E402

# (label, year, meeting-name fragment, session name)
SESSIONS = [
    ("2023-bahrain-race", 2023, "bahrain", "Race"),
    ("2024-monza-qualifying", 2024, "italian", "Qualifying"),
    ("2024-miami-sprint", 2024, "miami", "Sprint"),
    ("2025-abu-dhabi-race", 2025, "abu dhabi", "Race"),
    ("2026-latest-race", 2026, "", "Race"),
]
WHOLE = ("meeting_sessions", "drivers", "session_result", "starting_grid", "drivers_championship", "teams_championship", "stints", "pit")
HEAD = 6


def trim(rows: list[dict]) -> list[dict]:
    nulls = [r for r in rows[HEAD:] if any(v is None for v in r.values())][:3]
    return rows[:HEAD] + nulls


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "openf1"))
    out = Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    client = OpenF1Client(tempfile.mkdtemp(prefix="openf1-fixtures-"))
    summary = {}
    for label, year, frag, name in SESSIONS:
        try:
            meetings = [m for m in client.get("meetings", {"year": year}) if frag in str(m.get("meeting_name", "")).lower() and "test" not in str(m.get("meeting_name", "")).lower()]
            if not meetings:
                summary[label] = "no such meeting published"
                continue
            now = datetime.now(UTC)
            cands = []
            for m in meetings:
                for s in client.get("sessions", {"meeting_key": m["meeting_key"]}):
                    end = N.parse_dt(s.get("date_end"))
                    if s.get("session_name") == name and end and end < now - timedelta(days=2):
                        cands.append((m, s))
            if not cands:
                summary[label] = "no finished session published"
                continue
            meeting, sess = sorted(cands, key=lambda t: str(t[1].get("date_start")))[-1]
            sk, data = sess["session_key"], {"meetings": [meeting], "sessions": [sess]}
            data["meeting_sessions"] = client.get("sessions", {"meeting_key": meeting["meeting_key"]})
            for ep in ("drivers", "laps", "stints", "pit", "weather", "race_control", "position", "session_result",
                       "overtakes", "team_radio", "drivers_championship", "teams_championship"):
                data[ep] = client.get(ep, {"session_key": sk})
            # OpenF1 files the starting grid under the qualifying-type session that set it, not under the race
            grid_names = {"Race": ("Qualifying",), "Sprint": ("Sprint Qualifying", "Sprint Shootout")}.get(name, ())
            grid_sess = next((x for x in data["meeting_sessions"] if x.get("session_name") in grid_names), None)
            data["starting_grid"] = client.get("starting_grid", {"session_key": grid_sess["session_key"]}) if grid_sess else []
            num = next((d["driver_number"] for d in data["drivers"]), 1)
            first = next((N.parse_dt(lp.get("date_start")) for lp in data["laps"] if lp.get("driver_number") == num and lp.get("lap_number") == 2), None)
            if first:
                rng = (("date", ">=", N.api_time(first)), ("date", "<", N.api_time(first + timedelta(seconds=30), ceil=True)))
                for ep in ("car_data", "location"):
                    data[ep] = client.get(ep, {"session_key": sk, "driver_number": num}, rng)
            data["intervals"] = client.get("intervals", {"session_key": sk, "driver_number": num})
            kept = {k: (v if k in WHOLE or k in ("meetings", "sessions") else trim(v)) for k, v in data.items()}
            kept["_meta"] = {"label": label, "session_key": sk, "meeting_key": meeting["meeting_key"], "driver_number": num,
                             "downloaded": datetime.now(UTC).isoformat(timespec="seconds"), "source": "https://api.openf1.org/v1",
                             "full_row_counts": {k: len(v) for k, v in data.items()}}
            (out / f"{label}.json").write_text(json.dumps(kept, indent=1, sort_keys=True))
            summary[label] = kept["_meta"]["full_row_counts"]
        except OpenF1Error as exc:
            summary[label] = f"failed: {exc}"
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
