"""Summarise a stored historical session without opening the UI or the database by hand.

    python scripts/inspect_historical_session.py                 # list stored sessions
    python scripts/inspect_historical_session.py <session_id> [--json]

Reports: drivers, laps (valid / deleted / inaccurate), telemetry samples and channels per driver (with missing
channels), track position, weather samples and their spacing, circuit corners, warnings recorded at import.
"""
from __future__ import annotations

import argparse
import json
import sys

import _common  # noqa: F401  (path + database bootstrap)
from _common import cache_dir_for, root_engine

from f1lab.integrations.historical.repository import HistoricalRepository
from f1lab.integrations.historical.service import POSITION_CHANNELS, TELEMETRY_CHANNELS


def inspect(repo: HistoricalRepository, sid: str) -> dict:
    m = repo.manifest(sid)
    if m is None:
        raise KeyError(sid)
    laps = repo.laps(sid)
    out = {"session_id": sid, "event": m.get("event"), "session": m.get("session_type"), "year": m.get("year"),
           "drivers": [d.get("code") for d in m.get("drivers", [])], "warnings": m.get("warnings", []), "partial": m.get("partial", False),
           "corners": len(m.get("corners") or []), "laps": {"total": len(laps)}}  # fmt: skip
    if not laps.empty:
        for col, name in (("is_valid", "valid"), ("is_deleted", "deleted"), ("is_accurate", "accurate")):
            if col in laps.columns:
                s = laps[col]
                out["laps"][name] = int((s == True).sum())  # noqa: E712
                out["laps"][f"{name}_unknown"] = int(s.isna().sum())
    per = {}
    for d in out["drivers"]:
        tel, pos = repo.telemetry(sid, d), repo.position(sid, d)
        present = [c for c in TELEMETRY_CHANNELS if c in tel.columns and tel[c].notna().any()]
        per[d] = {"telemetry_samples": len(tel), "channels": present,
                  "missing_channels": [c for c in TELEMETRY_CHANNELS if c not in present and not (c == "brake_pct" and "brake_applied" in present)
                                       and not (c == "brake_applied" and "brake_pct" in present)],
                  "position_samples": len(pos), "position_channels": [c for c in POSITION_CHANNELS if c in pos.columns and pos[c].notna().any()]}
    out["per_driver"] = per
    wx = repo.weather(sid)
    out["weather"] = {"samples": len(wx)}
    if len(wx) > 1 and "time_s" in wx.columns:
        gaps = wx["time_s"].diff().dropna()
        out["weather"].update(median_interval_s=round(float(gaps.median()), 1), max_interval_s=round(float(gaps.max()), 1))
    out["stored_frames"] = repo.frame_info(sid)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    engine, url = root_engine(a.db)
    repo = HistoricalRepository(engine, cache_dir_for(url))
    if not a.session_id:
        sessions = repo.list_sessions()
        for s in sessions:
            print(f"{s['session_id']:45s} {s.get('event')} {s.get('session_type')} drivers={len(s.get('drivers') or [])}")
        if not sessions:
            print("no historical sessions stored")
        return 0
    try:
        info = inspect(repo, a.session_id)
    except KeyError:
        print(f"session {a.session_id} is not stored (or did not complete)", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(info, indent=2, default=str))
        return 0
    print(f"{info['event']} {info['year']} {info['session']} ({info['session_id']})")
    print(f"laps: {info['laps']}")
    for d, p in info["per_driver"].items():
        print(f"  {d}: {p['telemetry_samples']} telemetry samples, {p['position_samples']} position samples; "
              f"missing channels: {', '.join(p['missing_channels']) or 'none'}")
    print(f"weather: {info['weather']}; circuit corners: {info['corners']}")
    for w in info["warnings"]:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
