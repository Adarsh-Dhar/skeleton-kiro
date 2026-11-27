"""Bill reconciliation, retiring experiments on example inputs, and the sign-off criteria of the data audit."""
from __future__ import annotations

from datetime import datetime, timedelta

from inputs import SRC, household, inputs, tariff

BILL = {"source_type": "user_provided", "source_name": "household owner", "document": "bill for March 2026 (test fixture)", "verification": "user_supplied"}
OPTS = {"timestamp_column": "timestamp", "value_column": "kwh", "reading_kind": "interval_energy", "unit": "kWh", "timezone": "UTC"}


def month_csv(days=31, start=datetime(2026, 3, 1)):
    """Quarter-hour energy readings that vary by slot and by day (not a pattern real meters never produce)."""
    rows = ["timestamp,kwh"]
    for i in range(days * 96):
        rows.append(f"{(start + timedelta(minutes=15 * i)).isoformat()},{0.071 + 0.013 * ((i * 7) % 11) / 10 + 0.0017 * (i // 96):.4f}")
    return "\n".join(rows) + "\n"


def imported(client, days=31):
    r = client.post("/api/meter/import", json={"csv": month_csv(days), "source": "DISCOM portal (test fixture)", "provenance": "real_meter", "options": OPTS})
    assert r.status_code == 201, r.text
    return r.json()


def reconcile(client, **kw):
    body = {"import_id": kw.pop("import_id"), "tariff_id": "billed", "period_start": "2026-03-01", "period_end": "2026-03-31",
            "billed_kwh": 100, "billed_amount": 100, "bill_source": BILL, **kw}
    return client.post("/api/reconcile", json=body)


def test_a_bill_that_matches_the_priced_meter_readings_passes(client):
    client.post("/api/tariffs", json=tariff("Billed", [{"label": "all", "start": 0, "end": 24, "price": 6.0}], fixed_monthly_charge=60.0, billing_days=31))
    m = imported(client)
    probe = reconcile(client, import_id=m["import_id"]).json()  # learn what the readings price to
    kwh, amount = probe["computed"]["kwh"], probe["computed"]["amount"]
    assert probe["coverage"] == 1.0 and amount == round(kwh * 6.0 + 60.0, 2)  # energy + one month's fixed charge
    ok = reconcile(client, import_id=m["import_id"], billed_kwh=kwh, billed_amount=amount).json()
    assert ok["passed"] is True and ok["reasons"] == [] and ok["difference_pct"] == {"kwh": 0.0, "amount": 0.0}
    assert ok["billed"]["source_status"]["label"] == "USER SUPPLIED" and ok["meter_source"]["source_type"] == "meter_export"
    off = reconcile(client, import_id=m["import_id"], billed_kwh=kwh, billed_amount=amount * 1.10).json()
    assert off["passed"] is False and any("on the bill" in r for r in off["reasons"])
    assert [r["passed"] for r in client.get("/api/reconciliations").json()] == [False, True, False]


def test_a_billing_period_the_readings_do_not_cover_cannot_pass(client):
    client.post("/api/tariffs", json=tariff("Billed", [{"label": "all", "start": 0, "end": 24, "price": 6.0}]))
    m = imported(client, days=20)
    probe = reconcile(client, import_id=m["import_id"]).json()
    r = reconcile(client, import_id=m["import_id"], billed_kwh=probe["computed"]["kwh"], billed_amount=probe["computed"]["amount"]).json()
    assert r["passed"] is False and r["coverage"] < 0.7 and any("cover" in x and "2026-03-21" in x for x in r["reasons"])


def test_reconciliation_refuses_example_tariffs_and_bad_periods(client):
    m = imported(client, days=2)
    import json
    from datetime import UTC

    from db.models.wattwise import Tariff
    now = datetime.now(UTC)
    with client.app.state.lab.engine.begin() as c:
        c.execute(Tariff.__table__.insert().values(tariff_id="flat-old", name="x", position=9, created_at=now, updated_at=now,
                                                   data_json=json.dumps({"id": "flat-rate", "name": "x", "periods": [{"label": "All day", "start": 0, "end": 24, "price": 0.25}]})))
    client.post("/api/tariffs", json=tariff("Billed", [{"label": "all", "start": 0, "end": 24, "price": 6.0}]))
    assert reconcile(client, import_id=m["import_id"], period_end="2026-02-01").status_code == 422
    assert reconcile(client, import_id="mi-nope").status_code == 404
    assert reconcile(client, import_id=m["import_id"], bill_source={**SRC, "verification": "verified"}).status_code == 422


def test_retiring_an_experiment_hides_it_without_deleting_it(client):
    d = client.post("/api/experiments", json=inputs(name="to retire", simulator={"latency_seconds": 0}, design={"method": "ofat"})).json()
    r = client.post(f"/api/experiments/{d['id']}/retire").json()
    assert r["retired"] == d["id"] and client.get("/api/experiments").json() == [] and client.get(f"/api/experiments/{d['id']}").status_code == 404
    assert client.get("/api/experiments-retired").json() == [{"id": d["id"]}]
    assert client.post("/api/experiments", json=inputs(name="same id", experiment_id=d["id"])).status_code == 409  # the id stays reserved
    assert client.post(f"/api/experiments/{d['id']}/unretire").json() == {"restored": d["id"]}
    assert client.get(f"/api/experiments/{d['id']}").json()["name"] == "to retire"


def test_sign_off_passes_only_when_every_criterion_holds(client):
    def signoff():
        return {c["criterion"]: c["passed"] for c in client.get("/api/audit").json()["signoff"]["criteria"]}

    client.put("/api/household", json=household())
    s = signoff()
    assert s["no blocking findings (no example, synthetic or missing input)"] is True
    assert not all(s.values())  # no meter export, no reconciliation, no experiment yet
    m = imported(client)
    probe = client.post("/api/reconcile", json={"import_id": m["import_id"], "tariff_id": "time-of-use", "period_start": "2026-03-01", "period_end": "2026-03-31",
                                                "billed_kwh": 1, "billed_amount": 1, "bill_source": BILL}).json()
    client.post("/api/reconcile", json={"import_id": m["import_id"], "tariff_id": "time-of-use", "period_start": "2026-03-01", "period_end": "2026-03-31",
                                        "billed_kwh": probe["computed"]["kwh"], "billed_amount": probe["computed"]["amount"], "bill_source": BILL})
    client.post("/api/experiments", json=inputs(name="confirmed inputs", simulator={"latency_seconds": 0}, design={"method": "ofat"}))
    audit = client.get("/api/audit").json()
    assert audit["signoff"]["passed"] is True, audit["signoff"]
    from wattwise.audit import main
    assert main(["--db", str(client.app.state.lab.engine.url.database)]) == 0


def test_an_unverified_tariff_never_satisfies_the_sign_off(client):
    """A fictional or untraceable tariff can be used for a demo, but it can never count as the household's real tariff."""
    loose = {"source_type": "unverified", "source_name": "demo pack (fictional)", "verification": "unverified"}
    t = client.post("/api/tariffs", json=tariff("Fictional", [{"label": "all", "start": 0, "end": 24, "price": 3.0}], source=loose)).json()
    client.put("/api/household", json=household(tariff_id=t["id"]))
    crit = {c["criterion"]: c["passed"] for c in client.get("/api/audit").json()["signoff"]["criteria"]}
    assert crit["the selected tariff is from your bill (user supplied) or a retrieved official document, and not expired"] is False
    assert crit["no input is unverified or cites a document WATTWISE never retrieved"] is False
