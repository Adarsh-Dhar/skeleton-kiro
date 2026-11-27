"""Bill reconciliation: price one billing period from your imported meter readings with your tariff, and compare it with the
kWh and the amount printed on the real bill for that period.

This is the check that a configured tariff and a meter export actually describe your bill. It only passes when the meter
readings cover the whole billing period (missing days would undercount fixed charges and energy) and both the kWh and the
amount are within the tolerance. A bill itself is evidence you supply: give its source (and attach the PDF if you like).
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.engine import immediate_transaction
from db.models.wattwise import BillReconciliation
from wattwise.adapters import provenance as PV
from wattwise.adapters import tariff_model as TM
from wattwise.services.baseline import _split_local
from wattwise.services.meter_library import usable_as_baseline

MIN_COVERAGE = 0.99
def _view(r: BillReconciliation) -> dict[str, Any]:
    return {"reconciliation_id": r.reconciliation_id, "created_at": r.created_at.isoformat() if r.created_at else None, **json.loads(r.result_json)}
def reconcile(lab, req) -> dict[str, Any]:
    meta = lab.meters.get(req.import_id)  # NotFound for an unknown import
    usable_as_baseline(meta)  # a synthetic demo file can never reconcile a real bill
    tariff = lab.resolve_tariff(req.tariff_id, None)  # example tariffs are refused here
    start, end = req.period_start, req.period_end
    if end < start:
        raise ValueError("period_end is before period_start")
    days = (end - start).days + 1
    if days > 400:
        raise ValueError("a billing period longer than 400 days is not supported")
    tz = ZoneInfo(meta["timezone"])
    pieces = [(local, kwh, kw, minutes) for local, kwh, kw, minutes in _split_local(lab.meters.readings(req.import_id), tz)
              if start <= local.date() <= end]
    covered = sum(m for *_x, m in pieces)
    coverage = covered / (days * 1440)
    b = TM.bill(tariff, [(local, kwh, kw) for local, kwh, kw, _m in pieces])
    # bill() counts fixed charges for the days that have readings; a billing period charges every calendar day
    if b["days"] != days:
        parts = TM.finish({**tariff, "demand_charge_per_kw": 0.0}, b["energy_charge"], b["export_credit"], 0.0, days, single_day=False)
        b = {**b, "fixed_charge": parts["fixed_charge"], "total": b["total"] - b["fixed_charge"] + parts["fixed_charge"]}
    kwh, amount = b["import_kwh"], b["total"]
    kwh_diff = None if not req.billed_kwh else 100 * (kwh - req.billed_kwh) / req.billed_kwh
    amt_diff = None if not req.billed_amount else 100 * (amount - req.billed_amount) / req.billed_amount
    reasons: list[str] = []
    if coverage < MIN_COVERAGE:
        missing = [str(start + timedelta(days=i)) for i in range(days) if not any(p[0].date() == start + timedelta(days=i) for p in pieces)]
        reasons.append(f"the meter readings cover {coverage:.1%} of the billing period (need {MIN_COVERAGE:.0%})"
                       + (f"; days with no readings: {', '.join(missing[:6])}{'...' if len(missing) > 6 else ''}" if missing else ""))
    if kwh_diff is None or abs(kwh_diff) > req.tolerance_pct:
        reasons.append(f"metered {kwh:.2f} kWh vs {req.billed_kwh:g} kWh on the bill ({kwh_diff:+.2f}%)" if kwh_diff is not None else "billed kWh is 0")
    if amt_diff is None or abs(amt_diff) > req.tolerance_pct:
        reasons.append(f"priced {amount:.2f} vs {req.billed_amount:g} on the bill ({amt_diff:+.2f}%)" if amt_diff is not None else "billed amount is 0")
    passed = not reasons
    bill_source = lab.evidence.bind(req.bill_source.model_dump(mode="json"))
    result = {
        "import_id": req.import_id, "tariff_id": tariff["id"], "tariff_name": tariff["name"], "period_start": start.isoformat(), "period_end": end.isoformat(),
        "days": days, "coverage": round(coverage, 4), "tolerance_pct": req.tolerance_pct,
        "billed": {"kwh": req.billed_kwh, "amount": req.billed_amount, "source": bill_source, "source_status": PV.status(bill_source)},
        "computed": {"kwh": round(kwh, 3), "amount": round(amount, 2), "energy_charge": round(b["energy_charge"], 2), "fixed_charge": round(b["fixed_charge"], 2),
                     "demand_charge": round(b["demand_charge"], 2), "tax": round(b["tax"], 2), "export_credit": round(b["export_credit"], 2),
                     "cost_by_period": {k: {a: round(v, 3) for a, v in x.items()} for k, x in b["cost_by_period"].items()}},
        "difference_pct": {"kwh": None if kwh_diff is None else round(kwh_diff, 2), "amount": None if amt_diff is None else round(amt_diff, 2)},
        "passed": passed, "reasons": reasons,
        "meter_source": meta.get("source_record"), "tariff_source": tariff.get("source"),
        "note": "a pass shows that your tariff and meter export reproduce this bill; check another period before relying on projections",
    }  # fmt: skip
    rid = "rc-" + uuid.uuid4().hex[:10]
    with immediate_transaction(lab.engine) as c:
        c.execute(BillReconciliation.__table__.insert().values(
            reconciliation_id=rid, created_at=datetime.now(UTC), import_id=req.import_id, tariff_id=tariff["id"], period_start=start.isoformat(),
            period_end=end.isoformat(), billed_kwh=req.billed_kwh, billed_amount=req.billed_amount, computed_kwh=kwh, computed_amount=amount,
            coverage=coverage, passed=int(passed), result_json=json.dumps(result)))
    return {"reconciliation_id": rid, **result}
def list_all(lab) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session
    with Session(lab.engine) as s:
        return [_view(r) for r in s.scalars(select(BillReconciliation).order_by(BillReconciliation.created_at.desc()))]
