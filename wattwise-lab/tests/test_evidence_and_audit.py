"""Source evidence (claims cannot be spoofed), the data audit, and meter-file plausibility checks."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta

import pytest
from inputs import FAKE_DOCUMENT, SRC, fake_fetch, household, meter_csv, retrieved_source, tariff
from sqlalchemy import text

from wattwise.services.evidence import EvidenceError, _check_host

FLAT = [{"label": "all", "start": 0, "end": 24, "price": 5.5}]


# ------------------------------------------------------------------ evidence
def test_retrieved_document_is_archived_with_its_hash(client):
    src = retrieved_source(client)
    ev = client.get(f"/api/evidence/{src['evidence_id']}").json()
    assert ev["kind"] == "retrieved_url" and ev["sha256"] == hashlib.sha256(FAKE_DOCUMENT).hexdigest() and ev["size_bytes"] == len(FAKE_DOCUMENT)
    body = client.get(f"/api/evidence/{src['evidence_id']}/content")
    assert body.content == FAKE_DOCUMENT and "attachment" in body.headers["content-disposition"]
    t = client.post("/api/tariffs", json=tariff("Order", FLAT, source=src)).json()
    assert t["source"]["evidence"]["sha256"] == ev["sha256"] and t["source_status"]["document_retrieved"] is True
    assert any("transcribed by you" in w for w in t["source_status"]["warnings"])  # the archive does not vouch for the numbers


def test_verified_without_retrieved_evidence_is_rejected(client):
    claim = {"source_type": "official", "source_name": "Regulator", "document": "Order 12/2025", "collected_on": "2026-11-01", "verification": "verified"}
    r = client.post("/api/tariffs", json=tariff("Claimed", FLAT, source=claim))
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "evidence"


def test_evidence_sent_by_a_client_is_discarded(client):
    forged = {"evidence_id": "ev-000000000000", "kind": "retrieved_url", "sha256": "0" * 64, "retrieved_at": "2026-11-01"}
    claim = {"source_type": "official", "source_name": "Regulator", "document": "x", "collected_on": "2026-11-01", "verification": "verified",
             "evidence": forged}
    assert client.post("/api/tariffs", json=tariff("Forged", FLAT, source=claim)).status_code == 422  # no evidence_id: nothing to bind
    r = client.post("/api/tariffs", json=tariff("Forged id", FLAT, source={**claim, "evidence_id": "ev-000000000000"}))
    assert r.status_code == 422 and "does not exist" in r.json()["detail"][0]["message"]
    own = {**SRC, "evidence": forged}  # a user-supplied source with a made-up evidence block: the block is dropped
    t = client.post("/api/tariffs", json=tariff("Own", FLAT, source=own)).json()
    assert "evidence" not in t["source"] or t["source"]["evidence"] is None
    assert t["source_status"]["evidence"] is False


def test_an_attached_file_backs_user_supplied_only(client):
    b64 = base64.b64encode(b"%PDF-1.4 my electricity bill (test fixture)").decode()
    ev = client.post("/api/evidence/attach", json={"file_name": "bill.pdf", "content_b64": b64, "content_type": "application/pdf"}).json()
    assert ev["kind"] == "uploaded_file"
    claim = {"source_type": "official", "source_name": "Regulator", "document": "x", "collected_on": "2026-11-01", "verification": "verified",
             "evidence_id": ev["evidence_id"]}
    assert client.post("/api/tariffs", json=tariff("Bill claimed official", FLAT, source=claim)).status_code == 422
    t = client.post("/api/tariffs", json=tariff("My bill", FLAT, source={**SRC, "evidence_id": ev["evidence_id"]})).json()
    assert t["source_status"]["label"] == "USER SUPPLIED · DOCUMENT ATTACHED"
    assert client.post("/api/evidence/attach", json={"file_name": "x", "content_b64": "not base64!"}).status_code == 422


def test_a_stored_verified_claim_without_evidence_is_shown_as_cited(client):
    from wattwise.adapters import provenance as PV
    st = PV.status({"source_type": "official", "source_name": "R", "document": "x", "collected_on": "2026-01-01", "verification": "verified"})
    assert st["verification"] == "cited" and st["label"] == "CITED · NOT RETRIEVED"


@pytest.mark.parametrize("url", ["http://example.com/x.pdf", "https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.5/x", "ftp://example.com/x"])
def test_retrieval_refuses_non_https_and_non_public_addresses(url):
    with pytest.raises(EvidenceError):
        _check_host(url)


def test_retrieval_errors_are_reported_not_hidden(client):
    def boom(url):
        raise EvidenceError("the server answered HTTP 404 for " + url)
    client.app.state.lab.evidence.fetch = boom
    r = client.post("/api/evidence/retrieve", json={"url": "https://regulator.example/missing.pdf"})
    assert r.status_code == 422 and "404" in r.json()["detail"][0]["message"]
    client.app.state.lab.evidence.fetch = fake_fetch
    assert client.get("/api/evidence").json() == []  # nothing archived for a failed retrieval


# ------------------------------------------------------------------ meter plausibility
def _csv(values, start=datetime(2026, 3, 2)):
    return "timestamp,power_kw\n" + "\n".join(f"{(start + timedelta(minutes=15 * i)).isoformat()},{v}" for i, v in enumerate(values)) + "\n"


def test_a_constant_or_copied_meter_file_is_flagged(client):
    flat = client.post("/api/meter/import", json={"csv": _csv([0.4] * 192), "source": "x", "provenance": "real_meter"}).json()
    assert any("same value" in p for p in flat["source_record"]["plausibility"])
    day = [round(0.3 + 0.01 * (i % 37), 3) for i in range(96)]
    copied = client.post("/api/meter/import", json={"csv": _csv(day * 3, datetime(2026, 4, 1)), "source": "x", "provenance": "real_meter"}).json()
    assert any("repeat exactly" in p for p in copied["source_record"]["plausibility"])
    assert any(w.startswith("plausibility:") for w in copied["source_status"]["warnings"])
    real = client.post("/api/meter/import", json={"csv": meter_csv(), "source": "x", "provenance": "real_meter"}).json()
    assert real["source_record"]["plausibility"] == [] and real["file_sha256"] == hashlib.sha256(meter_csv().encode()).hexdigest()
    assert real["source_status"]["label"].startswith("USER SUPPLIED")


# ------------------------------------------------------------------ audit
def test_audit_of_a_clean_household(client):
    client.put("/api/household", json=household())
    a = client.get("/api/audit").json()
    assert a["ready"] is True and a["counts"]["blocking"] == 0 and a["counts"]["clean_inputs"] >= 5
    assert a["limits"] and any("cannot prove" in x for x in a["limits"])


def test_audit_lists_examples_legacy_unverified_and_legacy_experiments(client):
    from datetime import UTC

    from db.models.wattwise import Tariff
    now = datetime.now(UTC)
    with client.app.state.lab.engine.begin() as c:
        c.execute(Tariff.__table__.insert().values(tariff_id="time-of-use-old", name="old", position=9, created_at=now, updated_at=now,
                                                   data_json=json.dumps({"id": "time-of-use", "name": "x", "periods": [
                                                       {"label": "Night", "start": 23, "end": 7, "price": 0.11}, {"label": "Day", "start": 7, "end": 16, "price": 0.22},
                                                       {"label": "Peak", "start": 16, "end": 21, "price": 0.36}, {"label": "Evening", "start": 21, "end": 23, "price": 0.22}]})))
    loose = {"source_type": "unverified", "source_name": "a forum post", "verification": "unverified"}
    client.post("/api/tariffs", json=tariff("Forum", FLAT, source=loose))
    a = client.get("/api/audit").json()
    by = {(f["severity"], f["input"]) for f in a["findings"]}
    assert ("blocking", "tariff[time-of-use]") in by and ("warning", "tariff[forum]") in by
    assert a["ready"] is False


def test_audit_command_line(db_path, client):
    from wattwise.audit import main
    client.put("/api/household", json=household())
    assert main(["--db", db_path]) == 0
    with client.app.state.lab.engine.begin() as c:
        c.execute(text("DELETE FROM ww_household"))
    assert main(["--db", db_path, "--json"]) == 1  # nothing configured: blocking
