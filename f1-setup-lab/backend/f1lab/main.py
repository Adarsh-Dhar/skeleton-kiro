"""Command-line entry point: ``f1-setup-lab`` or ``python -m f1lab``."""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(description="F1 SETUP LAB (synthetic data) - experiment workstation on TRIALFORGE")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--db", default=os.environ.get("F1LAB_DB", "data/f1lab.db"), help="SQLite file (created if missing)")
    a = p.parse_args()
    os.environ["F1LAB_DB"] = a.db
    from f1lab.api.app import create_app

    print(f"F1 SETUP LAB on http://{a.host}:{a.port}  (database: {a.db})  - SYNTHETIC vehicle model, not real telemetry")
    uvicorn.run(create_app(a.db), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
