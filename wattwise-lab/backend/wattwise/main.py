"""Command-line entry point: ``wattwise-lab`` or ``python -m wattwise``."""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(description="WATTWISE LAB (simulated data) - household electricity experiments on TRIALFORGE")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8002)
    p.add_argument("--db", default=os.environ.get("WATTWISE_DB", "data/wattwise.db"), help="SQLite file (created if missing)")
    a = p.parse_args()
    os.environ["WATTWISE_DB"] = a.db
    from wattwise.api.app import create_app

    print(f"WATTWISE LAB on http://{a.host}:{a.port}  (database: {a.db})  - SIMULATED household, not a real meter or tariff feed")
    uvicorn.run(create_app(a.db), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
