"""Worker lifecycle helpers kept separate from domain pipeline code."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from ai_provider import AIProviderFailure
from procurement.domain.models import Evaluation, Operation, WorkbenchBid
from procurement.platform.database import session_scope

logger = logging.getLogger("procurement.worker")

def _log_pipeline_stage(
    job_id: str,
    pipeline: str,
    stage: str,
    **details: object,
) -> None:
    suffix = " ".join(
        f"{key}={str(value).replace(chr(10), ' ')[:160]}"
        for key, value in details.items()
        if value is not None
    )
    logger.info(
        "PIPELINE job_id=%s pipeline=%s stage=%s%s",
        job_id,
        pipeline,
        stage,
        f" {suffix}" if suffix else "",
    )

def _select_intake_rows(rows: list[WorkbenchBid], vendor_key: str) -> list[WorkbenchBid]:
    if vendor_key:
        return [row for row in rows if row.vendor_key == vendor_key]
    return [row for row in rows if not row.intake_json]


def _persist_operation_progress(
    sessions: Any,
    operation_id: str,
    lease_id: str,
    stage: str,
    details: dict[str, object],
) -> None:
    with session_scope(sessions) as session:
        operation = session.get(Operation, operation_id)
        if (
            operation
            and operation.status == "running"
            and operation.lease_id == lease_id
        ):
            operation.result_json = {
                "progress": {"stage": stage, **details},
                "attempt": operation.attempt,
            }
            operation.progress_version = int(operation.progress_version or 0) + 1

def _public_failure_detail(exc: Exception) -> str:
    detail = str(exc).lower()
    if isinstance(exc, AIProviderFailure):
        if exc.category == "budget_exceeded":
            return "The configured AI budget is exhausted. Increase AI_BUDGET_LIMIT_INR before starting more paid work."
        if not exc.retryable:
            return "Configured AI provider rejected the request. Check its endpoint, model, and credentials."
        return "Configured AI provider is temporarily unavailable. The operation will retry automatically."
    if "bid package does not match" in detail or "different tender" in detail:
        return "Bid package references a different tender. Upload the correct bidder submission."
    if "tender ocr failed" in detail:
        return "Tender OCR did not complete. Retry understanding; completed pages are cached."
    if "bidder ocr failed" in detail:
        return "Bidder OCR did not complete. Retry the operation; completed pages are cached."
    if "spend" in detail and ("cap" in detail or "limit" in detail):
        return "Configured AI provider spending cap was exceeded."
    if "rate limit" in detail or "resource_exhausted" in detail or "429" in detail:
        return "Configured AI provider rate limit was exceeded."
    if "timeout" in detail or "timed out" in detail:
        return "Processing timed out. The operation may be retried with the same idempotency key."
    if any(term in detail for term in ("connection", "ssl", "eof", "temporarily unavailable")):
        return "Configured AI or OCR provider is temporarily unavailable."
    if "json" in detail and any(term in detail for term in ("invalid", "decode", "delimiter", "truncated")):
        return "Configured AI provider returned malformed JSON."
    return "Processing failed. Check the restricted worker log using this job ID."

def _is_retryable_failure(exc: Exception) -> bool:
    detail = str(exc).lower()
    if isinstance(exc, AIProviderFailure):
        return exc.retryable
    if "spend" in detail and ("cap" in detail or "limit" in detail):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(
        term in detail
        for term in (
            "429",
            "rate limit",
            "resource_exhausted",
            "timeout",
            "timed out",
            "connection",
            "ssl",
            "eof",
            "temporarily unavailable",
            "ocr failed",
            "invalid json",
            "jsondecode",
            "delimiter",
            "truncated",
            "502",
            "503",
            "504",
        )
    )

def restore_previous_successful_evaluation(
    session: Any,
    evaluation: Evaluation,
    payload: dict,
) -> str | None:
    previous_id = str(payload.get("previous_evaluation_id") or "")
    previous = session.get(Evaluation, previous_id) if previous_id else None
    row = session.scalar(select(WorkbenchBid).where(WorkbenchBid.bid_id == evaluation.bid_id))
    if previous and previous.status == "succeeded" and row and row.evaluation_id == evaluation.id:
        row.evaluation_id = previous.id
        return previous.id
    return None

__all__ = [
  "_is_retryable_failure",
  "_log_pipeline_stage",
  "_persist_operation_progress",
  "_public_failure_detail",
  "_select_intake_rows",
  "logger",
  "restore_previous_successful_evaluation",
]
