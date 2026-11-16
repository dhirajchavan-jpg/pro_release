"""Explicit adapters for the existing durable-job modules.

The job modules deliberately receive a small, typed compatibility object because
their public call signatures are already part of the worker contract. Domain
pipeline code does not use this object; it imports its dependencies directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StageRuntime:
    Bid: Any
    Document: Any
    Evaluation: Any
    Operation: Any
    Tender: Any
    TenderVersion: Any
    WorkbenchBid: Any
    WorkbenchProject: Any
    audit: Any
    datetime: Any
    finish: Any
    logger: Any
    model_dump: Any
    select: Any
    session_scope: Any
    simple_report: Any
    timezone: Any
    _bundle: Any
    _load_pinned_tender_version: Any
    _log_pipeline_stage: Any
    _persist_operation_progress: Any
    _postgres_json_safe: Any
    _select_intake_rows: Any
    _started_tender_version_id: Any
    _tender_version_has_been_replaced: Any


@dataclass(frozen=True)
class DispatcherRuntime:
    Evaluation: Any
    Operation: Any
    OperationLeaseError: Any
    audit: Any
    claim_next: Any
    clear_job_deadline: Any
    fail: Any
    heartbeat: Any
    load_local_env: Any
    make_session_factory: Any
    recover_interrupted: Any
    reset_llm_usage: Any
    restore_previous_successful_evaluation: Any
    retry_later: Any
    run_once: Any
    select: Any
    session_scope: Any
    set_job_deadline: Any
    start_provider_usage: Any
    stop_provider_usage: Any
    _heartbeat_forever: Any
    _is_retryable_failure: Any
    _log_pipeline_stage: Any
    _operation_heartbeat: Any
    _public_failure_detail: Any
    _provider_usage_context: Any
    _persist_provider_call: Any
    _run_compliance_operation: Any
    _run_evaluation_operation: Any
    _run_intake_operation: Any
    _run_once_safely: Any
    _run_understanding_operation: Any


__all__ = ["DispatcherRuntime", "StageRuntime"]
