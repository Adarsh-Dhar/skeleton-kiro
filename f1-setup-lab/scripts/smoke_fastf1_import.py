"""Real-network smoke test of the FastF1 import (needs internet access to the FastF1 / F1 live-timing servers).

    python scripts/smoke_fastf1_import.py --year 2024 --event Monza --session Q --drivers VER LEC [--force] [--db PATH]

Imports the session into the root database through the same job machinery as the app, then reports: session
downloaded, drivers found, laps imported, telemetry samples, weather, track position, circuit corners, database rows
written, missing channels. Exit codes: 0 PASSED, 1 FAILED, 3 SKIPPED (no network / FastF1 unreachable: this is NOT
a pass and must not be counted as one).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys

import _common  # noqa: F401
from _common import cache_dir_for, root_engine
from inspect_historical_session import inspect

from f1lab.integrations.historical.fastf1_client import credentials_configured
from f1lab.integrations.historical.schemas import HistoricalImportRequest
from f1lab.integrations.historical.service import HistoricalService

SKIPPED, FAILED, PASSED = 3, 1, 0
HOSTS = ("livetiming.formula1.com", "api.jolpi.ca")


def network_available(timeout: float = 5.0) -> tuple[bool, str]:
    for host in HOSTS:
        try:
            socket.create_connection((host, 443), timeout=timeout).close()
            return True, host
        except OSError as exc:
            last = f"{host}: {exc}"
    return False, last


async def run(a) -> tuple[int, dict]:
    engine, url = root_engine(a.db)
    svc = HistoricalService(engine, cache_dir_for(url), job_timeout_s=a.timeout)
    svc.recover()
    req = HistoricalImportRequest(year=a.year, event=a.event, session=a.session, drivers=a.drivers)
    job = await svc.start_import(req, force=a.force)
    while job["state"] not in ("COMPLETED", "FAILED", "CANCELLED"):
        await asyncio.sleep(1)
        job = svc.import_status(job["import_id"])
        print(f"  [{job['state']}] {job['detail']}", flush=True)
    if job["state"] != "COMPLETED":
        if job.get("error_code") in ("NETWORK_ERROR", "NETWORK_TIMEOUT"):
            return SKIPPED, {"result": "SKIPPED", "reason": f"{job['error_code']}: {job['error']}; this is NOT a pass", "job": job}
        if job.get("error_code") == "NO_LAP_DATA" and not credentials_configured():
            return SKIPPED, {"result": "SKIPPED", "job": job,
                             "reason": "FastF1 needs an F1TV Access/Pro/Premium account: set F1_USERNAME and F1_PASSWORD in your environment and run again; this is NOT a pass"}
        return FAILED, {"result": "FAILED", "reason": f"{job.get('error_code')}: {job['error']}", "job": job}
    info = inspect(svc.repo, job["session_id"])
    rows = sum(f["rows"] for f in info["stored_frames"])
    checks = {
        "session downloaded": True,
        "drivers found": sorted(set(info["drivers"])) == sorted(set(d.upper() for d in a.drivers)) if a.drivers else bool(info["drivers"]),
        "laps imported": info["laps"]["total"] > 0,
        "telemetry samples": all(p["telemetry_samples"] > 0 for p in info["per_driver"].values()),
        "weather available": info["weather"]["samples"] > 0,
        "track position available": all(p["position_samples"] > 0 for p in info["per_driver"].values()),
        "circuit corners found": info["corners"] > 0,
        "database rows written": rows > 0,
    }
    summary = {"result": "PASSED" if all(checks.values()) else "FAILED", "checks": checks, "session_id": job["session_id"], "rows_written": rows,
               "job_detail": job["detail"], "laps": info["laps"], "weather": info["weather"],
               "telemetry_samples": {d: p["telemetry_samples"] for d, p in info["per_driver"].items()},
               "missing_channels": {d: p["missing_channels"] for d, p in info["per_driver"].items()}, "warnings": info["warnings"]}  # fmt: skip
    return (PASSED if all(checks.values()) else FAILED), summary



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
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--event", default="Monza")
    ap.add_argument("--session", default="Q")
    ap.add_argument("--drivers", nargs="*", default=["VER", "LEC"])
    ap.add_argument("--force", action="store_true", help="download again even if stored")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--db", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ok, where = network_available()
    if not ok:
        res = {"result": "SKIPPED", "reason": f"no network access to FastF1 servers ({where}); this is NOT a pass"}
        rc = SKIPPED
    else:
        print(f"network ok ({where}); importing {a.year} {a.event} {a.session} {' '.join(a.drivers)} ...", flush=True)
        rc, res = run_without_waiting_for_workers(run(a))
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"\n{res['result']}" + (f": {res['reason']}" if res.get("reason") else ""))
        for k, v in res.get("checks", {}).items():
            print(f"  [{'x' if v else ' '}] {k}")
        for k in ("session_id", "rows_written", "laps", "weather", "telemetry_samples", "missing_channels", "warnings"):
            if k in res:
                print(f"  {k}: {res[k]}")
    return rc


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # FastF1's download runs in a worker thread that cannot be interrupted; after a timeout it can stay blocked on the network.
    # The result is final at this point, so exit without waiting for that thread (sys.exit would wait for it indefinitely).
    os._exit(code)
