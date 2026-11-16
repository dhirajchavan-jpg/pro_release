"""Polling, leasing, recovery, and lifecycle control for durable operations."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from procurement.platform.env import env_int
from procurement.platform.files import write_json_atomic
from procurement.platform.log_safety import configure_worker_logging
from procurement.platform.object_store import ensure_free_disk


logger = logging.getLogger("procurement.worker")


def cleanup_stale_worker_directories(now: float | None = None) -> int:
    root = Path(tempfile.gettempdir()).resolve()
    cutoff = (now if now is not None else time.time()) - env_int(
        "WORKER_TEMP_STALE_SECONDS", 86400, minimum=600
    )
    removed = 0
    for candidate in root.glob("procurement-worker-*"):
        try:
            resolved = candidate.resolve()
            if not resolved.is_dir() or resolved.parent != root or resolved.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(resolved)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Skipping locked stale worker directory %s: %s", candidate, exc)
    return removed


def heartbeat_forever() -> None:
    path = Path(os.getenv("WORKER_HEARTBEAT_FILE", "runtime/worker.heartbeat.json"))
    interval = env_int("WORKER_HEARTBEAT_SECONDS", 10, minimum=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        write_json_atomic(
            path,
            {"pid": os.getpid(), "timestamp": time.time()},
            indent=None,
        )
        time.sleep(interval)


def operation_heartbeat(
    runtime: Any,
    sessions: Any,
    operation_id: str,
    lease_id: str,
    stop: threading.Event,
) -> None:
    lease_seconds = env_int("OPERATION_LEASE_SECONDS", 300, minimum=30)
    interval = max(5, min(60, lease_seconds // 3))
    while not stop.wait(interval):
        try:
            with runtime.session_scope(sessions) as session:
                operation = session.get(runtime.Operation, operation_id)
                if not operation:
                    return
                runtime.heartbeat(
                    session,
                    operation,
                    lease_id=lease_id,
                    lease_seconds=lease_seconds,
                )
        except runtime.OperationLeaseError:
            logger.warning(
                "Operation %s lease was transferred; stopping heartbeat", operation_id
            )
            return
        except Exception:
            logger.exception(
                "Operation %s heartbeat failed; it will retry", operation_id
            )


def run_once_safely(
    runtime: Any,
    run: Callable[[], bool] | None = None,
) -> bool:
    try:
        return (run or runtime.run_once)()
    except Exception:
        logger.exception("Worker polling failed; it will retry")
        return False


def run_once(runtime: Any, sessions: Any | None = None) -> bool:
    sessions = sessions or runtime.make_session_factory()
    with runtime.session_scope(sessions) as session:
        operation = runtime.claim_next(session)
        if not operation:
            return False
        operation_id = operation.id
        entity_id = operation.entity_id
        operation_stage = operation.stage
        operation_payload = dict(operation.payload_json or {})
        operation_attempt = operation.attempt
        operation_lease_id = str(operation.lease_id)
    work: Path | None = None
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    usage_started = False
    try:
        runtime._log_pipeline_stage(
            operation_id,
            operation_stage,
            "job_started",
            entity_id=entity_id,
            attempt=operation_attempt,
        )
        ensure_free_disk(
            Path(tempfile.gettempdir()),
            additional_bytes=env_int("MAX_EXTRACTED_MB", 250, minimum=1) * 1024 * 1024,
        )
        work = Path(tempfile.mkdtemp(prefix=f"procurement-worker-{operation_id}-"))
        runtime.reset_llm_usage(operation_id)
        runtime.start_provider_usage(
            operation_id,
            runtime._provider_usage_context(
                sessions,
                operation_id,
                operation_attempt,
                operation_stage,
                entity_id,
                operation_payload,
            ),
            lambda event: runtime._persist_provider_call(sessions, event),
            sessions,
        )
        usage_started = True
        runtime.set_job_deadline()
        heartbeat_thread = threading.Thread(
            target=runtime._operation_heartbeat,
            args=(sessions, operation_id, operation_lease_id, heartbeat_stop),
            name=f"operation-heartbeat-{operation_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        if operation_stage == "understanding":
            return runtime._run_understanding_operation(
                sessions,
                operation_id,
                entity_id,
                operation_payload,
                operation_lease_id,
                work,
            )
        if operation_stage == "compliance":
            return runtime._run_compliance_operation(
                sessions,
                operation_id,
                entity_id,
                operation_payload,
                operation_lease_id,
            )
        if operation_stage == "intake":
            return runtime._run_intake_operation(
                sessions,
                operation_id,
                entity_id,
                operation_payload,
                operation_lease_id,
                work,
            )
        if operation_stage == "evaluation":
            return runtime._run_evaluation_operation(
                sessions,
                operation_id,
                entity_id,
                operation_lease_id,
                work,
            )
        raise ValueError(f"unsupported operation stage: {operation_stage}")
    except Exception as exc:
        runtime._log_pipeline_stage(
            operation_id,
            operation_stage,
            "job_failed",
            error_type=type(exc).__name__,
        )
        logger.exception("Operation %s failed", operation_id)
        if isinstance(exc, runtime.OperationLeaseError):
            logger.warning(
                "Operation %s result was discarded because another worker owns it",
                operation_id,
            )
            return True
        with runtime.session_scope(sessions) as session:
            operation = session.get(runtime.Operation, operation_id)
            evaluation = (
                session.get(runtime.Evaluation, entity_id)
                if operation_stage == "evaluation"
                else None
            )
            if operation:
                maximum_attempts = env_int("WORKER_MAX_ATTEMPTS", 3, minimum=1)
                if runtime._is_retryable_failure(exc) and operation.attempt < maximum_attempts:
                    base = env_int("WORKER_RETRY_BASE_SECONDS", 30, minimum=1)
                    maximum_delay = max(
                        base, env_int("WORKER_RETRY_MAX_SECONDS", 300, minimum=1)
                    )
                    delay = min(
                        maximum_delay,
                        base * (2 ** max(0, operation.attempt - 1)),
                    )
                    runtime.retry_later(
                        session,
                        operation,
                        code=f"{operation_stage}_retry_scheduled",
                        detail=runtime._public_failure_detail(exc),
                        delay_seconds=delay,
                        lease_id=operation_lease_id,
                    )
                    if evaluation:
                        evaluation.status = "queued"
                    runtime.audit(
                        session,
                        tenant_id=operation.tenant_id,
                        actor_user_id=None,
                        event_type="operation.retry_scheduled",
                        entity_type=operation.entity_type,
                        entity_id=operation.entity_id,
                        reason=runtime._public_failure_detail(exc),
                        before={
                            "attempt": operation.attempt,
                            "status": "running",
                        },
                        after={
                            "attempt": operation.attempt,
                            "status": "queued",
                            "delay_seconds": delay,
                        },
                    )
                    return True
                runtime.fail(
                    session,
                    operation,
                    code=f"{operation_stage}_failed",
                    detail=runtime._public_failure_detail(exc),
                    lease_id=operation_lease_id,
                )
            if evaluation and operation and operation.status == "failed":
                evaluation.status = "failed"
                restored = runtime.restore_previous_successful_evaluation(
                    session, evaluation, operation_payload
                )
                runtime.audit(
                    session,
                    tenant_id=evaluation.tenant_id,
                    actor_user_id=None,
                    event_type="evaluation.failed",
                    entity_type="evaluation",
                    entity_id=evaluation.id,
                    reason=None,
                    before={"status": "running"},
                    after={
                        "status": "failed",
                        "restored_evaluation_id": restored,
                    },
                )
        return True
    finally:
        heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=2)
        runtime.clear_job_deadline()
        if usage_started:
            runtime.stop_provider_usage(operation_id)
        if work:
            shutil.rmtree(work, ignore_errors=True)


def run_forever(runtime: Any) -> None:
    runtime.load_local_env()
    configure_worker_logging(os.getenv("LOG_LEVEL", "INFO"))
    removed = cleanup_stale_worker_directories()
    if removed:
        logger.info("Removed %s stale worker temp directories", removed)
    runtime._log_pipeline_stage(
        "worker",
        "system",
        "worker_ready",
        ai_provider=os.getenv("AI_PROVIDER", "gemini"),
        fast_model=os.getenv("AI_MODEL_FAST") or os.getenv("GEMINI_MODEL_FAST", ""),
        deep_model=os.getenv("AI_MODEL_DEEP") or os.getenv("GEMINI_MODEL_DEEP", ""),
        ocr_provider=os.getenv("BASE_RFP_OCR_PROVIDER", "paddle"),
    )
    threading.Thread(
        target=runtime._heartbeat_forever,
        name="worker-heartbeat",
        daemon=True,
    ).start()
    sessions = runtime.make_session_factory()
    with runtime.session_scope(sessions) as session:
        recovered = runtime.recover_interrupted(
            session,
            maximum_attempts=env_int("WORKER_MAX_ATTEMPTS", 3, minimum=1),
        )
        exhausted = list(
            session.scalars(
                runtime.select(runtime.Operation).where(
                    runtime.Operation.stage == "evaluation",
                    runtime.Operation.status == "failed",
                    runtime.Operation.error_code == "restart_attempts_exhausted",
                )
            )
        )
        for operation in exhausted:
            evaluation = session.get(runtime.Evaluation, operation.entity_id)
            if evaluation and evaluation.status in {"queued", "running"}:
                evaluation.status = "failed"
                runtime.restore_previous_successful_evaluation(
                    session,
                    evaluation,
                    dict(operation.payload_json or {}),
                )
    if recovered:
        logger.warning("Recovered %s interrupted operation(s)", recovered)
    poll_seconds = env_int("WORKER_POLL_SECONDS", 3, minimum=1)
    worker_concurrency = env_int(
        "WORKER_CONCURRENCY", 1, minimum=1, maximum=1
    )
    logger.info("Operation worker concurrency=%s", worker_concurrency)

    def operation_loop() -> None:
        while True:
            if not runtime._run_once_safely(
                lambda: runtime.run_once(sessions)
            ):
                time.sleep(poll_seconds)

    threads = [
        threading.Thread(
            target=operation_loop,
            name=f"operation-worker-{index + 1}",
        )
        for index in range(worker_concurrency)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
