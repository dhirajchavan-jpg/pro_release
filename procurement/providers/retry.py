"""Bounded retries, provider gates, and shared concurrency control."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import random
import threading
import time
from typing import Any, Callable, Iterator

from .errors import (
    AIConfigurationError,
    AIProviderFailure,
    AIProviderTransportError,
    classify_provider_exception,
)
from .settings import _provider_group, _provider_int_setting


logger = logging.getLogger("procurement.ai_provider")
_PROVIDER_GATES_LOCK = threading.Lock()
_PROVIDER_GATES: dict[tuple[str, str, int], "_ProviderGate"] = {}


class _ProviderGate:
    def __init__(self, limit: int) -> None:
        self.semaphore = threading.BoundedSemaphore(limit)
        self.warm_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.last_success = 0.0
        self.last_failure: AIProviderFailure | None = None
        self.failure_until = 0.0

    def mark_success(self) -> None:
        with self.state_lock:
            self.last_success = time.monotonic()
            self.last_failure = None
            self.failure_until = 0.0

    def mark_failure(self, failure: AIProviderFailure) -> None:
        cooldown = max(1, int(os.getenv("AI_PROVIDER_FAILURE_COOLDOWN_SECONDS", "30")))
        with self.state_lock:
            self.last_failure = failure
            self.failure_until = time.monotonic() + cooldown

    def recent_failure(self) -> AIProviderFailure | None:
        with self.state_lock:
            failure = self.last_failure
            if failure is None or time.monotonic() >= self.failure_until:
                return None
            return AIProviderFailure(
                str(failure),
                category=failure.category,
                retryable=failure.retryable,
                status_code=failure.status_code,
                retry_after=failure.retry_after,
            )


def _provider_retry_delay(attempt: int, retry_after: float | None = None) -> float:
    base = max(0.0, float(os.getenv("AI_RETRY_BASE_SECONDS", "1")))
    ceiling = max(base, float(os.getenv("AI_RETRY_MAX_DELAY_SECONDS", "60")))
    jitter = max(0.0, float(os.getenv("AI_RETRY_JITTER_SECONDS", "0.25")))
    delay = min(ceiling, base * (2 ** max(0, attempt - 1)))
    if retry_after is not None:
        delay = max(delay, min(ceiling, retry_after))
    return min(ceiling, delay + (0.0 if retry_after is not None else random.uniform(0, jitter)))


def _provider_attempts(task: str = "primary", fallback: int | None = None) -> int:
    return _provider_int_setting(
        task,
        "RETRY_ATTEMPTS",
        fallback if fallback is not None else int(os.getenv("AI_RETRY_ATTEMPTS", "3")),
        maximum=10,
    )


def call_with_provider_retry(
    call: Any,
    *,
    task: str,
    attempts: int,
    retry_delay: Any = None,
    sleep: Callable[[float], Any] | None = None,
) -> Any:
    """Call a provider with bounded, classified retries and no fallback provider."""
    from procurement.platform.provider_usage import check_provider_budget

    check_provider_budget(task)
    sleeper = sleep or time.sleep
    last_failure: AIProviderFailure | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return call()
        except AIConfigurationError:
            raise
        except Exception as exc:
            failure = classify_provider_exception(exc)
            last_failure = failure
            if not failure.retryable or attempt >= max(1, attempts):
                raise failure from exc
            delay = retry_delay(attempt) if retry_delay is not None else _provider_retry_delay(attempt, failure.retry_after)
            if failure.retry_after is not None:
                delay = max(delay, min(float(os.getenv("AI_RETRY_MAX_DELAY_SECONDS", "60")), failure.retry_after))
            logger.warning(
                "AI provider retry task=%s attempt=%s category=%s status=%s delay_seconds=%.3f",
                task,
                attempt,
                failure.category,
                failure.status_code,
                delay,
            )
            sleeper(max(0.0, delay))
    raise last_failure or AIProviderFailure("provider request failed", category="provider_error", retryable=False)


@contextmanager
def provider_slot(settings_value: Any, task: str) -> Iterator[_ProviderGate]:
    """Bound concurrent calls per capability and provider endpoint."""
    from .settings import base_url_for

    group = _provider_group(task)
    defaults = {"LLM": 2, "VISION": 1, "OCR": 2, "EMBEDDING": 2, "RERANKER": 2}
    limit = _provider_int_setting(task, "CONCURRENCY", defaults.get(group, 2), maximum=64)
    key = (group, base_url_for(settings_value, task).rstrip("/"), limit)
    with _PROVIDER_GATES_LOCK:
        gate = _PROVIDER_GATES.setdefault(key, _ProviderGate(limit))
    queue_timeout = max(
        1,
        int(os.getenv("AI_PROVIDER_QUEUE_TIMEOUT_SECONDS", "900")),
    )
    started = time.monotonic()
    if not gate.semaphore.acquire(timeout=queue_timeout):
        raise AIProviderTransportError(
            f"{group.lower()} provider concurrency queue timed out after {queue_timeout} seconds"
        )
    waited = time.monotonic() - started
    if waited >= 0.5:
        logger.info(
            "AI provider slot acquired capability=%s wait_seconds=%.3f concurrency=%s",
            group.lower(),
            waited,
            limit,
        )
    try:
        yield gate
    finally:
        gate.semaphore.release()


__all__ = [
    "_PROVIDER_GATES",
    "_PROVIDER_GATES_LOCK",
    "_ProviderGate",
    "_provider_attempts",
    "_provider_retry_delay",
    "call_with_provider_retry",
    "provider_slot",
]
