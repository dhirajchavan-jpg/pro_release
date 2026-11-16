"""Durable, content-free provider metering for worker operations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlparse

from procurement.domain.models import ProviderCall
from procurement.platform.database import session_scope
from procurement.platform.job_context import JOB_CONTEXT
from procurement.providers.errors import AIProviderBudgetExceeded
from sqlalchemy import func, select


logger = logging.getLogger("procurement.provider_usage")
_SINKS_LOCK = threading.RLock()
_SINKS: dict[str, "_UsageSink"] = {}
_BLOCKED_METRIC_NAMES = ("key", "secret", "prompt", "content", "document", "text", "url")


class ProviderUsagePersistenceError(RuntimeError):
    pass


@dataclass
class _UsageSink:
    context: dict[str, Any]
    persist: Callable[[dict[str, Any]], None]
    sessions: Any | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    threads: list[threading.Thread] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.context)

    def update(self, values: dict[str, Any]) -> None:
        with self.lock:
            self.context.update(values)

    def register(self, thread: threading.Thread) -> None:
        with self.lock:
            self.threads.append(thread)

    def registered_threads(self) -> list[threading.Thread]:
        with self.lock:
            return list(self.threads)


def start_provider_usage(
    operation_id: str,
    context: dict[str, Any],
    persist: Callable[[dict[str, Any]], None],
    sessions: Any | None = None,
) -> None:
    sink = _UsageSink({**context, "operation_id": operation_id}, persist, sessions)
    with _SINKS_LOCK:
        _SINKS[operation_id] = sink
    JOB_CONTEXT.job_id = operation_id
    JOB_CONTEXT.provider_operation_id = operation_id


def stop_provider_usage(operation_id: str) -> None:
    with _SINKS_LOCK:
        sink = _SINKS.get(operation_id)
    if sink:
        for thread in sink.registered_threads():
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join()
    with _SINKS_LOCK:
        if _SINKS.get(operation_id) is sink:
            _SINKS.pop(operation_id, None)
    if getattr(JOB_CONTEXT, "provider_operation_id", None) == operation_id:
        JOB_CONTEXT.provider_operation_id = None


def update_provider_usage(operation_id: str, **values: Any) -> None:
    with _SINKS_LOCK:
        sink = _SINKS.get(operation_id)
    if sink:
        sink.update(values)


def bind_provider_usage(operation_id: str) -> None:
    JOB_CONTEXT.job_id = operation_id
    JOB_CONTEXT.provider_operation_id = operation_id


def register_provider_thread(operation_id: str, thread: threading.Thread) -> None:
    with _SINKS_LOCK:
        sink = _SINKS.get(operation_id)
    if sink:
        sink.register(thread)


def current_operation_id() -> str | None:
    value = str(
        getattr(JOB_CONTEXT, "provider_operation_id", None)
        or getattr(JOB_CONTEXT, "job_id", "")
        or ""
    ).strip()
    if value:
        return value
    # ponytail: production admits one running operation; this lets its worker
    # pool threads inherit attribution without wrapping every executor submit.
    with _SINKS_LOCK:
        return next(iter(_SINKS)) if len(_SINKS) == 1 else None


def fingerprint(value: Any) -> str:
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def endpoint_host(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return (parsed.hostname or "").lower() or None


def _decimal_env(*names: str) -> tuple[Decimal, str | None]:
    for name in names:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            logger.error("Ignoring invalid non-decimal provider price setting %s", name)
            return Decimal(0), name
        if value < 0:
            logger.error("Ignoring negative provider price setting %s", name)
            return Decimal(0), name
        return value, name
    return Decimal(0), None


def _required_decimal_env(name: str) -> Decimal:
    raw = os.getenv(name, "").strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ProviderUsagePersistenceError(f"{name} must be a positive decimal") from exc
    if value <= 0:
        raise ProviderUsagePersistenceError(f"{name} must be a positive decimal")
    return value


def _budget_capability(task: str) -> str:
    value = task.lower()
    if "embedding" in value:
        return "embedding"
    if "rerank" in value:
        return "reranker"
    if any(marker in value for marker in ("ocr", "signature", "structural")):
        return "ocr"
    if any(marker in value for marker in ("verifier", "verification", "judge")):
        return "verifier"
    return "primary"


def _tenant_spend(sink: _UsageSink, tenant_id: str) -> Decimal:
    if sink.sessions is None:
        raise ProviderUsagePersistenceError("provider budget meter has no database session")
    with session_scope(sink.sessions) as session:
        value = session.scalar(
            select(func.coalesce(func.sum(ProviderCall.estimated_cost_inr), 0)).where(
                ProviderCall.tenant_id == tenant_id
            )
        )
    return Decimal(str(value or 0))


def check_provider_budget(task: str) -> None:
    """Block new provider calls after the tenant's recorded spend reaches its cap."""
    if not os.getenv("AI_BUDGET_LIMIT_INR", "").strip():
        return
    limit = _required_decimal_env("AI_BUDGET_LIMIT_INR")
    operation_id = current_operation_id()
    if not operation_id:
        raise ProviderUsagePersistenceError("provider budget check has no active operation meter")
    with _SINKS_LOCK:
        sink = _SINKS.get(operation_id)
    if not sink:
        raise ProviderUsagePersistenceError("provider budget check has no active operation meter")
    tenant_id = str(sink.snapshot().get("tenant_id") or "")
    if not tenant_id:
        raise ProviderUsagePersistenceError("provider budget check has no tenant")
    spent = _tenant_spend(sink, tenant_id)
    if spent < limit:
        return
    capability = _budget_capability(task)
    record_provider_call(
        capability=capability,
        provider="budget_guard",
        model="",
        task=task,
        status="budget_blocked",
        duration_ms=0,
        error_category="budget_exceeded",
        metrics={
            "budget_limit_inr": float(limit),
            "budget_spent_inr": float(spent),
        },
    )
    raise AIProviderBudgetExceeded(
        f"configured AI budget of INR {limit} is exhausted"
    )


