"""Fail fast on invalid production configuration and unavailable providers."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from pathlib import Path

from ai_provider import (
    AIConfigurationError,
    api_key_for,
    settings as ai_settings,
    validate as validate_ai_settings,
)
from procurement.platform.env import EnvironmentValueError, env_int, load_local_env


def _missing(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return not value or "replace_" in value.lower()


def _executable_exists(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if Path(candidate).is_file():
        return True
    return shutil.which(candidate) is not None


def _trial_control_errors() -> list[str]:
    errors: list[str] = []
    try:
        worker_concurrency = env_int("WORKER_CONCURRENCY", 1, minimum=1)
        running_jobs = env_int("MAX_RUNNING_JOBS", 1, minimum=1)
        queue_capacity = env_int("MAX_QUEUED_OPERATIONS", 11, minimum=1, maximum=11)
        upload_mb = env_int("MAX_UPLOAD_MB", 50, minimum=1)
        bidder_limit = env_int("MAX_BIDDERS_PER_TENDER", 10, minimum=1)
        retention_days = env_int("RETENTION_DAYS", 0, minimum=0)
    except EnvironmentValueError as exc:
        return [str(exc)]
    if worker_concurrency != 1:
        errors.append("WORKER_CONCURRENCY must be 1 for this single-operation deployment")
    if running_jobs != 1:
        errors.append("MAX_RUNNING_JOBS must be 1 for this single-operation deployment")
    if queue_capacity != 11:
        errors.append("MAX_QUEUED_OPERATIONS must allow one running operation and ten pending operations")
    if upload_mb > 50:
        errors.append("MAX_UPLOAD_MB must not exceed 50")
    if bidder_limit != 10:
        errors.append("MAX_BIDDERS_PER_TENDER must be 10")
    if retention_days != 0:
        errors.append("RETENTION_DAYS must be 0 so trial data is retained indefinitely")
    if os.getenv("AUTH_MODE", "").strip().lower() not in {"session", "api_token"}:
        errors.append("AUTH_MODE must be session or api_token for local workspace authentication")
    if os.getenv("PROVIDER_USAGE_REQUIRED", "0").strip() != "1":
        errors.append("PROVIDER_USAGE_REQUIRED must be 1 so paid calls cannot bypass metering")
    decimal_values: dict[str, Decimal] = {}
    required_decimals = (
        "AI_BUDGET_LIMIT_INR",
        "USD_TO_INR_RATE",
        "AI_FAST_INPUT_COST_PER_MILLION_TOKENS",
        "AI_FAST_OUTPUT_COST_PER_MILLION_TOKENS",
        "AI_DEEP_INPUT_COST_PER_MILLION_TOKENS",
        "AI_DEEP_OUTPUT_COST_PER_MILLION_TOKENS",
        "AI_VERIFIER_INPUT_COST_PER_MILLION_TOKENS",
        "AI_VERIFIER_OUTPUT_COST_PER_MILLION_TOKENS",
        "AI_OCR_INPUT_COST_PER_MILLION_TOKENS",
        "AI_OCR_OUTPUT_COST_PER_MILLION_TOKENS",
        "AI_EMBEDDING_INPUT_COST_PER_MILLION_TOKENS",
        "AI_RERANKER_INPUT_COST_PER_MILLION_TOKENS",
        "AI_RERANKER_OUTPUT_COST_PER_MILLION_TOKENS",
        "OPENPARSER_COST_PER_PAGE_USD",
    )
    for name in required_decimals:
        try:
            value = Decimal(os.getenv(name, ""))
        except InvalidOperation:
            errors.append(f"{name} must be a positive decimal")
            continue
        if value <= 0:
            errors.append(f"{name} must be a positive decimal")
        else:
            decimal_values[name] = value
    revision = os.getenv("AI_PRICING_REVISION", "").strip().lower()
    if not revision or "replace_" in revision or revision == "unversioned":
        errors.append("AI_PRICING_REVISION must identify the configured provider prices")
    return errors


def _public_access_errors() -> list[str]:
    value = os.getenv("PUBLIC_BASE_URL", "").strip()
    remote_host = os.getenv("API_HOST", "127.0.0.1").strip() not in {
        "127.0.0.1", "::1", "localhost",
    }
    if not value:
        return ["PUBLIC_BASE_URL must be configured for remote access"] if remote_host else []
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ["PUBLIC_BASE_URL must be an absolute HTTP(S) URL"]
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return []
    errors: list[str] = []
    if parsed.scheme != "https":
        errors.append("PUBLIC_BASE_URL must use HTTPS for remote access")
    if os.getenv("AUTH_COOKIE_SECURE", "0") != "1":
        errors.append("AUTH_COOKIE_SECURE=1 is required for remote access")
    return errors


def _provider_endpoint_checks(
    settings_value: object,
    *,
    need_embedding: bool,
    need_ocr: bool,
    need_reranker: bool,
) -> list[str]:
    """Verify configured OpenAI-compatible capability endpoints before startup."""
    if os.getenv("AI_PROVIDER_HEALTHCHECK_ENABLED", "0") == "0":
        return []

    checks: list[tuple[str, str, str]] = []
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    if provider in {"openai", "openai-compatible", "openai_compatible", "yotta", "yota"}:
        checks.append(("primary", getattr(settings_value, "primary_base_url", ""), "primary"))
    if need_ocr and os.getenv("AI_OCR_BASE_URL", "").strip():
        checks.append(("OCR", getattr(settings_value, "ocr_base_url", ""), "ocr_pdf_page"))
    if need_embedding and os.getenv("AI_EMBEDDING_BASE_URL", "").strip():
        checks.append(("embedding", getattr(settings_value, "embedding_base_url", ""), "embedding"))
    if need_reranker and os.getenv("AI_RERANKER_BASE_URL", "").strip():
        checks.append(("reranker", getattr(settings_value, "reranker_base_url", ""), "rerank"))

    timeout = max(1, min(300, int(os.getenv("AI_PROVIDER_HEALTHCHECK_TIMEOUT_SECONDS", "5"))))
    attempts = max(1, min(10, int(os.getenv("AI_PROVIDER_HEALTHCHECK_ATTEMPTS", "3"))))
    retry_seconds = max(0, min(300, int(os.getenv("AI_PROVIDER_HEALTHCHECK_RETRY_SECONDS", "10"))))
    errors: list[str] = []
    for name, base_url, task in checks:
        base_url = str(base_url or "").rstrip("/")
        if not base_url:
            errors.append(f"{name} endpoint is configured but has no URL")
            continue
        key = api_key_for(settings_value, task)  # type: ignore[arg-type]
        capability = name.upper()
        deployment_id = (
            os.getenv(f"AI_{capability}_DEPLOYMENT_ID", "").strip()
            or os.getenv("AI_DEPLOYMENT_ID", "").strip()
        )
        headers = {"Authorization": f"Bearer {key}"}
        if deployment_id:
            headers["id"] = deployment_id
        endpoint = f"{base_url}/models"
        method = "GET"
        failure = ""
        for attempt in range(1, attempts + 1):
            try:
                request = Request(endpoint, headers=headers, method=method)
                with urlopen(request, timeout=timeout) as response:
                    if 200 <= int(response.status) < 300:
                        failure = ""
                        break
                    failure = f"{name} endpoint health check returned HTTP {response.status}"
            except HTTPError as exc:
                failure = f"{name} endpoint health check returned HTTP {exc.code}"
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                reason = getattr(exc, "reason", None) or type(exc).__name__
                failure = f"{name} endpoint health check failed: {reason}"
            if attempt < attempts:
                time.sleep(retry_seconds)
        if failure:
            errors.append(failure)
    return errors


def main() -> None:
    load_local_env()
    errors: list[str] = []
    if os.getenv("APP_ENV", "").strip().lower() != "production":
        errors.append("APP_ENV must be production")
    if os.getenv("PROCUREMENT_OFFLINE_LLM", "0") == "1":
        errors.append("PROCUREMENT_OFFLINE_LLM must be disabled in production")
    if os.getenv("COMMERCIAL_OPENING_ENABLED", "0") == "1":
        errors.append("COMMERCIAL_OPENING_ENABLED must remain disabled; price opening requires an audited project authorization")
    errors.extend(_public_access_errors())
    errors.extend(_trial_control_errors())
    evaluation_profile = os.getenv("BID_EVALUATION_PROFILE", "").strip().lower()
    if evaluation_profile not in {"", "none", "bhel_purchase_materials"}:
        errors.append("BID_EVALUATION_PROFILE must be empty, none, or bhel_purchase_materials")
    evaluation_profile_file = os.getenv("BID_EVALUATION_PROFILE_FILE", "").strip()
    if evaluation_profile_file and not Path(evaluation_profile_file).is_file():
        errors.append("BID_EVALUATION_PROFILE_FILE does not identify a readable profile JSON file")
    elif evaluation_profile == "bhel_purchase_materials":
        from procurement.bhel_evaluation import load_bhel_profile

        if load_bhel_profile() is None:
            errors.append("BHEL bid-evaluation profile is missing or invalid")
    if _missing("DATABASE_URL"):
        errors.append("DATABASE_URL is required")
    storage_backend = os.getenv("OBJECT_STORAGE_BACKEND", "filesystem").strip().lower()
    if storage_backend == "s3":
        for name in ("OBJECT_STORAGE_BUCKET", "OBJECT_STORAGE_ACCESS_KEY_ID", "OBJECT_STORAGE_SECRET_ACCESS_KEY"):
            if _missing(name):
                errors.append(f"{name} is required for OBJECT_STORAGE_BACKEND=s3")
    elif storage_backend != "filesystem":
        errors.append("OBJECT_STORAGE_BACKEND must be filesystem or s3")
    if os.getenv("API_HOST", "127.0.0.1").strip() not in {"127.0.0.1", "::1", "localhost"} and os.getenv("ALLOW_REMOTE_API", "0") != "1":
        errors.append("API_HOST must be loopback unless ALLOW_REMOTE_API=1")
    auth_mode = os.getenv("AUTH_MODE", "").strip().lower()
    if auth_mode == "jwt":
        for name in ("AUTH_JWKS_URL", "AUTH_ISSUER", "AUTH_AUDIENCE"):
            if _missing(name):
                errors.append(f"{name} is required for AUTH_MODE=jwt")
    elif auth_mode in {"session", "api_token"}:
        if _missing("SESSION_SECRET_KEY"):
            errors.append("SESSION_SECRET_KEY is required for AUTH_MODE=session")
    else:
        errors.append("AUTH_MODE must be session, api_token or jwt")
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    if provider != "gemini" and (
        os.getenv("AI_REQUIRE_INDIA_REGION", "") != "1"
        or os.getenv("AI_INFERENCE_REGION", "").strip().lower() != "india"
    ):
        errors.append("AI_REQUIRE_INDIA_REGION=1 and AI_INFERENCE_REGION=india are required")
    if provider in {"openai", "openai-compatible", "openai_compatible", "yotta", "yota"}:
        for name in ("AI_MODEL_FAST", "AI_MODEL_DEEP", "AI_MODEL_EMBEDDING", "AI_MODEL_VERIFIER"):
            if _missing(name):
                errors.append(f"{name} is required explicitly for AI_PROVIDER=openai_compatible")
    if provider != "gemini" and os.getenv("EVIDENCE_RERANKER_ENABLED", "0") == "1" and _missing("AI_MODEL_RERANKER"):
        errors.append("AI_MODEL_RERANKER is required explicitly when reranking is enabled")
    ocr_provider = os.getenv("AI_OCR_PROVIDER", provider).strip().lower()
    if ocr_provider in {"openai", "openai-compatible", "openai_compatible", "yotta", "yota"}:
        if os.getenv("BASE_RFP_OCR_PROVIDER", "").strip().lower() == "ai":
            for name in ("AI_OCR_BASE_URL", "AI_MODEL_OCR"):
                if _missing(name):
                    errors.append(f"{name} is required explicitly for OpenAI-compatible OCR")
            key_file = os.getenv("AI_OCR_API_KEY_FILE", "").strip()
            if (
                _missing("AI_OCR_API_KEY")
                and _missing("AI_API_KEY")
                and (not key_file or not Path(key_file).is_file())
            ):
                errors.append(
                    "AI_OCR_API_KEY, AI_OCR_API_KEY_FILE, or AI_API_KEY is required "
                    "for OpenAI-compatible OCR"
                )
    if os.getenv("BASE_RFP_VISUAL_OCR_FALLBACK_ENABLED", "0") != "0":
        errors.append("BASE_RFP_VISUAL_OCR_FALLBACK_ENABLED must be 0; production provider fallback is disabled")
    if os.getenv("BASE_RFP_OCR_PROVIDER", "").strip().lower() == "paddle_api":
        for name in ("PADDLEOCR_ACCESS_TOKEN", "PADDLEOCR_BASE_URL", "BASE_RFP_PADDLE_API_MODEL"):
            if _missing(name):
                errors.append(f"{name} is required for BASE_RFP_OCR_PROVIDER=paddle_api")
    if os.getenv("BASE_RFP_OCR_PROVIDER", "").strip().lower() == "openparser":
        for name in ("OPENPARSER_API_KEY", "OPENPARSER_BASE_URL", "OPENPARSER_OCR_MODEL"):
            if _missing(name):
                errors.append(f"{name} is required for BASE_RFP_OCR_PROVIDER=openparser")
    converter = os.getenv("OFFICE_CONVERTER_BIN", "").strip()
    if converter and not _executable_exists(converter):
        errors.append("OFFICE_CONVERTER_BIN does not identify an installed LibreOffice/soffice executable")
    try:
        need_embedding = (
            os.getenv("EVIDENCE_EMBEDDING_ENABLED", "0") == "1"
            or os.getenv(
                "RFP_FIELD_EMBEDDING_ENABLED",
                os.getenv("EVIDENCE_EMBEDDING_ENABLED", "0"),
            )
            == "1"
        )
        need_reranker = (
            os.getenv("EVIDENCE_RERANKER_ENABLED", "0") == "1"
            or os.getenv(
                "RFP_FIELD_RERANKER_ENABLED",
                os.getenv("EVIDENCE_RERANKER_ENABLED", "0"),
            )
            == "1"
        )
        validate_ai_settings(
            ai_settings(),
            need_embedding=need_embedding,
            need_ocr=os.getenv("BASE_RFP_OCR_PROVIDER", "").strip().lower() == "ai",
            need_reranker=need_reranker,
            need_verifier=provider != "gemini",
        )
    except AIConfigurationError as exc:
        errors.append(str(exc))
    if not errors:
        endpoint_errors = _provider_endpoint_checks(
            ai_settings(),
            need_embedding=need_embedding,
            need_ocr=os.getenv("BASE_RFP_OCR_PROVIDER", "").strip().lower() == "ai",
            need_reranker=need_reranker,
        )
        if os.getenv("AI_PROVIDER_HEALTHCHECK_REQUIRED", "0") == "1":
            errors.extend(endpoint_errors)
        elif endpoint_errors:
            print("Provider health warning; API startup will continue:", file=sys.stderr)
            print("\n".join(f"- {error}" for error in endpoint_errors), file=sys.stderr)
    if errors:
        print("Production configuration is incomplete:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        raise SystemExit(2)
    print("Production configuration is valid.")


if __name__ == "__main__":
    main()
