"""Liveness and readiness endpoints for the API process."""

import os
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_provider import AIConfigurationError, settings as ai_settings, validate as validate_ai_settings
from procurement.platform.legacy_jobs import jobs_root_writable
from procurement.platform.prompts import prompt_config_error


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _resource_status() -> dict[str, int | None]:
    available_memory_mb = None
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            available_memory_mb = int(status.available_physical // (1024 * 1024))
    elif Path("/proc/meminfo").is_file():
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                available_memory_mb = int(line.split()[1]) // 1024
                break
    storage_path = Path(os.getenv("OBJECT_STORAGE_LOCAL_ROOT", "runtime/objects")).resolve()
    while not storage_path.exists() and storage_path != storage_path.parent:
        storage_path = storage_path.parent
    free_disk_mb = int(shutil.disk_usage(storage_path).free // (1024 * 1024))
    return {
        "cpu_count": os.cpu_count() or 1,
        "available_memory_mb": available_memory_mb,
        "free_disk_mb": free_disk_mb,
    }


@router.get("/ready")
def ready() -> JSONResponse:
    prompt_error = prompt_config_error()
    provider_error = None
    try:
        validate_ai_settings(ai_settings())
    except AIConfigurationError as exc:
        provider_error = str(exc)
    configured_auth_mode = os.getenv("AUTH_MODE", "api_token").strip().lower()
    api_auth_token = os.getenv("PROCUREMENT_API_TOKEN") or os.getenv("API_AUTH_TOKEN")
    auth_configured = (
        configured_auth_mode == "api_token" and bool(api_auth_token)
    ) or (
        configured_auth_mode == "jwt"
        and bool(os.getenv("AUTH_JWKS_URL", "").strip())
        and bool(os.getenv("AUTH_ISSUER", "").strip())
        and bool(os.getenv("AUTH_AUDIENCE", "").strip())
    )
    checks = {
        "auth_configured": auth_configured,
        "ai_provider_configured": provider_error is None,
        "jobs_root_writable": jobs_root_writable(),
        "prompt_config_valid": prompt_error is None,
    }
    heartbeat_path = Path(os.getenv("WORKER_HEARTBEAT_FILE", "runtime/worker.heartbeat.json"))
    heartbeat_age = None
    try:
        heartbeat_age = max(0.0, time.time() - heartbeat_path.stat().st_mtime)
    except OSError:
        pass
    checks["worker_ready"] = (
        heartbeat_age is not None
        and heartbeat_age <= max(5, int(os.getenv("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30")))
    )
    resources = _resource_status()
    checks["memory_available"] = (
        resources["available_memory_mb"] is None
        or resources["available_memory_mb"] >= max(1, int(os.getenv("MIN_AVAILABLE_MEMORY_MB", "512")))
    )
    checks["disk_available"] = (
        resources["free_disk_mb"] >= max(1, int(os.getenv("MIN_FREE_DISK_MB", "1024")))
    )
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        database_ready = False
        try:
            from sqlalchemy import text
            from procurement.platform.database import make_session_factory, session_scope

            with session_scope(make_session_factory()) as session:
                database_ready = session.execute(text("SELECT 1")).scalar_one() == 1
        except Exception:
            pass
        from procurement.platform.object_store import ready as object_storage_ready

        checks.update(
            {
                "postgres_ready": database_ready,
                "object_storage_ready": object_storage_ready(),
                "production_auth_configured": auth_configured,
            }
        )
    ok = all(checks.values())
    content: dict[str, Any] = {"status": "ready" if ok else "not_ready", "checks": checks}
    content["worker"] = {
        "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None
    }
    content["resources"] = resources
    if prompt_error:
        content["prompt_error"] = prompt_error
    if provider_error:
        content["provider_error"] = provider_error
    return JSONResponse(status_code=200 if ok else 503, content=content)
