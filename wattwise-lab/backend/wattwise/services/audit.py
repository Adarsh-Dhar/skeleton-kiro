"""Data audit: every stored input checked against the zero-fabricated-input-data policy.

Findings have a severity:
  blocking  the value is an example or synthetic, or a required input is missing: no projection uses it
  confirm   the owner's value saved before source tracking: still used, but it must be checked against its source
  warning   unverified, cited but not retrieved, expired, or a meter file with implausible readings
  info      what is fine (counted, not listed one by one)
The audit only reads. ``python -m wattwise.audit`` prints the same report for a database file.
"""
from __future__ import annotations

from typing import Any

from wattwise.adapters import provenance as PV


def _finding(severity: str, item: str, message: str, action: str) -> dict[str, str]:
    return {"severity": severity, "input": item, "message": message, "action": action}
def _check_source(out: list, item: str, src: dict[str, Any] | None) -> bool:
    """Append findings for one source record; True when it is clean."""
    st = PV.status(src)
    if not src:
        out.append(_finding("blocking", item, "no source recorded", "add where the value comes from"))
        return False
    if not st["usable"]:
        out.append(_finding("blocking", item, "built-in example, not real data", "replace it with your own value, or delete it"))
        return False
    clean = True
    if src.get("legacy"):
        out.append(_finding("confirm", item, "saved before source tracking", "check the value against your bill, label or vehicle documents and confirm it"))
        clean = False
    if st["verification"] == "cited":
        out.append(_finding("warning", item, "claims 'verified' but WATTWISE never retrieved the document", "retrieve the publisher's URL, or mark it user supplied"))
        clean = False
    elif st["verification"] == "unverified":
        out.append(_finding("warning", item, "unverified: no traceable source", "add the document or URL and the date you read it"))
        clean = False
    if st["expired"]:
        out.append(_finding("warning", item, f"expired (effective until {src.get('effective_to')})", "enter the current tariff order or document"))
        clean = False
    for p in src.get("plausibility") or []:
        out.append(_finding("warning", item, f"implausible readings: {p}", "check that this is an unedited export from your meter or utility"))
        clean = False
    return clean
def run(lab) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    clean = 0
    state = lab.households.get()
    s = state["settings"]
    for ex in state.get("example_values") or []:
        findings.append(_finding("blocking", ex["input"], ex["message"], "if this really is your value, enter it again with its source; otherwise ignore it"))
    for t in lab.tariffs.list():
        clean += _check_source(findings, f"tariff[{t['id']}]", t.get("source"))
    if s.get("ev"):
        clean += _check_source(findings, "ev", s["ev"].get("source"))
    if state.get("ev_draft"):
        d = state["ev_draft"]
        if d.get("example"):
            pass  # already listed in example_values
        else:
            findings.append(_finding("blocking", "ev", f"incomplete EV draft (missing {', '.join(d.get('missing') or [])})", "complete the EV in Household setup"))
    if s.get("charger_power_kw") is not None:
        clean += _check_source(findings, "charger", s.get("charger_source"))
    for a in s.get("appliances") or []:
        clean += _check_source(findings, f"appliances[{a['name']}]", a.get("source"))
    if s.get("base_load_kw") is not None:
        clean += _check_source(findings, "base_load", s.get("base_load_source"))
    for m in lab.meters.list():
        item = f"meter_import[{m['import_id']}]"
        if m["source_label"] == "synthetic_demo":
            findings.append(_finding("blocking", item, "synthetic demo meter data", "delete it and import your own meter export"))
            continue
        clean += _check_source(findings, item, m.get("source_record"))
    for e in lab.list():
        if e.get("legacy_inputs"):
            findings.append(_finding("blocking", f"experiment[{e['id']}]", "built on example inputs before the data policy",
                                     "ignore its results; create a new experiment from your configured inputs"))
    ready = lab.readiness(s)
    for m in ready["missing"]:
        findings.append(_finding("blocking", m["field"], m["message"], "enter it in Household setup"))
    order = {"blocking": 0, "confirm": 1, "warning": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    counts = {k: sum(f["severity"] == k for f in findings) for k in order}
    # sign-off: the real-data-only acceptance, criterion by criterion (all must pass)
    from wattwise.services.reconciliation import list_all
    meters = [m for m in lab.meters.list() if m["source_label"] == "real_meter" and not (m.get("source_record") or {}).get("plausibility")]
    recs = [r for r in list_all(lab) if r["passed"] and r["tariff_id"] == s.get("tariff_id") and any(m["import_id"] == r["import_id"] for m in meters)]
    new_exps = [e for e in lab.list() if not e.get("legacy_inputs")]
    sel = next((t for t in lab.tariffs.list() if t["id"] == s.get("tariff_id")), None)
    sel_ok = bool(sel) and sel["source_status"]["verification"] in ("user_supplied", "verified") and not sel["source_status"]["expired"] and ready["ready"]
    untraced = [f for f in findings if f["severity"] == "warning" and ("unverified" in f["message"] or "never retrieved" in f["message"])]
    signoff = [
        {"criterion": "no blocking findings (no example, synthetic or missing input)", "passed": counts["blocking"] == 0},
        {"criterion": "no unconfirmed values saved before source tracking", "passed": counts["confirm"] == 0},
        {"criterion": "the selected tariff is from your bill (user supplied) or a retrieved official document, and not expired", "passed": sel_ok},
        {"criterion": "no input is unverified or cites a document WATTWISE never retrieved", "passed": not untraced},
        {"criterion": "at least one meter export declared real, with no plausibility warning", "passed": bool(meters)},
        {"criterion": "a billing period reconciles with the real bill (selected tariff, that meter export)", "passed": bool(recs),
         "evidence": [r["reconciliation_id"] for r in recs]},
        {"criterion": "no experiment on example inputs remains in use", "passed": not any(e.get("legacy_inputs") for e in lab.list())},
        {"criterion": "an experiment created from the confirmed inputs", "passed": bool(new_exps), "evidence": [e["id"] for e in new_exps]},
    ]
    return {"ready": ready["ready"], "counts": {**counts, "clean_inputs": clean}, "findings": findings,
            "signoff": {"passed": all(c["passed"] for c in signoff), "criteria": signoff},
            "limits": ["WATTWISE records where each value comes from; it cannot prove that a file or a typed value is genuine.",
                       "A document retrieved by WATTWISE shows which document a value was taken from, not that it was copied correctly.",
                       "Meter files are checked for implausible patterns only; a careful forgery would pass."]}