def _model_price_prefix(model: str, capability: str) -> str:
    capability = capability.upper()
    configured = (
        (capability, os.getenv(f"AI_MODEL_{capability}", "")),
        ("UNDERSTANDING", os.getenv("AI_MODEL_UNDERSTANDING", "")),
        ("SOURCE_EXTRACTION", os.getenv("AI_MODEL_SOURCE_EXTRACTION", "")),
        ("DEEP", os.getenv("AI_MODEL_DEEP", "")),
        ("FAST", os.getenv("AI_MODEL_FAST", "")),
        ("OCR", os.getenv("AI_MODEL_OCR", "")),
        ("EMBEDDING", os.getenv("AI_MODEL_EMBEDDING", "")),
        ("RERANKER", os.getenv("AI_MODEL_RERANKER", "")),
        ("VERIFIER", os.getenv("AI_MODEL_VERIFIER", "")),
    )
    return next((name for name, value in configured if value.strip() and value.strip() == model), capability)


def estimate_cost(
    *,
    provider: str,
    model: str,
    capability: str,
    status: str,
    attempts: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    unit_count: int = 0,
    unit_name: str | None = None,
) -> tuple[Decimal | None, Decimal | None, dict[str, Any]]:
    prefix = _model_price_prefix(model, capability)
    capability = capability.upper()
    legacy_input = (
        ("GEMINI_INPUT_COST_PER_MILLION_TOKENS",)
        if capability in {"PRIMARY", "VERIFIER", "OCR"}
        else ()
    )
    legacy_output = (
        ("GEMINI_OUTPUT_COST_PER_MILLION_TOKENS",)
        if capability in {"PRIMARY", "VERIFIER", "OCR"}
        else ()
    )
    input_rate, input_source = _decimal_env(
        f"AI_{prefix}_INPUT_COST_PER_MILLION_TOKENS",
        f"AI_{capability}_INPUT_COST_PER_MILLION_TOKENS",
        *legacy_input,
    )
    output_rate, output_source = _decimal_env(
        f"AI_{prefix}_OUTPUT_COST_PER_MILLION_TOKENS",
        f"AI_{capability}_OUTPUT_COST_PER_MILLION_TOKENS",
        *legacy_output,
    )
    request_rate, request_source = _decimal_env(
        f"AI_{prefix}_COST_PER_REQUEST_USD",
        f"AI_{capability}_COST_PER_REQUEST_USD",
    )
    provider_name = provider.upper().replace("-", "_")
    unit_rate, unit_source = _decimal_env(
        f"{provider_name}_COST_PER_{str(unit_name or 'UNIT').upper()}_USD",
        f"AI_{capability}_COST_PER_{str(unit_name or 'UNIT').upper()}_USD",
    )
    configured = any((input_source, output_source, request_source, unit_source))
    cost: Decimal | None
    attempt_count = max(1, int(attempts))
    if status in {"cache_hit", "budget_blocked"} or provider == "local":
        cost = Decimal(0)
    elif not configured:
        cost = None
    elif (
        (input_rate or output_rate)
        and not input_tokens
        and not output_tokens
        and not request_rate
        and not unit_rate
    ):
        cost = None
    else:
        cost = (
            Decimal(max(0, input_tokens)) * input_rate * attempt_count / Decimal(1_000_000)
            + Decimal(max(0, output_tokens)) * output_rate * attempt_count / Decimal(1_000_000)
            + request_rate * attempt_count
            + Decimal(max(0, unit_count)) * unit_rate * attempt_count
        ).quantize(Decimal("0.00000001"))
    fx_rate, fx_source = _decimal_env("USD_TO_INR_RATE")
    cost_inr = (
        (cost * fx_rate).quantize(Decimal("0.00000001"))
        if cost is not None and fx_source
        else None
    )
    return cost, cost_inr, {
        "revision": os.getenv("AI_PRICING_REVISION", "unversioned").strip() or "unversioned",
        "currency": "USD",
        "input_per_million_tokens": str(input_rate) if input_source else None,
        "output_per_million_tokens": str(output_rate) if output_source else None,
        "per_request_usd": str(request_rate) if request_source else None,
        "per_unit_usd": str(unit_rate) if unit_source else None,
        "unit_name": unit_name,
        "attempt_multiplier": attempt_count,
        "usd_to_inr": str(fx_rate) if fx_source else None,
        "sources": [name for name in (input_source, output_source, request_source, unit_source, fx_source) if name],
        "priced": cost is not None,
    }


