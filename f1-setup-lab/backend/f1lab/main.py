"""Command-line entry point: ``f1-setup-lab`` or ``python -m f1lab``."""
from __future__ import annotations

import argparse

import uvicorn

from db.config import resolve_db_url


def main() -> None:
    p = argparse.ArgumentParser(description="F1 SETUP LAB (synthetic data) - experiment workstation on TRIALFORGE")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    a = p.parse_args()
    from f1lab.api.app import create_app

    print(f"F1 SETUP LAB on http://{a.host}:{a.port}  (root database: {resolve_db_url()}; set TRIALFORGE_DB to change)  - SYNTHETIC vehicle model, not real telemetry")
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
