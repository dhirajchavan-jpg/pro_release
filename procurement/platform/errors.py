"""Public HTTP errors with one stable, frontend-safe envelope."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.responses import JSONResponse

from procurement.platform.http import request_id_context


def error_response(
    status_code: int,
    detail: Any,
    headers: dict[str, str] | None = None,
    *,
    code: str | None = None,
) -> JSONResponse:
    if isinstance(detail, dict):
        message = str(detail.get("message") or "request failed")
        details: Any = {key: value for key, value in detail.items() if key != "message"}
    else:
        message = str(detail)
        details = {}
    request_id = request_id_context.get() or str(uuid.uuid4())
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code or str(status_code), "message": message, "request_id": request_id, "details": details}},
        headers=headers,
    )


def internal_error_response() -> tuple[str, JSONResponse]:
    error_id = uuid.uuid4().hex[:12]
    return error_id, error_response(500, f"request failed; error_id={error_id}", code="INTERNAL_ERROR")
