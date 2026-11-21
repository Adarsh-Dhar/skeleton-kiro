"""Manual setup-mapping confirmations (``f1_setup_mapping_confirmation``) and probe reports (``f1_probe_report``).

The catalog in ``ea_f1_25.py`` keeps ``verified: False`` in code. A factor counts as verified for a game variant only
when a person recorded a confirmation for that variant (``confirm``). Every status says where it came from.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, select

from db.engine import immediate_transaction
from db.models.f1 import ProbeReport, SetupMappingConfirmation
from f1lab.catalogs.ea_f1_25 import GAME_FACTORS
from f1lab.integrations.game_udp.probe import VARIANT_LABELS

VARIANTS = tuple(VARIANT_LABELS)


def save_probe_report(engine: Engine, report: dict, recording_id: str | None = None) -> str:
    rid = report["report_id"]
    variant = report["game_variants"][0] if len(report.get("game_variants") or []) == 1 else None
    fmt = report["packet_formats"][0] if len(report.get("packet_formats") or []) == 1 else None
    with immediate_transaction(engine) as c:
        c.execute(delete(ProbeReport).where(ProbeReport.report_id == rid))
        c.execute(ProbeReport.__table__.insert().values(report_id=rid, created_at=datetime.now(UTC), packet_format=fmt, game_variant=variant,
                                                        recording_id=recording_id, report_json=json.dumps(report, default=str)))
    return rid


def get_probe_report(engine: Engine, report_id: str) -> dict | None:
    with engine.connect() as c:
        row = c.execute(select(ProbeReport.report_json).where(ProbeReport.report_id == report_id)).scalar()
    return json.loads(row) if row else None


def list_probe_reports(engine: Engine, limit: int = 50) -> list[dict]:
    q = select(ProbeReport.report_id, ProbeReport.created_at, ProbeReport.game_variant, ProbeReport.packet_format).order_by(ProbeReport.created_at.desc()).limit(limit)
    with engine.connect() as c:
        return [{"report_id": r, "created_at": str(t), "game_variant": v, "packet_format": f} for r, t, v, f in c.execute(q)]


def confirm(engine: Engine, factor: str, game_variant: str, *, verified: bool, confirmed_by: str, low_value_seen: float | None = None,
            high_value_seen: float | None = None, probe_report_id: str | None = None, note: str | None = None) -> dict:
    """Record a person's decision for one factor in one game variant (replaces an earlier decision)."""
    if factor not in GAME_FACTORS:
        raise ValueError(f"unknown factor '{factor}'; one of {sorted(GAME_FACTORS)}")
    if game_variant not in VARIANTS:
        raise ValueError(f"unknown game variant '{game_variant}'; one of {list(VARIANTS)}")
    if not confirmed_by or not confirmed_by.strip():
        raise ValueError("confirmed_by is required (who compared the packet values with the game screen)")
    if verified and probe_report_id is None:
        raise ValueError("a verified confirmation must reference the probe report that produced the evidence")
    if probe_report_id is not None:
        rep = get_probe_report(engine, probe_report_id)
        if rep is None:
            raise ValueError(f"probe report '{probe_report_id}' not found")
        if verified and rep.get("game_variants") and game_variant not in rep["game_variants"]:
            raise ValueError(f"probe report {probe_report_id} was recorded with {rep['game_variants']}, not {game_variant}")
    field = GAME_FACTORS[factor]["packet_field"]
    with immediate_transaction(engine) as c:
        c.execute(delete(SetupMappingConfirmation).where(SetupMappingConfirmation.factor == factor,
                                                         SetupMappingConfirmation.game_variant == game_variant))
        c.execute(SetupMappingConfirmation.__table__.insert().values(
            factor=factor, game_variant=game_variant, packet_field=field, verified=bool(verified), low_value_seen=low_value_seen,
            high_value_seen=high_value_seen, probe_report_id=probe_report_id, confirmed_by=confirmed_by.strip(), note=note,
            confirmed_at=datetime.now(UTC)))
    return status(engine)[game_variant][factor]


def status(engine: Engine) -> dict[str, dict[str, dict[str, Any]]]:
    """{variant: {factor: {verified, source, ...}}}. ``verified`` is True only with a manual confirmation."""
    with engine.connect() as c:
        rows = c.execute(select(SetupMappingConfirmation)).mappings().all()
    by = {(r["factor"], r["game_variant"]): r for r in rows}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for v in VARIANTS:
        out[v] = {}
        for f, meta in GAME_FACTORS.items():
            r = by.get((f, v))
            if r is None or r["packet_field"] != meta["packet_field"]:
                out[v][f] = {"verified": False, "packet_field": meta["packet_field"], "source": "not confirmed (no manual confirmation recorded)"}
                continue
            out[v][f] = {
                "verified": bool(r["verified"]), "packet_field": r["packet_field"],
                "source": f"manual confirmation by {r['confirmed_by']} on {str(r['confirmed_at'])[:19]}" + ("" if r["verified"] else " (rejected)"),
                "probe_report_id": r["probe_report_id"], "low_value_seen": r["low_value_seen"], "high_value_seen": r["high_value_seen"], "note": r["note"],
            }  # fmt: skip
    return out


def factors_verified(engine: Engine, factors: list[str], variant: str | None = None) -> bool:
    """True when every factor is confirmed for ``variant`` (or for every variant when the variant is unknown)."""
    st = status(engine)
    variants = [variant] if variant else list(VARIANTS)
    return all(st[v][f]["verified"] for v in variants for f in factors)
