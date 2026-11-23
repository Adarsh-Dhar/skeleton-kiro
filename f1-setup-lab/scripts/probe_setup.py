"""Probe the game's setup fields and produce an evidence report (it never marks anything verified).

    python scripts/probe_setup.py [--host 127.0.0.1] [--port 20777] [--guided] [--out report.json]
    python scripts/probe_setup.py --from-recording <recording_id>        # re-analyse a stored UDP capture

Start a session in EA SPORTS F1 25 with UDP telemetry on (F1 25 or F1 25: 2026 Season Pack format), go to the garage and
change ONE control at a time to its lowest and then its highest position. With ``--guided`` the script tells you which
control to move and attributes every change to it. It prints each change, and at the end stores a report (initial
setup, every change, which field moved, whether it matched the catalog's expected field) in the root database and
optionally writes it as JSON. The raw packets are stored too (f1_udp_recording, source "probe").

After checking the values against the numbers shown in the game, record your decision per factor and game variant:
    python scripts/verify_setup_mapping.py confirm --factor front_wing --variant f1-25 --report <report_id> --by "your name"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db import create_db_engine, migrate, resolve_db_url  # noqa: E402
from f1lab.catalogs.ea_f1_25 import GAME_FACTORS  # noqa: E402
from f1lab.catalogs.verification import save_probe_report  # noqa: E402
from f1lab.integrations.game_udp import protocols as P  # noqa: E402
from f1lab.integrations.game_udp.probe import SetupProbe  # noqa: E402
from f1lab.integrations.game_udp.receiver import UdpReceiver  # noqa: E402
from f1lab.integrations.game_udp.recorder import RawRecorder, read_raw  # noqa: E402

EXPECTED = {k: v["packet_field"] for k, v in GAME_FACTORS.items()}


def feed(probe: SetupProbe, h, data: bytes, t: float | None, quiet: bool = False) -> None:
    if h.packet_id != P.PID_SETUPS:
        return
    setup = P.decode_setup(data, h)
    first = probe.last is None
    ev = probe.observe(setup, t, h.packet_format)
    if quiet:
        return
    if first:
        print(f"first setup received (packet format {h.packet_format}):\n" + "\n".join(f"  {k:24s}{v}" for k, v in setup.items()), flush=True)
    elif ev:
        who = f"[{ev['control_moved']}] " if ev["control_moved"] else ""
        print(who + "changed: " + ", ".join(f"{k} {a} -> {b}" for k, (a, b) in ev["changed"].items()), flush=True)


def summarise(report: dict) -> str:
    lines = [f"\nprobe report {report['report_id']}  (formats {report['packet_formats']}, variants {report['game_variant_labels']})",
             f"setup packets {report['setup_packets']}, changes {len(report['changes'])}"]
    for f, r in report["factors"].items():
        lines.append(f"  {f:22s} field {r['expected_packet_field']:20s} {r['status']:34s} values {r['values_seen']}")
        lines += [f"      note: {n}" for n in r["notes"]]
    if report["other_fields_changed"]:
        lines.append(f"  other fields that changed: {', '.join(report['other_fields_changed'])}")
    lines.append("NOT VERIFIED: " + report["verification_note"])
    return "\n".join(lines)


async def guide(probe: SetupProbe, factors: list[str]) -> None:
    loop = asyncio.get_running_loop()
    for f in factors:
        await loop.run_in_executor(None, input, f"\nMove ONLY '{GAME_FACTORS[f]['label']}' to its lowest, then highest position. Press Enter to start...")
        probe.mark(f)
        await loop.run_in_executor(None, input, f"  recording changes for {f}; press Enter when done...")
        probe.mark(None)


async def live(args, engine) -> tuple[SetupProbe, str | None]:
    probe = SetupProbe(EXPECTED)
    recorder = None if args.no_record else RawRecorder(engine, host=args.host, port=args.port, source="probe")

    def on_packet(h, data):
        feed(probe, h, data, h.session_time)

    rx = UdpReceiver(on_packet, recorder)
    await rx.start(args.host, args.port)
    print(f"listening on {args.host}:{args.port}; change one control at a time in the garage (Ctrl+C to stop)", flush=True)
    try:
        if args.guided:
            await guide(probe, args.factors or list(EXPECTED))
        elif args.seconds:
            await asyncio.sleep(args.seconds)
        else:
            await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        st = rx.status()
        rx.stop()
        print(f"\npackets accepted {st['packets']}, wrong format {st['wrong_format']}, malformed {st['malformed']}")
        if st["wrong_format"]:
            print("The game is sending an unsupported UDP format: select F1 25 or F1 25: 2026 Season Pack.")
    return probe, recorder.recording_id if recorder else None


def replay(args, engine) -> tuple[SetupProbe, str]:
    probe = SetupProbe(EXPECTED)
    n = 0
    for t, data in read_raw(engine, args.from_recording):
        n += 1
        try:
            h = P.parse_header(data)
            if h.packet_format in P.accepted_formats():
                feed(probe, h, data, t, quiet=args.quiet)
        except P.PacketError:
            continue
    if n == 0:
        raise SystemExit(f"recording '{args.from_recording}' has no packets")
    return probe, args.from_recording


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--seconds", type=float, default=None, help="stop after this many seconds (for tests)")
    ap.add_argument("--guided", action="store_true", help="prompt for one control at a time and attribute changes to it")
    ap.add_argument("--factors", nargs="*", choices=sorted(EXPECTED), help="factors to guide through (default: all four)")
    ap.add_argument("--from-recording", default=None, help="analyse a stored UDP recording instead of listening")
    ap.add_argument("--no-record", action="store_true", help="do not store the raw packets")
    ap.add_argument("--out", default=None, help="also write the JSON report to this file")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--db", default=None, help="database path/URL (default: TRIALFORGE_DB or <repo>/data/trialforge.db)")
    a = ap.parse_args(argv)
    engine = create_db_engine(resolve_db_url(a.db))
    migrate(engine)
    if a.from_recording:
        probe, rid = replay(a, engine)
    else:
        try:
            probe, rid = asyncio.run(live(a, engine))
        except KeyboardInterrupt:
            return 130
    report = probe.report()
    report["recording_id"] = rid
    save_probe_report(engine, report, rid)
    print(summarise(report))
    print(f"\nreport stored in the database as {report['report_id']}" + (f" (raw packets: recording {rid})" if rid else ""))
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=2, default=str))
        print(f"JSON report written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
