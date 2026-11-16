"""Stable provider errors and exception classification."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os


class AIConfigurationError(RuntimeError):
    """The selected provider configuration is incomplete or invalid."""


class AIContextLimitError(RuntimeError):
    """The request would exceed the configured safe context budget."""


class AIProviderFailure(RuntimeError):
    """A provider failure with stable retry and operator semantics."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        retryable: bool,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class AIProviderBudgetExceeded(AIProviderFailure):
    def __init__(self, message: str = "configured AI budget is exhausted") -> None:
        super().__init__(message, category="budget_exceeded", retryable=False)


class AIProviderTransportError(AIProviderFailure):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, category="transport", retryable=True, retry_after=retry_after)


class AIProviderHTTPError(AIProviderFailure):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            category=f"http_{status_code}",
            retryable=retryable,
            status_code=status_code,
            retry_after=retry_after,
        )


class AIProviderOutputError(AIProviderFailure):
    def __init__(self, message: str, *, category: str = "invalid_output") -> None:
        super().__init__(message, category=category, retryable=False)


class AIProviderTruncatedError(AIProviderOutputError):
    def __init__(self, message: str) -> None:
        super().__init__(message, category="truncated_output")


def is_provider_spend_cap_error(exc: Exception) -> bool:
    if isinstance(exc, AIProviderFailure) and exc.category in {"http_429", "spend_cap"}:
        return exc.category == "spend_cap"
    text = str(exc).lower()
    return "monthly spending cap" in text or ("resource_exhausted" in text and "spend" in text)


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(exc, "code", None)
    if value is None:
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_provider_exception(exc: BaseException) -> AIProviderFailure:
    """Normalize SDK, HTTP and transport failures without retrying fatal input errors."""
    if isinstance(exc, AIProviderFailure):
        return exc
    if isinstance(exc, AIConfigurationError):
        return AIProviderFailure(str(exc), category="configuration", retryable=False)
    status = _status_code(exc)
    retry_after = _retry_after_seconds(exc)
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    configured_statuses = os.getenv("AI_RETRY_HTTP_STATUS_CODES", "").strip()
    if configured_statuses:
        try:
            retryable_statuses = {
                int(value.strip())
                for value in configured_statuses.split(",")
                if value.strip()
            }
        except ValueError:
            return AIProviderFailure(
                "AI_RETRY_HTTP_STATUS_CODES must be a comma-separated integer list",
                category="configuration",
                retryable=False,
            )
    if status in retryable_statuses:
        return AIProviderHTTPError(
            f"provider returned HTTP {status}",
            status_code=status,
            retryable=True,
            retry_after=retry_after,
        )
    if status in {400, 401, 403, 404}:
        return AIProviderHTTPError(
            f"provider returned HTTP {status}",
            status_code=status,
            retryable=False,
            retry_after=retry_after,
        )
    exception_name = type(exc).__name__.lower()
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
        marker in exception_name
        for marker in ("timeout", "connecterror", "connectionerror", "networkerror")
    ):
        return AIProviderTransportError(str(exc) or "provider transport failure", retry_after=retry_after)
    return AIProviderFailure(str(exc) or "provider request failed", category="provider_error", retryable=False)


__all__ = [
    "AIConfigurationError",
    "AIContextLimitError",
    "AIProviderBudgetExceeded",
    "AIProviderFailure",
    "AIProviderHTTPError",
    "AIProviderOutputError",
    "AIProviderTransportError",
    "AIProviderTruncatedError",
    "classify_provider_exception",
    "is_provider_spend_cap_error",
]
