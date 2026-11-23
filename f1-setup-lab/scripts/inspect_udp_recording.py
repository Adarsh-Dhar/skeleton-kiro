"""Decode and summarise a raw UDP capture (stored recording or a legacy .f1raw file) without the UI.

    python scripts/inspect_udp_recording.py                         # list stored recordings
    python scripts/inspect_udp_recording.py <recording_id> [--json]
    python scripts/inspect_udp_recording.py --file path/to/raw.f1raw [--import]

Reports: packets by id and packet format, malformed / unsupported packets, session UIDs, player car, time span,
setups seen (first and every change), laps completed, telemetry samples and top speed.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import _common  # noqa: F401
from _common import root_engine

from f1lab.integrations.game_udp import protocols as P
from f1lab.integrations.game_udp.live import LiveState
from f1lab.integrations.game_udp.recorder import import_raw_file, list_recordings, read_raw, read_raw_file

NAMES = {P.PID_SESSION: "session", P.PID_LAP_DATA: "lap data", P.PID_SETUPS: "car setups", P.PID_TELEMETRY: "car telemetry", P.PID_HISTORY: "session history"}


def summarise(packets) -> dict:
    live = LiveState()
    by_id, formats, uids = Counter(), Counter(), set()
    bad, unsupported, n, first, last = 0, 0, 0, None, None
    setups, prev = [], None
    for t, data in packets:
        n += 1
        first = t if first is None else first
        last = t
        try:
            h = P.parse_header(data)
        except P.PacketError:
            bad += 1
            continue
        formats[h.packet_format] += 1
        if h.packet_format not in P.accepted_formats():
            unsupported += 1
            continue
        by_id[NAMES.get(h.packet_id, f"id {h.packet_id}")] += 1
        uids.add(h.session_uid)
        try:
            live.on_packet(h, data)
        except P.PacketError:
            bad += 1
            continue
        if live.setup and live.setup != prev:
            changed = {k: [None if prev is None else prev.get(k), v] for k, v in live.setup.items() if prev is None or prev.get(k) != v}
            setups.append({"t": t, "changed": changed if prev is not None else "initial", "setup": dict(live.setup) if prev is None else None})
            prev = dict(live.setup)
    tel = live.telemetry.summary()
    return {"packets": n, "malformed": bad, "unsupported_format": unsupported, "packet_formats": dict(formats), "by_packet": dict(by_id),
            "session_uids": sorted(uids), "player_car": live.player_car, "span_s": round((last or 0) - (first or 0), 3),
            "setup_changes": len(setups) - 1 if setups else 0, "setups": setups[:50],
            "laps_completed": len(live.history.laps) if live.history else 0, "telemetry": tel}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording_id", nargs="?")
    ap.add_argument("--file", default=None, help="a legacy .f1raw capture")
    ap.add_argument("--import", dest="do_import", action="store_true", help="store the --file capture in the root database")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    engine, _ = root_engine(a.db)
    if a.file:
        if a.do_import:
            rid, n = import_raw_file(engine, Path(a.file))
            print(f"stored {n} packets as recording {rid}")
        packets = list(read_raw_file(Path(a.file)))
    elif a.recording_id:
        packets = list(read_raw(engine, a.recording_id))
        if not packets:
            print(f"recording {a.recording_id} has no packets (or does not exist)", file=sys.stderr)
            return 1
    else:
        recs = list_recordings(engine)
        for r in recs:
            print(f"{r['recording_id']:32s} {r['started_at'][:19]} {r['source']:7s} packets={r['packets']}")
        if not recs:
            print("no UDP recordings stored")
        return 0
    s = summarise(packets)
    if a.json:
        print(json.dumps(s, indent=2, default=str))
        return 0
    print(f"packets {s['packets']} (malformed {s['malformed']}, unsupported format {s['unsupported_format']}), formats {s['packet_formats']}")
    print(f"by packet: {s['by_packet']}")
    print(f"sessions {s['session_uids']}, player car {s['player_car']}, span {s['span_s']} s, laps completed {s['laps_completed']}")
    print(f"setup changes: {s['setup_changes']}; telemetry: {s['telemetry']}")
    for ch in s["setups"][1:11]:
        print(f"  t={ch['t']:.3f} changed {ch['changed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
