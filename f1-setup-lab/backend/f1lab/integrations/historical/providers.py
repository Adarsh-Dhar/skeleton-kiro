"""Historical providers: a common interface, the registry (exactly OpenF1 and CSV Upload) and provenance labels."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from f1lab.integrations.historical.importer import (
    INVALID_REQUEST,
    METADATA_INVALID,
    HistoricalImportError,
    Progress,
)
from f1lab.integrations.historical.openf1_importer import import_session_openf1
from f1lab.integrations.historical.repository import HistoricalRepository

PROVENANCE_OPENF1 = "OPENF1 — external provider"
PROVENANCE_USER = "USER UPLOADED — UNVERIFIED"
PROVENANCE_DEMO = "SYNTHETIC DEMO — generated test data"
DEMO_SOURCES = frozenset({"demo", "demo_csv", "generated", "generated_csv", "simulator"})


class HistoricalProvider(Protocol):
    name: str  # "openf1" | "csv_upload"
    source_type: str  # "external_provider" | "user_uploaded"

    def run(self, repo: HistoricalRepository, payload: dict, progress: Progress | None,
            should_cancel: Callable[[], bool] | None = None, **ctx: Any) -> dict:
        """Import one session and return the stored manifest."""
        ...


class OpenF1Provider:
    """Thin wrapper: ``import_session_openf1`` is called exactly as before (``ctx`` carries ``client`` and ``force``)."""

    name = "openf1"
    source_type = "external_provider"

    def run(self, repo, payload, progress=None, should_cancel=None, *, client, force: bool = True, **_ctx) -> dict:
        return import_session_openf1(client, repo, payload, progress, should_cancel=should_cancel, force=force)


class CSVProvider:
    """Wraps ``csv_importer.import_session_csv`` (imported lazily; that module is delivered separately)."""

    name = "csv_upload"
    source_type = "user_uploaded"

    def run(self, repo, payload, progress=None, should_cancel=None, **ctx) -> dict:
        try:
            from f1lab.integrations.historical.csv_importer import import_session_csv
        except ImportError as exc:  # pragma: no cover - until csv_importer exists
            raise HistoricalImportError(INVALID_REQUEST, "the CSV Upload provider is not available") from exc
        return import_session_csv(repo, payload, progress, should_cancel, **ctx)


PROVIDERS: dict[str, HistoricalProvider] = {"openf1": OpenF1Provider(), "csv_upload": CSVProvider()}


def provenance_status(source: str) -> str:
    """The one Provenance_Status label for a source: ``openf1``, ``csv_upload`` or a demo/generated/simulator origin."""
    key = (source or "").strip().lower()
    if key == "openf1":
        return PROVENANCE_OPENF1
    if key in ("csv_upload", "user_uploaded", "upload"):
        return PROVENANCE_USER
    if key in DEMO_SOURCES:
        return PROVENANCE_DEMO
    raise ValueError(f"unknown provenance source {source!r}")


def describe_session(manifest: dict) -> dict:
    """The provenance facts every session summary and manifest carries (Req 7.9, 11.15): provider, source_type, verification,
    Data_Type_Label, Provenance_Status and Session_Metadata. ``metadata`` is returned as stored, so an Unavailable_Marker stays
    a null value named in ``metadata.unavailable``; it is None for providers without Session_Metadata (OpenF1)."""
    provider = manifest.get("provider") or "openf1"
    prov = PROVIDERS.get(provider)
    telemetry = (manifest.get("channels") or {}).get("telemetry") or {}
    label = manifest.get("data_type") or ("Full telemetry" if any(telemetry.values()) else "Lap summaries")
    return {
        "provider": provider,
        "source_type": manifest.get("source_type") or (prov.source_type if prov else None),
        "verification": manifest.get("verification") or ("verified" if provider == "openf1" else "unverified"),
        "data_type_label": label,
        "provenance_status": manifest.get("provenance_status") or provenance_status(provider),
        "metadata": manifest.get("metadata"),
    }


def guard_synthetic(source: str, synthetic: bool) -> bool:
    """Demo sources must stay synthetic: refuse ``synthetic=False`` for them, otherwise return ``synthetic``."""
    if (source or "").strip().lower() in DEMO_SOURCES and not synthetic:
        raise HistoricalImportError(METADATA_INVALID, "demo data must remain synthetic; synthetic cannot be set to false")
    return synthetic
