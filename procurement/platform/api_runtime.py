"""Application-wide HTTP guard and stable error handling."""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from procurement.platform.auth import authenticate
from procurement.platform.env import env_int
from procurement.platform.errors import (
    error_response as platform_error_response,
    internal_error_response,
)
from procurement.platform.http import enforce_rate_limit, guarded_request
from procurement.platform.operations import IdempotencyConflict, QueueCapacityExceeded


logger = logging.getLogger("procurement_evaluator")
RATE_LIMITS: dict[str, list[float]] = {}
RATE_LIMIT_LOCK = threading.Lock()
WORKBENCH_PUBLIC_PATHS = {
    "/workbench",
    "/assets/bid-workbench.js",
    "/assets/workbench-theme.css",
    "/workbench/account/login",
    "/workbench/account/logout",
    "/workbench/account/register",
    "/workbench/login",
    "/workbench/register",
}
WORKBENCH_UI_PATH = re.compile(r"^/workbench/ui/[^/]+/[^/]+$")
WORKBENCH_MODULE_PATH = re.compile(r"^/assets/(?:current/)?modules/[a-z][a-z0-9-]*\.js$")


async def guard_api(request: Request, call_next: Any) -> Any:
    public_paths = {"/", "/health", "/ready"} | WORKBENCH_PUBLIC_PATHS
    if WORKBENCH_UI_PATH.match(request.url.path) or WORKBENCH_MODULE_PATH.match(request.url.path):
        public_paths = public_paths | {request.url.path}
    return await guarded_request(
        request,
        call_next,
        public_paths=public_paths,
        rate_limited_public_paths={"/workbench/account/login"},
        check_rate_limit=check_rate_limit,
        check_auth=check_api_auth,
        make_error_response=error_response,
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if request.url.path.startswith("/v1/pipeline/fixed-schema") and not (
        isinstance(exc.detail, dict) and exc.detail.get("code")
    ):
        code = {
            400: "INVALID_REQUEST",
            401: "INVALID_API_TOKEN",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "PRECONDITION_FAILED",
            413: "UPLOAD_TOO_LARGE",
            415: "UNSUPPORTED_MEDIA_TYPE",
            429: "RATE_LIMITED",
            500: "INTERNAL_ERROR",
            503: "PROVIDER_UNAVAILABLE",
            504: "JOB_TIMEOUT",
        }.get(exc.status_code)
        if code:
            detail = {"message": str(exc.detail), "code": code}
            if exc.status_code == 429:
                detail["retry_after_seconds"] = int((exc.headers or {}).get("Retry-After", "60"))
            return error_response(exc.status_code, detail, exc.headers)
    return error_response(exc.status_code, exc.detail, exc.headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "message": str(error.get("msg") or "invalid value"),
            "type": str(error.get("type") or "validation_error"),
        }
        for error in exc.errors()
    ]
    if request.url.path.startswith("/v1/pipeline/fixed-schema"):
        malformed_json = any(error.get("type") == "json_invalid" for error in exc.errors())
        if malformed_json:
            return error_response(400, {"message": "Request body is not valid JSON.", "code": "INVALID_JSON", "field_errors": errors})
        if request.method == "PUT":
            code = "CLASSIFICATION_VALIDATION_FAILED" if "/classification" in request.url.path else "FIXED_SCHEMA_VALIDATION_FAILED"
            return error_response(422, {"message": "Request validation failed.", "code": code, "field_errors": errors})
        return error_response(400, {"message": "Request validation failed.", "code": "INVALID_REQUEST", "field_errors": errors})
    return error_response(422, {"message": "request validation failed", "errors": errors})


async def idempotency_error_handler(request: Request, exc: IdempotencyConflict) -> JSONResponse:
    if request.url.path.startswith("/v1/pipeline/fixed-schema"):
        return error_response(409, {"message": str(exc), "code": "IDEMPOTENCY_CONFLICT"})
    return error_response(409, str(exc))


async def queue_capacity_error_handler(request: Request, exc: QueueCapacityExceeded) -> JSONResponse:
    return error_response(
        503,
        {
            "code": "QUEUE_CAPACITY_EXCEEDED",
            "message": "The processing queue is full. Retry later.",
        },
        {"Retry-After": "60"},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id, response = internal_error_response()
    logger.exception("Unhandled API error %s request_id=%s path=%s", error_id, getattr(request.state, "request_id", "unknown"), request.url.path)
    return response


def check_api_auth(request: Request) -> None:
    try:
        authenticate(
            request,
            os.getenv("PROCUREMENT_API_TOKEN") or os.getenv("API_AUTH_TOKEN"),
        )
    except HTTPException as exc:
        path = str(getattr(getattr(request, "url", None), "path", ""))
        if path.startswith("/v1/pipeline/fixed-schema") and exc.status_code == 401:
            cookies = getattr(request, "cookies", {})
            supplied = bool(request.headers.get("authorization") or request.headers.get("x-api-key") or cookies.get("procurement_session"))
            exc.detail = {
                "message": "Bearer token is invalid or expired." if supplied else "Authorization header is missing.",
                "code": "INVALID_API_TOKEN" if supplied else "AUTHENTICATION_REQUIRED",
            }
        raise


def error_response(status_code: int, detail: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    code = str(detail.get("code")) if isinstance(detail, dict) and detail.get("code") else None
    clean_detail = {key: value for key, value in detail.items() if key != "code"} if code else detail
    return platform_error_response(status_code, clean_detail, headers, code=code)


def check_rate_limit(request: Request) -> None:
    # ponytail: in-memory limiter is enough for one API process; use Redis only behind multiple API workers.
    try:
        enforce_rate_limit(
            request,
            rate_limit_per_minute=env_int("RATE_LIMIT_PER_MINUTE", 30, minimum=1),
            recent_requests=RATE_LIMITS,
            lock=RATE_LIMIT_LOCK,
        )
    except HTTPException as exc:
        if request.url.path.startswith("/v1/pipeline/fixed-schema") and exc.status_code == 429:
            exc.detail = {"message": "Request is temporarily rate limited.", "code": "RATE_LIMITED", "retry_after_seconds": 60}
        raise


def install(app: FastAPI) -> None:
    app.middleware("http")(guard_api)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(IdempotencyConflict, idempotency_error_handler)
    app.add_exception_handler(QueueCapacityExceeded, queue_capacity_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