def _safe_metrics(values: dict[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in (values or {}).items():
        name = str(key)[:64]
        if any(marker in name.lower() for marker in _BLOCKED_METRIC_NAMES):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            safe[name] = value
        elif isinstance(value, str):
            safe[name] = value[:120]
    return safe


def record_provider_call(
    *,
    capability: str,
    provider: str,
    model: str,
    task: str,
    status: str,
    duration_ms: int,
    operation_id: str | None = None,
    endpoint: str | None = None,
    attempts: int = 1,
    cache_status: str = "miss",
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    input_units: int = 0,
    output_units: int = 0,
    unit_name: str | None = None,
    request_fingerprint: str | None = None,
    response_fingerprint: str | None = None,
    error_category: str | None = None,
    http_status: int | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    operation_id = operation_id or current_operation_id()
    if not operation_id:
        if os.getenv("PROVIDER_USAGE_REQUIRED", "0") == "1":
            raise ProviderUsagePersistenceError("provider call has no active operation meter")
        return None
    with _SINKS_LOCK:
        sink = _SINKS.get(operation_id)
    if not sink:
        if os.getenv("PROVIDER_USAGE_REQUIRED", "0") == "1":
            raise ProviderUsagePersistenceError("provider call has no active operation meter")
        return None
    context = sink.snapshot()
    cost_usd, cost_inr, pricing = estimate_cost(
        provider=provider,
        model=model,
        capability=capability,
        status=status,
        attempts=attempts,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unit_count=input_units,
        unit_name=unit_name,
    )
    finished_at = datetime.now(timezone.utc)
    duration_ms = max(0, int(duration_ms))
    event = {
        "id": str(uuid.uuid4()),
        **context,
        "operation_id": operation_id,
        "capability": capability.lower()[:32],
        "provider": provider.lower()[:64],
        "model": model[:255],
        "task": task[:128],
        "endpoint_host": endpoint_host(endpoint),
        "status": status[:32],
        "attempts": max(1, int(attempts)),
        "cache_status": cache_status[:16],
        "input_tokens": max(0, int(input_tokens)),
        "output_tokens": max(0, int(output_tokens)),
        "total_tokens": max(0, int(total_tokens or input_tokens + output_tokens)),
        "input_units": max(0, int(input_units)),
        "output_units": max(0, int(output_units)),
        "unit_name": unit_name[:32] if unit_name else None,
        "duration_ms": duration_ms,
        "estimated_cost_usd": str(cost_usd) if cost_usd is not None else None,
        "estimated_cost_inr": str(cost_inr) if cost_inr is not None else None,
        "pricing_json": pricing,
        "metrics_json": _safe_metrics(metrics),
        "request_fingerprint": request_fingerprint,
        "response_fingerprint": response_fingerprint,
        "error_category": error_category[:64] if error_category else None,
        "http_status": http_status,
        "started_at": finished_at - timedelta(milliseconds=duration_ms),
        "finished_at": finished_at,
    }
    try:
        sink.persist(event)
    except Exception as exc:
        logger.exception(
            "Provider usage persistence failed operation_id=%s capability=%s",
            operation_id,
            capability,
        )
        if os.getenv("PROVIDER_USAGE_REQUIRED", "0") == "1":
            raise ProviderUsagePersistenceError(
                "provider usage persistence failed"
            ) from exc
    return event


def persist_provider_call(sessions: Any, event: dict[str, Any]) -> None:
    allowed = {column.name for column in ProviderCall.__table__.columns}
    with session_scope(sessions) as session:
        session.add(ProviderCall(**{key: value for key, value in event.items() if key in allowed}))


__all__ = [
    "bind_provider_usage",
    "check_provider_budget",
    "current_operation_id",
    "endpoint_host",
    "estimate_cost",
    "fingerprint",
    "persist_provider_call",
    "ProviderUsagePersistenceError",
    "record_provider_call",
    "register_provider_thread",
    "start_provider_usage",
    "stop_provider_usage",
    "update_provider_usage",
]
