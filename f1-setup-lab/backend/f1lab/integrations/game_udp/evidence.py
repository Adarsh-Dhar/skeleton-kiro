"""Per-run evidence: what was asked for, what the game reported, which laps were used, and why a stint was accepted or rejected."""

from __future__ import annotations

import gzip
import json
from pathlib import Path


def evidence_path(data_dir: Path, exp_id: str, run_id: str) -> Path:
    return data_dir / "evidence" / exp_id / f"{run_id}.json"


def telemetry_series_path(data_dir: Path, exp_id: str, run_id: str) -> Path:
    return evidence_path(data_dir, exp_id, run_id).with_suffix(".telemetry.jsonl.gz")


def save_telemetry_series(data_dir: Path, exp_id: str, run_id: str, records: list[dict]) -> Path:
    """Persist decoded telemetry samples as compressed JSONL (one sample per line)."""
    p = telemetry_series_path(data_dir, exp_id, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    tmp.replace(p)  # atomic: a crash never leaves half a file
    return p


def load_telemetry_series(data_dir: Path, exp_id: str, run_id: str) -> list[dict] | None:
    p = telemetry_series_path(data_dir, exp_id, run_id)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def save_evidence(data_dir: Path, exp_id: str, run_id: str, payload: dict) -> Path:
    p = evidence_path(data_dir, exp_id, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(p)  # atomic: a crash never leaves half a file
    return p


def load_evidence(data_dir: Path, exp_id: str, run_id: str) -> dict | None:
    p = evidence_path(data_dir, exp_id, run_id)
    return json.loads(p.read_text()) if p.exists() else None
