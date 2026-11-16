"""HTTP guard helpers; application policy stays at the composition root."""

from __future__ import annotations

import secrets
import time
import uuid
import logging
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from threading import Lock
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger("uvicorn.error")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
request_id_context: ContextVar[str | None] = ContextVar("procurement_request_id", default=None)


def safe_request_id(value: str | None) -> str:
    candidate = (value or "").strip()
    return candidate if _REQUEST_ID.fullmatch(candidate) else uuid.uuid4().hex


def require_api_token(request: Request, api_token: str | None) -> None:
    if not api_token:
        raise HTTPException(status_code=503, detail="PROCUREMENT_API_TOKEN or API_AUTH_TOKEN is required")
    header = request.headers.get("authorization", "")
    token = request.headers.get("x-api-key", "")
    if header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, api_token):
        raise HTTPException(status_code=401, detail="missing or invalid API token")


def enforce_rate_limit(
    request: Request,
    *,
    rate_limit_per_minute: int,
    recent_requests: dict[str, list[float]],
    lock: Lock,
) -> None:
    if rate_limit_per_minute <= 0:
        return
    now = time.time()
    client = request.client.host if request.client else "unknown"
    with lock:
        recent = [stamp for stamp in recent_requests.get(client, []) if now - stamp < 60]
        if len(recent) >= rate_limit_per_minute:
            recent_requests[client] = recent
            raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"})
        recent.append(now)
        recent_requests[client] = recent


async def guarded_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
    *,
    public_paths: set[str],
    rate_limited_public_paths: set[str] | None = None,
    check_rate_limit: Callable[[Request], None],
    check_auth: Callable[[Request], None],
    make_error_response: Callable[[int, Any, dict[str, str] | None], Any],
) -> Any:
    request_id = safe_request_id(request.headers.get("x-request-id"))
    context_token = request_id_context.set(request_id)
    state = getattr(request, "state", None)
    if state is not None:
        state.request_id = request_id
    try:
        is_public = request.url.path in public_paths
        if not is_public or request.url.path in (rate_limited_public_paths or set()):
            try:
                check_rate_limit(request)
                if not is_public:
                    check_auth(request)
            except HTTPException as exc:
                response = make_error_response(exc.status_code, exc.detail, exc.headers)
                response.headers["X-Request-ID"] = request_id
                return response
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/v1/") or request.url.path.startswith("/workbench/account"):
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        principal = getattr(state, "principal", None)
        logger.info(
            "request_id=%s method=%s path=%s status=%s duration_ms=%s actor=%r tenant=%r",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            round((time.perf_counter() - started) * 1000, 1),
            getattr(principal, "subject", None),
            getattr(principal, "tenant_id", None),
        )
        return response
    finally:
        request_id_context.reset(context_token)
