"""Visual signature and stamp verification for bidder evidence."""

from __future__ import annotations

import os
from concurrent.futures import Future
from threading import Lock
from typing import Any, Literal

from procurement.domain.schemas import (
    DocumentVerification,
    EvidenceMapEntry,
    ExtractedDocument,
    PageUnit,
)
from procurement.evaluation.verification import signature_stamp_signal, status_from_check_results
from procurement.platform.llm import (
    AI_VERIFIER_SUPPORTS_VISION,
    GEMINI_MODEL_FAST,
    GEMINI_THINKING_BUDGET,
    PROCUREMENT_OFFLINE_LLM,
    gemini_api_key,
    parse_json_object,
    post_gemini_content,
)
from procurement.platform.prompts import prompt_text
from procurement.understanding.rendering import render_pdf_page_png_b64


VISUAL_SIGNATURE_ENABLED = os.getenv("VISUAL_SIGNATURE_ENABLED", "1") != "0"
VISUAL_SIGNATURE_MAX_PAGES = int(os.getenv("VISUAL_SIGNATURE_MAX_PAGES", "3"))


def apply_visual_signature_checks(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
    snippets: list[str],
) -> None:
    check_names = {check.lower() for check in entry.checks} | {check.lower() for check in verification.check_results}
    needs_signature = any("sign" in check for check in check_names)
    needs_stamp = any("stamp" in check or "seal" in check for check in check_names)
    if not (needs_signature or needs_stamp):
        return
    signal = signature_stamp_signal("\n".join(snippets))
    if needs_signature and verification.check_results.get("signed") == "pass" and signal != "strong":
        verification.check_results["signed"] = "unclear"
    if needs_stamp and verification.check_results.get("stamped") == "pass" and signal != "strong":
        verification.check_results["stamped"] = "unclear"
    if needs_signature and "signed" not in verification.check_results and signal == "placeholder":
        verification.check_results["signed"] = "unclear"
    if needs_stamp and "stamped" not in verification.check_results and signal == "placeholder":
        verification.check_results["stamped"] = "unclear"
    verification.status = status_from_check_results(verification.status, verification.check_results)


def visual_signature_agent(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
    page_source_by_id: dict[str, tuple[ExtractedDocument, PageUnit]],
    *,
    inspection_cache: dict[str, Future[dict[str, Any]]] | None = None,
    cache_lock: Lock | None = None,
) -> None:
    if not VISUAL_SIGNATURE_ENABLED or not AI_VERIFIER_SUPPORTS_VISION or PROCUREMENT_OFFLINE_LLM:
        return
    check_names = {check.lower() for check in entry.checks} | {check.lower() for check in verification.check_results}
    needs_signature = any("sign" in check for check in check_names)
    needs_stamp = any("stamp" in check or "seal" in check for check in check_names)
    if not (needs_signature or needs_stamp):
        return
    if (not needs_signature or verification.check_results.get("signed") == "pass") and (
        not needs_stamp or verification.check_results.get("stamped") == "pass"
    ):
        return
    api_key = gemini_api_key()
    if not api_key:
        return

    inspections: list[dict[str, Any]] = []
    candidate_page_ids = list(dict.fromkeys([
        *verification.page_ids,
        *entry.matched_page_ids,
    ]))
    for page_id in candidate_page_ids[:VISUAL_SIGNATURE_MAX_PAGES]:
        source = page_source_by_id.get(page_id)
        if not source:
            continue
        document, page = source
        if not document.source_path or document.source_path.suffix.lower() != ".pdf":
            continue
        cache_key = f"{document.source_path.resolve()}:{page.page_number}"
        future: Future[dict[str, Any]] | None = None
        owner = True
        if inspection_cache is not None and cache_lock is not None:
            with cache_lock:
                future = inspection_cache.get(cache_key)
                if future is None:
                    future = Future()
                    inspection_cache[cache_key] = future
                else:
                    owner = False
        if owner:
            try:
                image_b64 = render_pdf_page_png_b64(document.source_path, page.page_number - 1)
                result = inspect_signature_stamp_image(api_key, image_b64, True, True)
            except Exception as exc:
                result = {
                    "status": "error",
                    "reason": str(exc)[:240],
                    "signature": "unclear",
                    "stamp_or_seal": "unclear",
                }
            if future is not None:
                future.set_result(result)
        else:
            result = future.result()
        result = dict(result)
        result["page_id"] = page_id
        result["page_number"] = page.page_number
        inspections.append(result)

    if not inspections:
        return
    verification.visual_checks.extend(inspections)
    signature_pass = any(item.get("signature") == "present" for item in inspections)
    stamp_pass = any(item.get("stamp_or_seal") == "present" for item in inspections)
    if needs_signature and signature_pass:
        verification.check_results["signed"] = "pass"
    if needs_stamp and stamp_pass:
        verification.check_results["stamped"] = "pass"
    if (not needs_signature or verification.check_results.get("signed") == "pass") and (
        not needs_stamp or verification.check_results.get("stamped") == "pass"
    ):
        if verification.status == "unclear":
            verification.status = "pass"
            verification.reason = "Signature/stamp verified by visual inspection of the matched evidence page."
    elif verification.status == "pass":
        verification.status = "unclear"
        verification.reason = "Document is present, but visual signature/stamp inspection was inconclusive."
    verification.status = status_from_check_results(verification.status, verification.check_results)


def inspect_signature_stamp_image(api_key: str, image_b64: str, needs_signature: bool, needs_stamp: bool) -> dict[str, Any]:
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt_text(
                            "visual_signature",
                            "You are a visual document authenticity assistant for Indian procurement. "
                            "Inspect only the supplied page image. Do not use file names. "
                            "Decide whether the page visibly contains an actual handwritten signature, "
                            "digital signature block/certificate, company stamp, seal, or round seal. "
                            "Do not count blank placeholders such as 'signature and seal' unless ink, stamp, "
                            "seal, or digital-signature details are visible. Return JSON only with keys: "
                            "signature, stamp_or_seal, reason. Values for signature and stamp_or_seal must be "
                            "present, absent, or unclear.",
                        )
                        + f" Required checks: signature={needs_signature}, stamp_or_seal={needs_stamp}."
                    },
                    {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET},
        },
    }
    data = parse_json_object(post_gemini_content(api_key, GEMINI_MODEL_FAST, body))
    return {
        "signature": normalize_visual_status(data.get("signature")),
        "stamp_or_seal": normalize_visual_status(data.get("stamp_or_seal")),
        "reason": str(data.get("reason") or "")[:300],
    }


def normalize_visual_status(value: Any) -> Literal["present", "absent", "unclear"]:
    text = str(value or "").strip().lower()
    if text in {"present", "yes", "pass", "visible", "found"}:
        return "present"
    if text in {"absent", "no", "missing", "not_present", "not visible", "not_found"}:
        return "absent"
    return "unclear"
