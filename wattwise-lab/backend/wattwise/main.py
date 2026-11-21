"""Command-line entry point: ``wattwise-lab`` or ``python -m wattwise``."""
from __future__ import annotations

import argparse

import uvicorn

from db.config import resolve_db_url


def main() -> None:
    p = argparse.ArgumentParser(description="WATTWISE LAB (simulated data) - household electricity experiments on TRIALFORGE")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8002)
    a = p.parse_args()
    from wattwise.api.app import create_app

    print(f"WATTWISE LAB on http://{a.host}:{a.port}  (root database: {resolve_db_url()}; set TRIALFORGE_DB to change)  - SIMULATED household, not a real meter or tariff feed")
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
