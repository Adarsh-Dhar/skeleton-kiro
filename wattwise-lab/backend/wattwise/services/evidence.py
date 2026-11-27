"""Source evidence: documents WATTWISE retrieved itself, or files the owner attached, archived with their SHA-256.

Why: a source record that merely *claims* an official document proves nothing. ``verified`` is therefore only accepted when the
record points at evidence WATTWISE retrieved from the publisher's URL itself (``retrieved_url``). The archived copy and its hash
show which document the values were transcribed from, and that it existed at that URL on that date. It does not prove the numbers
were copied correctly: WATTWISE does not read tariff orders or spec sheets, and says so in every status.

Attached files (``uploaded_file``, e.g. a PDF of your bill) are archived and hashed too, but the owner supplied them, so they
support ``user_supplied`` only.

Retrieval is HTTPS-only, refuses private/loopback/link-local addresses (also after redirects), and is limited in size and time.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select

from db.engine import immediate_transaction
from db.models.wattwise import SourceEvidence

MAX_BYTES = 10_000_000
TIMEOUT_S = 20
MAX_REDIRECTS = 5
class EvidenceError(ValueError):
    """The document could not be retrieved or attached, or a source refers to evidence that does not exist or cannot back it."""
def _check_host(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname:
        raise EvidenceError(f"only https:// documents can be retrieved (got {url!r})")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EvidenceError(f"cannot resolve {p.hostname}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise EvidenceError(f"{p.hostname} resolves to a non-public address ({ip}); only public documents can be retrieved")
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # redirects are followed by hand so every hop is checked
        return None
def https_fetch(url: str) -> dict[str, Any]:
    """GET a public https URL: {final_url, status, content_type, body}. Every redirect hop is checked against private addresses."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _check_host(current)
        req = urllib.request.Request(current, headers={"User-Agent": "WATTWISE-LAB evidence retrieval (+source archive)"})
        try:
            resp = opener.open(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urllib.parse.urljoin(current, exc.headers["Location"])
                continue
            raise EvidenceError(f"the server answered HTTP {exc.code} for {current}") from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            raise EvidenceError(f"could not retrieve {current}: {exc}") from exc
        with resp:
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise EvidenceError(f"the document is larger than {MAX_BYTES // 1_000_000} MB")
            return {"final_url": resp.geturl(), "status": resp.status, "content_type": resp.headers.get("Content-Type"), "body": body}
    raise EvidenceError(f"more than {MAX_REDIRECTS} redirects for {url}")
def _view(e: SourceEvidence) -> dict[str, Any]:
    return {"evidence_id": e.evidence_id, "kind": e.kind, "url": e.url, "final_url": e.final_url, "http_status": e.http_status,
            "content_type": e.content_type, "file_name": e.file_name, "size_bytes": e.size_bytes, "sha256": e.sha256,
            "retrieved_at": e.retrieved_at.isoformat() if e.retrieved_at else None}
class EvidenceStore:
    def __init__(self, engine: Engine, fetch: Callable[[str], dict[str, Any]] = https_fetch) -> None:
        self.engine = engine
        self.fetch = fetch  # replaceable in tests (they never contact the internet)
    def _insert(self, **values: Any) -> dict[str, Any]:
        eid = "ev-" + uuid.uuid4().hex[:12]
        with immediate_transaction(self.engine) as c:
            c.execute(SourceEvidence.__table__.insert().values(evidence_id=eid, retrieved_at=datetime.now(UTC), **values))
        return self.get(eid)
    def retrieve(self, url: str) -> dict[str, Any]:
        """WATTWISE downloads the document itself and archives it. This is the only evidence that can back ``verified``."""
        url = (url or "").strip()
        if len(url) > 500:
            raise EvidenceError("the URL is longer than 500 characters")
        got = self.fetch(url)
        body = got["body"]
        if not body:
            raise EvidenceError(f"{url} returned an empty document")
        return self._insert(kind="retrieved_url", url=url, final_url=str(got.get("final_url") or url)[:500], http_status=got.get("status"),
                            content_type=(got.get("content_type") or "")[:120] or None, file_name=None, size_bytes=len(body),
                            sha256=hashlib.sha256(body).hexdigest(), content=body)
    def attach(self, file_name: str, content_b64: str, content_type: str | None = None) -> dict[str, Any]:
        """Archive a file the owner supplies (bill, spec-sheet PDF). Supports ``user_supplied`` only."""
        try:
            body = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise EvidenceError("the file content must be base64") from exc
        if not body:
            raise EvidenceError("the file is empty")
        if len(body) > MAX_BYTES:
            raise EvidenceError(f"the file is larger than {MAX_BYTES // 1_000_000} MB")
        return self._insert(kind="uploaded_file", url=None, final_url=None, http_status=None, content_type=(content_type or "")[:120] or None,
                            file_name=(file_name or "document")[:300], size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), content=body)
    def get(self, evidence_id: str) -> dict[str, Any]:
        from sqlalchemy.orm import Session
        with Session(self.engine) as s:
            e = s.get(SourceEvidence, evidence_id)
            if e is None:
                raise KeyError(evidence_id)
            return _view(e)
    def content(self, evidence_id: str) -> tuple[bytes, str | None, str]:
        with self.engine.connect() as c:
            row = c.execute(select(SourceEvidence.content, SourceEvidence.content_type, SourceEvidence.file_name, SourceEvidence.sha256)
                            .where(SourceEvidence.evidence_id == evidence_id)).first()
        if row is None:
            raise KeyError(evidence_id)
        safe = "".join(ch for ch in (row[2] or "") if ch.isalnum() or ch in "._- ")[:120].strip()
        return row[0], row[1], safe or f"{row[3][:12]}.bin"
    def list(self) -> list[dict[str, Any]]:
        from sqlalchemy.orm import Session
        with Session(self.engine) as s:
            return [_view(e) for e in s.scalars(select(SourceEvidence).order_by(SourceEvidence.retrieved_at.desc()))]
    # ------------------------------------------------------------------ binding evidence to source records
    def bind(self, src: dict[str, Any] | None) -> dict[str, Any] | None:
        """The stored form of a source record: ``evidence`` is filled in by the server from ``evidence_id`` (anything a client sent
        as ``evidence`` is discarded). ``verified`` requires evidence WATTWISE retrieved itself."""
        if not src:
            return src
        out = {k: v for k, v in src.items() if k != "evidence"}
        eid = out.get("evidence_id")
        if eid:
            try:
                ev = self.get(eid)
            except KeyError:
                raise EvidenceError(f"source evidence {eid} does not exist") from None
            out["evidence"] = {k: ev[k] for k in ("evidence_id", "kind", "url", "final_url", "sha256", "size_bytes", "retrieved_at", "file_name")}
        if out.get("verification") == "verified":
            ev = out.get("evidence")
            if not ev or ev["kind"] != "retrieved_url":
                raise EvidenceError("'verified' needs the document retrieved by WATTWISE itself: retrieve the publisher's URL first "
                                    "(POST /api/evidence/retrieve) and give its evidence_id; otherwise use 'user_supplied' or 'unverified'")
        return out
    def bind_all(self, obj: Any) -> Any:
        """Bind every source record inside a request body (tariffs, EV, charger, appliances, base load)."""
        if isinstance(obj, dict):
            if "source_type" in obj and "verification" in obj:
                return self.bind(obj)
            return {k: self.bind_all(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.bind_all(v) for v in obj]
        return obj
