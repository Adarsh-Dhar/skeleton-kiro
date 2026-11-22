"""Import one historical F1 session (FastF1) into the root database from the command line.

    python scripts/import_historical.py --year 2024 --event Monza --session Q --drivers VER LEC [--force] [--no-position]

Uses the same import job machinery as the app (the job appears under Historical F1 > imports), the same FastF1 cache
(``<repo>/data/fastf1-cache``) and the same database (``TRIALFORGE_DB`` or ``<repo>/data/trialforge.db``).
Exit code 0 = completed, 1 = failed/cancelled, 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db import create_db_engine, migrate, resolve_db_url  # noqa: E402
from db.config import sqlite_file_path  # noqa: E402
from f1lab.integrations.historical.fastf1_client import FastF1Client  # noqa: E402
from f1lab.integrations.historical.schemas import HistoricalImportRequest  # noqa: E402
from f1lab.integrations.historical.service import HistoricalService  # noqa: E402


def build_service(db: str | None) -> HistoricalService:
    url = resolve_db_url(db)
    engine = create_db_engine(url)
    migrate(engine)
    file = sqlite_file_path(url)
    cache = (file.parent if file else Path("data")) / "fastf1-cache"
    # on a terminal FastF1 may ask for your F1TV sign-in (or set F1_USERNAME / F1_PASSWORD beforehand; needs F1TV Access/Pro/Premium)
    return HistoricalService(engine, cache, client_factory=lambda: FastF1Client(cache, interactive=sys.stdin.isatty()))


async def run(args: argparse.Namespace) -> int:
    svc = build_service(args.db)
    svc.recover()
    req = HistoricalImportRequest(year=args.year, event=args.event, session=args.session, drivers=args.drivers or [],
                                  include_telemetry=not args.no_telemetry, include_position=not args.no_position,
                                  include_weather=not args.no_weather)
    job = await svc.start_import(req, force=args.force)
    jid = job["import_id"]
    if job.get("duplicate") and job["state"] == "COMPLETED":
        print(f"already imported: session {job['session_id']} (job {jid}); use --force to download again")
        return 0
    print(f"import {jid} started")
    last = None
    try:
        while True:
            j = svc.import_status(jid)
            line = f"[{j['state']}] {j['detail']}"
            if line != last:
                print(line, flush=True)
                last = line
            if j["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        svc.cancel_import(jid)
        print("cancelling ...", flush=True)
        await svc.shutdown()
        return 1
    if j["state"] == "COMPLETED":
        m = svc.get_session(j["session_id"])
        print(f"stored session {m['session_id']}: {m['event']} {m['session_type']}, drivers {', '.join(d['code'] for d in m['drivers'])}")
        for w in m.get("warnings", []):
            print(f"warning: {w}")
        return 0
    print(f"{j['state']} ({j['error_code']}): {j['error']}", file=sys.stderr)
    return 1



def run_without_waiting_for_workers(coro):
    """Like asyncio.run(), but does not wait for executor threads at the end: after an import timeout, FastF1's worker
    thread can stay blocked on the network for a long time, and the result is already known."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--event", required=True, help="Grand Prix name or location, e.g. Monza")
    ap.add_argument("--session", required=True, help="FP1 FP2 FP3 Q SQ S R")
    ap.add_argument("--drivers", nargs="*", default=[], help="driver codes or numbers; default all")
    ap.add_argument("--force", action="store_true", help="download again even if the session is stored")
    ap.add_argument("--no-telemetry", action="store_true")
    ap.add_argument("--no-position", action="store_true")
    ap.add_argument("--no-weather", action="store_true")
    ap.add_argument("--db", default=None, help="database path/URL (default: TRIALFORGE_DB or <repo>/data/trialforge.db)")
    args = ap.parse_args(argv)
    t0 = time.time()
    try:
        rc = run_without_waiting_for_workers(run(args))
    except ValueError as exc:  # request validation
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"done in {time.time() - t0:.1f}s")
    return rc


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # FastF1's download runs in a worker thread that cannot be interrupted; after a timeout it can stay blocked on the network.
    # The result is final at this point, so exit without waiting for that thread (sys.exit would wait for it indefinitely).
    os._exit(code)
