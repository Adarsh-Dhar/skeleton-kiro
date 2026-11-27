"""Run the WATTWISE workflow on the SYNTHETIC demo pack, in a separate sandbox database (never the real one).

Steps: tariff from the demo bill -> import the demo meter file -> dishwasher from the demo label -> household (meter baseline,
no EV) -> reconcile October 2026 against the demo bill (447 kWh, and the pack's misprinted 376 kWh) -> experiment -> audit.
Every input is labelled unverified / fictional, so the real-data sign-off is expected to FAIL.

    ../.venv/bin/python run_demo.py [--serve]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "sandbox.db"
sys.path.insert(0, str(HERE.parent / "backend"))

DEMO = {"source_type": "unverified", "source_name": "WATTWISE demo pack (fictional)", "document": "01_sample_bill_DEMO.md",
        "verification": "unverified", "notes": "SYNTHETIC DEMO DATA - not real; for the sandbox only"}
TARIFF = {
    "name": "DEMO LT Domestic (fictional)", "utility": "Demo State Power Distribution Company (fictional)", "consumer_category": "LT Domestic - DEMO ONLY",
    "currency": "INR", "source": {**DEMO, "source_name": "Demo State Power Distribution Company (fictional)"},
    "periods": [{"label": "all day", "start": 0, "end": 24, "price": 3.00}],
    "tiers": [{"up_to_kwh": 100, "adder_per_kwh": 0.0}, {"up_to_kwh": 200, "adder_per_kwh": 1.50}, {"up_to_kwh": None, "adder_per_kwh": 3.00}],
    "tier_basis": "billing_period", "fixed_monthly_charge": 120.00, "billing_days": 31, "surcharge_per_kwh": 0.25, "tax_pct": 5,
}  # fmt: skip
METER_OPTS = {"timestamp_column": "timestamp", "value_column": "energy_kwh", "reading_kind": "interval_energy", "unit": "kWh", "timezone": "Asia/Kolkata"}
# the demo label: 1.6 kWh per 2-hour cycle -> 0.8 kW average (not the 1.2 kW rating; see docs/DATA_SOURCES.md, appliances)
DISHWASHER = {"name": "Dishwasher (DEMO DW-1200-SAMPLE)", "power_kw": 0.8, "duration_h": 2.0, "flexible": True, "fixed_start": 20.0,
              "in_baseline": True, "source": {**DEMO, "source_name": "DEMO APPLIANCE WORKS (fictional)", "notes": "demo label: 1.6 kWh/cycle, 2 h"}}


def ok(r, code=200):
    if r.status_code != code:
        raise SystemExit(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:400]}")
    return r.json()


def run(c) -> dict:
    out: dict = {"database": str(DB), "warning": "SYNTHETIC DEMO DATA - NOT REAL"}
    t = ok(c.post("/api/tariffs", json=TARIFF), 201)
    out["tariff"] = {"id": t["id"], "status": t["source_status"]["label"]}
    csv = (HERE / "02_demo_meter_readings_30min.csv").read_text()
    m = ok(c.post("/api/meter/import", json={"csv": csv, "file_name": "02_demo_meter_readings_30min.csv", "source": "WATTWISE demo pack (synthetic)",
                                             "provenance": "unknown", "options": METER_OPTS}), 201)
    out["meter"] = {"import_id": m["import_id"], "rows": m["rows_used"], "total_kwh": round(m["total_import_kwh"], 2), "first": m["first_utc"],
                    "last": m["last_utc"], "status": m["source_status"]["label"], "plausibility": m["source_record"]["plausibility"]}
    cal = ok(c.get(f"/api/meter/imports/{m['import_id']}/calibration"))
    night = [v for i, v in enumerate(cal["base_profile_kw"]) if 32 <= i < 44]  # 00:00-02:59 in the 16:00-based window
    out["meter"]["measured_night_kw"] = round(sum(night) / len(night), 3)
    hh = ok(c.put("/api/household", json={"tariff_id": t["id"], "baseline_import_id": m["import_id"], "appliances": [DISHWASHER], "ev": None}))
    out["household"] = {"ready": hh["readiness"]["ready"], "missing": hh["readiness"]["missing"], "current_schedule": hh["current_schedule"]}
    rec = {}
    for label, kwh in (("bill_447_kwh", 447), ("pack_misprint_376_kwh", 376)):
        r = ok(c.post("/api/reconcile", json={"import_id": m["import_id"], "tariff_id": t["id"], "period_start": "2026-10-01", "period_end": "2026-10-31",
                                              "billed_kwh": kwh, "billed_amount": 2586.94, "bill_source": DEMO}), 201)
        rec[label] = {k: r[k] for k in ("passed", "coverage", "difference_pct", "reasons")} | {"computed": {k: r["computed"][k] for k in (
            "kwh", "amount", "energy_charge", "fixed_charge", "tax")}}
    out["reconciliation"] = rec
    body = {"name": "DEMO: when to run the dishwasher", "tariff_id": t["id"], "baseline_import_id": m["import_id"], "appliances": [DISHWASHER], "ev": None,
            "factors": {"appliance_start": {"low": 18, "high": 30}}, "design": {"method": "ofat", "levels": 5}, "simulator": {"latency_seconds": 0}}
    d = ok(c.post("/api/experiments", json=body), 201)
    ok(c.post(f"/api/experiments/{d['id']}/execute"), 202)
    for _ in range(200):
        d = ok(c.get(f"/api/experiments/{d['id']}"))
        if d["counts"]["queued"] + d["counts"]["running"] == 0:
            break
        time.sleep(0.05)
    a = ok(c.post(f"/api/experiments/{d['id']}/analysis", json={}))
    out["experiment"] = {"id": d["id"], "runs": d["counts"]["completed"], "baseline_check": d["baseline_check"]["status"],
                         "today_cost_per_day": d["baseline_cost"], "best": d["best"] and {k: d["best"][k] for k in ("schedule", "cost", "saving_pct")},
                         "headline": a["recommendation"]["headline"]}
    audit = ok(c.get("/api/audit"))
    out["audit"] = {"counts": audit["counts"], "signoff_passed": audit["signoff"]["passed"],
                    "criteria": [(x["criterion"], x["passed"]) for x in audit["signoff"]["criteria"]],
                    "findings": [f"{f['severity']}: {f['input']} - {f['message']}" for f in audit["findings"]]}
    return out


def report_md(o: dict) -> str:
    r447, r376 = o["reconciliation"]["bill_447_kwh"], o["reconciliation"]["pack_misprint_376_kwh"]
    lines = ["# WATTWISE demo sandbox run: SYNTHETIC DATA, NOT REAL", "", f"Database: `{o['database']}` (not the real installation).", "",
             "| Step | Result |", "|---|---|",
             f"| Tariff from the demo bill | `{o['tariff']['id']}`, {o['tariff']['status']} |",
             f"| Meter import | {o['meter']['rows']} rows, {o['meter']['total_kwh']} kWh, {o['meter']['first'][:16]} to {o['meter']['last'][:16]} UTC, {o['meter']['status']}; "
             f"measured 00:00-03:00 load {o['meter']['measured_night_kw']} kW |",
             f"| Household | ready: {o['household']['ready']} |",
             f"| Reconcile 447 kWh / Rs 2586.94 | **{'PASSED' if r447['passed'] else 'FAILED'}**: priced {r447['computed']['kwh']} kWh, Rs {r447['computed']['amount']} "
             f"({r447['difference_pct']['kwh']:+.2f}% kWh, {r447['difference_pct']['amount']:+.2f}% amount) |",
             f"| Reconcile 376 kWh (pack misprint) | **{'PASSED' if r376['passed'] else 'FAILED'}**: {'; '.join(r376['reasons'])} |",
             f"| Experiment | {o['experiment']['runs']} runs, baseline check {o['experiment']['baseline_check']}; {o['experiment']['headline']} |",
             f"| Audit sign-off | **{'PASSED' if o['audit']['signoff_passed'] else 'NOT PASSED'}** (expected: the data is fictional) |", "",
             "## Sign-off criteria", "", *[f"- [{'x' if p else ' '}] {c}" for c, p in o["audit"]["criteria"]], "",
             "## Audit findings", "", *[f"- {f}" for f in o["audit"]["findings"]], ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="after the run, serve the sandbox on http://127.0.0.1:8092")
    a = ap.parse_args()
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB) + suffix).unlink(missing_ok=True)  # the sandbox is rebuilt from scratch every run
    from fastapi.testclient import TestClient

    from wattwise.api.app import create_app
    with TestClient(create_app(str(DB))) as c:
        out = run(c)
    (HERE / "demo-report.json").write_text(json.dumps(out, indent=2))
    (HERE / "demo-report.md").write_text(report_md(out))
    print(report_md(out))
    if a.serve:
        import uvicorn
        uvicorn.run(create_app(str(DB)), host="127.0.0.1", port=8092, log_level="warning")


if __name__ == "__main__":
    main()
