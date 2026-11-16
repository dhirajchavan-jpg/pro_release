"""Shared, side-effect-free helpers used by the pipeline boundaries."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from procurement.domain.classification_routes import normalize_physical_route
from procurement.domain.models import Document
from procurement.platform.object_store import download_file, put_file

_GEM_BID_PATTERN = re.compile(
    r"\bGEM\s*/\s*(20\d{2})\s*/\s*B\s*/\s*(\d{5,10})\b",
    re.IGNORECASE,
)

_PRICE_DOCUMENT_TYPES = frozenset({
    "boq_or_price_schedule",
    "price_schedule",
    "financial_bid",
    "commercial_or_price",
})

def _postgres_json_safe(value: Any) -> Any:
    """Remove NUL characters PostgreSQL JSONB cannot store."""
    if isinstance(value, str):
        return value.replace("\x00", " ")
    if isinstance(value, dict):
        return {
            _postgres_json_safe(key): _postgres_json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_postgres_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_postgres_json_safe(item) for item in value)
    return value

def _gem_bid_ids(text: str) -> set[str]:
    return {
        f"GEM/{year}/B/{number}"
        for year, number in _GEM_BID_PATTERN.findall(text or "")
    }

def _bid_tender_match(
    understanding: dict[str, Any],
    documents: list[Any],
) -> dict[str, Any]:
    expected = _gem_bid_ids(json.dumps(understanding, default=str))
    def record_value(document: Any, name: str, default: Any = "") -> Any:
        record = document.record
        if isinstance(record, dict):
            return record.get(name, default)
        return getattr(record, name, default)

    observed_by_document = {
        str(record_value(document, "document_id")): _gem_bid_ids(
            f"{record_value(document, 'file_name')}\n"
            f"{getattr(document, 'text', '')}\n"
            + "\n".join(str(getattr(page, "text", "")) for page in getattr(document, "pages", []))
        )
        for document in documents
    }
    observed = set().union(*observed_by_document.values()) if observed_by_document else set()
    shared = expected & observed
    if shared:
        return {
            "status": "matched",
            "expected_tender_ids": sorted(expected),
            "observed_tender_ids": sorted(observed),
            "reason": "The bidder package references the selected GeM tender.",
        }
    if not expected or not observed:
        return {
            "status": "not_checked",
            "expected_tender_ids": sorted(expected),
            "observed_tender_ids": sorted(observed),
            "reason": "No comparable GeM tender identifier was available in both packages.",
        }
    foreign_document_count = sum(bool(ids - expected) for ids in observed_by_document.values())
    classification = record_value(documents[0], "classification") if documents else None
    document_type = (
        classification.get("document_type", "")
        if isinstance(classification, dict)
        else getattr(classification, "document_type", "")
    )
    if foreign_document_count >= 2 or (
        len(documents) == 1
        and document_type in {"gem_bid", "rfp_or_bid_document"}
    ):
        return {
            "status": "mismatch",
            "expected_tender_ids": sorted(expected),
            "observed_tender_ids": sorted(observed),
            "reason": "The bidder package repeatedly references a different GeM tender.",
        }
    return {
        "status": "not_checked",
        "expected_tender_ids": sorted(expected),
        "observed_tender_ids": sorted(observed),
        "reason": "A different tender identifier appears only in supporting material; review it manually.",
    }

def _profile_data_from_understanding(understanding: dict[str, Any]) -> dict[str, Any]:
    profile_data = dict(understanding.get("tender_profile") or {})
    field_understanding = understanding.get("field_understanding")
    if isinstance(field_understanding, dict) and field_understanding.get("sections"):
        from procurement.understanding.service import profile_from_fields

        criteria = profile_data.get("criteria") or []
        projected = profile_from_fields(field_understanding)
        profile_data.update(
            {
                key: value
                for key, value in projected.items()
                if value not in (None, "", "Unknown", [], {})
            }
        )
        profile_data["criteria"] = criteria
    policy_audit = understanding.get("policy_audit")
    policy_audit = policy_audit if isinstance(policy_audit, dict) else {}
    compliance = understanding.get("compliance")
    compliance = compliance if isinstance(compliance, dict) else {}
    governing_policy_id = str(
        policy_audit.get("policy_override")
        or (compliance.get("policy_id") if compliance.get("status") == "completed" else "")
        or ""
    ).strip()
    if not governing_policy_id:
        from procurement.compliance.profile import policy_id_for_profile

        governing_policy_id = policy_id_for_profile(profile_data)
    if governing_policy_id:
        profile_data["governing_policy_id"] = governing_policy_id
    return profile_data

def _is_price_document_type(document_type: str) -> bool:
    value = str(document_type or "").casefold().strip()
    if "techno_commercial" in value or value.startswith("techno-commercial"):
        return False
    return value in _PRICE_DOCUMENT_TYPES

def _logical_classification_items(intake: dict[str, Any]) -> list[dict[str, Any]]:
    classification = intake.get("classification") if isinstance(intake.get("classification"), dict) else {}
    reviewed = classification.get("reviewed_logical_documents")
    if isinstance(reviewed, list) and reviewed:
        return [item for item in reviewed if isinstance(item, dict)]
    items = classification.get("logical_documents") or []
    return [item for item in items if isinstance(item, dict)]

def _logical_source_ids(item: dict[str, Any]) -> list[str]:
    ids = [str(value) for value in item.get("source_document_ids") or [] if str(value)]
    source = str(item.get("source_document_id") or "")
    if source:
        ids.append(source)
    return list(dict.fromkeys(ids))

def _logical_page_ids(item: dict[str, Any], document_id: str) -> list[str]:
    prefix = f"{document_id}-p"
    refs = [str(value) for value in item.get("page_refs") or [] if str(value).startswith(prefix)]
    start = item.get("start_page")
    end = item.get("end_page")
    if start not in {None, "", 0} and end not in {None, ""}:
        refs.extend(f"{prefix}{page}" for page in range(int(start), int(end) + 1))
    return list(dict.fromkeys(refs))

def commercial_quarantine(intake: dict[str, Any]) -> dict[str, set[str]]:
    """Drop price-only files. Keep mixed offer PDFs and strip only price slices."""
    keep_document_ids: set[str] = set()
    drop_document_ids: set[str] = set()
    drop_page_ids: set[str] = set()
    drop_logical_ids: set[str] = set()
    logical = _logical_classification_items(intake)
    route_by_id = {
        str(item.get("document_id") or ""): normalize_physical_route(item.get("broad_route"))
        for item in ((intake.get("classification") or {}).get("physical_routes") or [])
        if isinstance(item, dict)
    }
    for saved in intake.get("documents") or []:
        if not isinstance(saved, dict):
            continue
        document_id = str(saved.get("document_id") or "")
        if not document_id:
            continue
        slices = [item for item in logical if document_id in _logical_source_ids(item)]
        price_slices = [item for item in slices if _is_price_document_type(str(item.get("document_type") or ""))]
        other_slices = [item for item in slices if not _is_price_document_type(str(item.get("document_type") or ""))]
        physical_type = str((saved.get("classification") or {}).get("document_type") or "")
        physical_is_price = _is_price_document_type(physical_type) or route_by_id.get(document_id) == "commercial_price_restricted"
        if other_slices:
            keep_document_ids.add(document_id)
            for item in price_slices:
                drop_logical_ids.add(str(item.get("logical_document_id") or ""))
                drop_page_ids.update(_logical_page_ids(item, document_id))
            continue
        if price_slices or physical_is_price:
            drop_document_ids.add(document_id)
            continue
        keep_document_ids.add(document_id)
    return {
        "keep_document_ids": keep_document_ids,
        "drop_document_ids": drop_document_ids,
        "drop_page_ids": drop_page_ids,
        "drop_logical_ids": {value for value in drop_logical_ids if value},
    }

def _ocr_gap_message(notes: list[dict[str, Any]], subject: str) -> str | None:
    failed = [note for note in notes if note.get("status") == "error"]
    if not failed:
        return None
    pages = sorted(
        {
            str(note.get("page_id") or note.get("page_number") or "unknown")
            for note in failed
        }
    )
    return (
        f"{subject} OCR was unavailable for {len(failed)} page(s) "
        f"({', '.join(pages[:12])}); affected conclusions require human review."
    )

def _raise_on_ocr_errors(notes: list[dict[str, Any]], subject: str) -> None:
    if os.getenv(
        "OCR_FAILURE_POLICY",
        "fail" if os.getenv("APP_ENV", "").strip().lower() == "production" else "warn",
    ).strip().lower() != "fail":
        return
    failed = [note for note in notes if note.get("status") == "error"]
    if not failed:
        return
    if any(note.get("status") != "error" for note in notes):
        return
    pages = sorted(
        {
            str(note.get("page_id") or note.get("page_number") or "unknown")
            for note in failed
        }
    )
    raise RuntimeError(
        f"{subject} OCR failed for all {len(failed)} page(s) "
        f"({', '.join(pages[:12])}); retry after the configured OCR provider is healthy."
    )

def _preserve_ocr_uncertainty(findings: list[Any], notes: list[dict[str, Any]]) -> None:
    warning = _ocr_gap_message(notes, "Bidder")
    if not warning:
        return
    for finding in findings:
        if finding.status in {"missing", "not_evaluated"}:
            finding.status = "unclear"
            finding.reason = f"{warning} {finding.reason}"

def _store_preview_sources(
    documents: list,
    tenant_id: str,
    owner_id: str,
    storage_group: str = "source-previews",
    *,
    put: Callable[..., Any] = put_file,
) -> dict[str, dict]:
    prefix = os.getenv("OBJECT_STORAGE_PREFIX", "procurement").strip().strip("/")
    if not prefix or PurePosixPath(prefix).is_absolute() or ".." in PurePosixPath(prefix).parts:
        raise ValueError("OBJECT_STORAGE_PREFIX is invalid")
    sources = {}
    for document in documents:
        source = document.source_path
        if not source or not source.is_file():
            continue
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        checksum = digest.hexdigest()
        source_id = str(document.record.document_id)
        filename = Path(document.record.file_name).name
        suffix = source.suffix.lower() or ".bin"
        key = f"{prefix}/{tenant_id}/{storage_group}/{owner_id}/{checksum}/{source_id}{suffix}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        put(source, key, content_type=content_type)
        sources[source_id] = {
            "file_name": filename,
            "content_type": content_type,
            "object_key": key,
            "page_count": len(document.pages),
            "sha256": checksum,
        }
    return sources

def _bundle(
    documents: list[Document],
    root: Path,
    name: str,
    *,
    download: Callable[..., Any] = download_file,
) -> Path:
    if not documents:
        raise ValueError(f"{name} documents are missing")
    downloaded: list[Path] = []
    for index, document in enumerate(documents, start=1):
        destination = root / name / f"{index}-{document.file_name}"
        download(document.object_key, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != document.sha256:
            raise ValueError("stored document integrity check failed")
        downloaded.append(destination)
    if len(downloaded) == 1 and downloaded[0].suffix.lower() == ".zip":
        return downloaded[0]
    archive = root / f"{name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for document in downloaded:
            output.write(document, document.name)
    return archive

def _canonical_evidence_id_groups(evidence_ids: list[str]) -> list[dict[str, Any]]:
    """Represent runtime evidence IDs in the canonical criterion contract shape."""
    return _canonical_evidence_groups([[evidence_id] for evidence_id in evidence_ids if evidence_id])

def _canonical_evidence_groups(groups: list[list[str]]) -> list[dict[str, Any]]:
    """Represent AND-of-OR runtime groups in the canonical contract shape."""
    return [
        {
            "mode": "OR" if len(group) > 1 else "AND",
            "evidence": [{"evidence_id": evidence_id} for evidence_id in group if evidence_id],
        }
        for group in groups
        if any(group)
    ]

__all__ = [
    "_bid_tender_match",
    "_bundle",
    "_canonical_evidence_groups",
    "_canonical_evidence_id_groups",
    "_gem_bid_ids",
    "_is_price_document_type",
    "_logical_classification_items",
    "_logical_page_ids",
    "_logical_source_ids",
    "_ocr_gap_message",
    "_postgres_json_safe",
    "_preserve_ocr_uncertainty",
    "_profile_data_from_understanding",
    "_raise_on_ocr_errors",
    "_store_preview_sources",
    "commercial_quarantine",
]
