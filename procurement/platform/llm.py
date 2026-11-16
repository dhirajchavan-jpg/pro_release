"""Structured LLM calls shared by understanding, compliance, and evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_provider import (
    AIConfigurationError,
    AIProviderHTTPError,
    AIProviderTruncatedError,
    classify_provider_exception,
    ensure_context_budget,
    base_url_for as provider_base_url_for,
    model_for as provider_model_for,
    openai_compatible_content,
    parse_json_object as parse_provider_json_object,
    settings as ai_settings,
)
from procurement.platform.env import load_local_env
from procurement.platform.files import write_json_atomic
from procurement.platform.provider_usage import (
    fingerprint,
    record_provider_call,
    check_provider_budget,
)
from procurement.providers.settings import _capability_for_task
from procurement.platform.job_context import (
    JOB_CONTEXT,
    check_job_deadline,
    gemini_call_timeout_seconds,
    gemini_retry_delay_seconds,
    record_llm_usage,
)


load_local_env()

logger = logging.getLogger("procurement_evaluator")

GEMINI_MODEL_FAST = os.getenv(
    "AI_MODEL_FAST",
    os.getenv("GEMINI_MODEL_FAST", os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")),
)
GEMINI_MODEL_DEEP = os.getenv("AI_MODEL_DEEP", os.getenv("GEMINI_MODEL_DEEP", GEMINI_MODEL_FAST))
GEMINI_BASE_URL = os.getenv(
    "AI_BASE_URL",
    os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
)
GEMINI_MAX_OUTPUT_TOKENS = int(
    os.getenv("AI_MAX_OUTPUT_TOKENS", os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "65536"))
)
GEMINI_THINKING_BUDGET = int(
    os.getenv("AI_THINKING_BUDGET", os.getenv("GEMINI_THINKING_BUDGET", "0"))
)
GEMINI_RETRY_ATTEMPTS = int(
    os.getenv("AI_RETRY_ATTEMPTS", os.getenv("GEMINI_RETRY_ATTEMPTS", "3"))
)
GEMINI_RETRY_BASE_SECONDS = int(
    os.getenv("AI_RETRY_BASE_SECONDS", os.getenv("GEMINI_RETRY_BASE_SECONDS", "5"))
)
GEMINI_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("AI_REQUEST_TIMEOUT_SECONDS", os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "45"))
)
GEMINI_INPUT_COST_PER_MILLION_TOKENS = float(
    os.getenv("GEMINI_INPUT_COST_PER_MILLION_TOKENS", "0")
)
GEMINI_OUTPUT_COST_PER_MILLION_TOKENS = float(
    os.getenv("GEMINI_OUTPUT_COST_PER_MILLION_TOKENS", "0")
)
PROCUREMENT_OFFLINE_LLM = os.getenv("PROCUREMENT_OFFLINE_LLM") == "1"
AI_VERIFIER_SUPPORTS_VISION = os.getenv(
    "AI_VERIFIER_SUPPORTS_VISION",
    "1" if os.getenv("AI_PROVIDER", "gemini").strip().lower() == "gemini" else "0",
) == "1"
LLM_JSON_CACHE_VERSION = "llm-json-v1"


class GeminiOutputTruncated(AIProviderTruncatedError):
    def __init__(self, message: str, *, partial_text: str = ""):
        super().__init__(message)
        # Keep source content out of exception messages and ordinary logs.
        self.partial_text = partial_text


def llm_json(
    system: str,
    payload: dict[str, Any],
    model: str | None = None,
    *,
    response_schema: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
    thinking_budget: int | None = None,
    media_parts: list[dict[str, Any]] | None = None,
    json_retry: bool = True,
) -> dict[str, Any]:
    check_job_deadline()
    if PROCUREMENT_OFFLINE_LLM:
        from scripts.offline_llm import offline_llm_json

        return offline_llm_json(system, payload, model)
    api_key = gemini_api_key()
    if not api_key:
        raise RuntimeError("Set AI_API_KEY (or the selected provider's API key) before running an evaluation job")

    request_text = "\n\n".join(
        [
            system,
            "Return exactly one JSON object. Keep every field concise and omit unrequested narration.",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ]
    )
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": request_text}, *(media_parts or [])],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "thinkingConfig": {
                "thinkingBudget": (
                    GEMINI_THINKING_BUDGET
                    if thinking_budget is None
                    else max(0, int(thinking_budget))
                )
            },
        },
    }
    task = str(payload.get("task") or "llm_json")
    capability = (
        "VERIFIER"
        if any(marker in task for marker in ("verify", "verifier", "judge"))
        else "OCR"
        if any(marker in task for marker in ("ocr", "signature", "structural"))
        else "RERANKER"
        if "rerank" in task
        else "PRIMARY"
    )
    output_limit = (
        int(os.getenv(f"AI_{capability}_MAX_OUTPUT_TOKENS", "4096"))
        if max_output_tokens is None
        else max_output_tokens
    )
    if output_limit > 0:
        body["generationConfig"]["maxOutputTokens"] = output_limit
    if response_schema:
        body["generationConfig"]["responseSchema"] = response_schema
    cache_path: Path | None = None
    if os.getenv("LLM_JSON_CACHE_ENABLED", "1") == "1":
        provider_settings = ai_settings()
        effective_task = task
        if any(part.get("inline_data") for part in media_parts or []) and "vision" not in task:
            effective_task = f"{task}_vision"
        resolved_model = provider_model_for(
            provider_settings,
            model or GEMINI_MODEL_FAST,
            effective_task,
        )
        cache_request = {
            "version": LLM_JSON_CACHE_VERSION,
            "revision": os.getenv(
                "AI_CACHE_REVISION",
                os.getenv("AI_DEPLOYMENT_REVISION", "1"),
            ),
            "provider": provider_settings.provider,
            "task_provider": (
                os.getenv("AI_OCR_PROVIDER", provider_settings.provider).strip().lower()
                if any(marker in effective_task for marker in ("ocr", "signature", "structural"))
                else provider_settings.provider
            ),
            "base_url": provider_base_url_for(provider_settings, effective_task).rstrip("/"),
            "provider_base_url": provider_settings.base_url.rstrip("/"),
            "native_gemini_base_url": GEMINI_BASE_URL.rstrip("/"),
            "model": resolved_model,
            "task": effective_task,
            "response_format": provider_settings.response_format,
            "body": body,
        }
        cache_key = hashlib.sha256(
            json.dumps(
                cache_request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        cache_root = Path(os.getenv("LLM_JSON_CACHE_DIR", "runtime/cache/llm-json"))
        cache_path = cache_root / cache_key[:2] / f"{cache_key}.json"
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached.get("version") == LLM_JSON_CACHE_VERSION
                and cached.get("request_sha256") == cache_key
                and isinstance(cached.get("result"), dict)
            ):
                logger.info(
                    "AI cache hit job_id=%s model=%s task=%s",
                    getattr(JOB_CONTEXT, "job_id", "unknown"),
                    resolved_model,
                    effective_task,
                )
                record_provider_call(
                    capability=capability,
                    provider=provider_settings.provider,
                    model=resolved_model,
                    task=effective_task,
                    status="cache_hit",
                    duration_ms=0,
                    endpoint=provider_base_url_for(provider_settings, effective_task),
                    cache_status="hit",
                    request_fingerprint=cache_key,
                )
                return cached["result"]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    try:
        raw = post_gemini_content(api_key, model or GEMINI_MODEL_FAST, body, task=task)
    except AIProviderTruncatedError:
        if not json_retry:
            raise
        logger.warning("LLM response was truncated for %s; retrying once with compact output", task)
        body["contents"][0]["parts"][0]["text"] = "\n\n".join(
            [
                request_text,
                "Your previous response exceeded the output limit. Return the same complete JSON schema with concise values: reason at most 240 characters, quote at most 240 characters, no repetition or narration.",
            ]
        )
        raw = post_gemini_content(
            api_key,
            model or GEMINI_MODEL_FAST,
            body,
            task=f"{task}_truncation_retry",
        )
    try:
        result = parse_json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        if not json_retry:
            raise
        logger.warning("LLM returned invalid JSON for %s; retrying once: %s", task, exc)
        body["contents"][0]["parts"][0]["text"] = "\n\n".join(
            [
                request_text,
                "Your previous response was invalid JSON. Return a complete, valid JSON object only. No markdown, no trailing text, no truncated strings.",
                f"Previous parse error: {str(exc)[:240]}",
            ]
        )
        raw = post_gemini_content(
            api_key,
            model or GEMINI_MODEL_FAST,
            body,
            task=f"{task}_json_retry",
        )
        result = parse_json_object(raw)
    if cache_path:
        try:
            write_json_atomic(
                cache_path,
                {
                    "version": LLM_JSON_CACHE_VERSION,
                    "request_sha256": cache_path.stem,
                    "result": result,
                },
                indent=None,
            )
        except OSError:
            logger.warning("Unable to write AI response cache for %s", task)
    return result


def gemini_api_key() -> str | None:
    if PROCUREMENT_OFFLINE_LLM:
        return "offline-fixture"
    try:
        return ai_settings().api_key
    except AIConfigurationError:
        return None


def post_gemini_content(
    api_key: str,
    model: str,
    body: dict[str, Any],
    attempt: int = 1,
    task: str = "gemini_content",
) -> str:
    if any(
        part.get("inline_data")
        for content in body.get("contents") or []
        for part in content.get("parts") or []
        if isinstance(part, dict)
    ) and "vision" not in task:
        task = f"{task}_vision"
    if attempt == 1:
        check_provider_budget(task)
    check_job_deadline()
    provider_settings = ai_settings()
    model = provider_model_for(provider_settings, model, task)
    capability = _capability_for_task(task)
    context_budget = ensure_context_budget(body, task)
    request_hash = fingerprint({"model": model, "task": task, "body": body})
    request_started = time.perf_counter()
    logger.info(
        "AI started job_id=%s provider=%s model=%s task=%s attempt=%s estimated_input_tokens=%s safe_input_tokens=%s",
        getattr(JOB_CONTEXT, "job_id", "unknown"),
        provider_settings.provider,
        model,
        task,
        attempt,
        context_budget["estimated_input_tokens"],
        context_budget["safe_input_tokens"],
    )
    task_provider = provider_settings.provider
    if any(marker in task for marker in ("ocr", "signature", "structural")):
        task_provider = os.getenv("AI_OCR_PROVIDER", task_provider).strip().lower()
        if task_provider in {"openai", "openai-compatible", "yotta", "yota"}:
            task_provider = "openai_compatible"
    if task_provider == "openai_compatible":
        try:
            raw, usage = openai_compatible_content(
                replace(provider_settings, provider="openai_compatible"),
                model,
                body,
                task=task,
                timeout=gemini_call_timeout_seconds(),
                attempts=GEMINI_RETRY_ATTEMPTS,
                retry_delay=gemini_retry_delay_seconds,
            )
        except AIProviderTruncatedError as exc:
            raise GeminiOutputTruncated(str(exc)) from exc
        except Exception:
            logger.exception(
                "AI failed job_id=%s provider=%s model=%s task=%s duration_seconds=%.3f",
                getattr(JOB_CONTEXT, "job_id", "unknown"),
                provider_settings.provider,
                model,
                task,
                time.perf_counter() - request_started,
            )
            raise
        record_llm_usage(
            task,
            provider_settings.model_ocr
            if "ocr" in task or "signature" in task or "structural" in task
            else model,
            usage,
        )
        logger.info(
            "AI complete job_id=%s provider=%s model=%s task=%s prompt_tokens=%s output_tokens=%s duration_seconds=%.3f",
            getattr(JOB_CONTEXT, "job_id", "unknown"),
            provider_settings.provider,
            model,
            task,
            usage.get("promptTokenCount", usage.get("prompt_tokens", 0)),
            usage.get("candidatesTokenCount", usage.get("completion_tokens", 0)),
            time.perf_counter() - request_started,
        )
        return raw
    if provider_settings.provider != "gemini":
        raise AIConfigurationError(f"Unsupported AI_PROVIDER: {provider_settings.provider}")
    escaped_model = urllib.parse.quote(model, safe="")
    url = f"{GEMINI_BASE_URL.rstrip('/')}/models/{escaped_model}:generateContent"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "procurement-evaluator/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=gemini_call_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="ignore")
        if exc.code == 400 and "thinkingConfig" in message and body.get("generationConfig", {}).get("thinkingConfig"):
            retry_body = dict(body)
            retry_body["generationConfig"] = dict(body.get("generationConfig", {}))
            retry_body["generationConfig"].pop("thinkingConfig", None)
            return post_gemini_content(api_key, model, retry_body, attempt + 1, task=task)
        failure = classify_provider_exception(exc)
        if failure.retryable and attempt < GEMINI_RETRY_ATTEMPTS:
            check_job_deadline()
            time.sleep(gemini_retry_delay_seconds(attempt, failure.retry_after))
            return post_gemini_content(api_key, model, body, attempt + 1, task=task)
        record_provider_call(
            capability=capability,
            provider=provider_settings.provider,
            model=model,
            task=task,
            status="failed",
            duration_ms=round((time.perf_counter() - request_started) * 1000),
            endpoint=GEMINI_BASE_URL,
            attempts=attempt,
            request_fingerprint=request_hash,
            error_category=failure.category,
            http_status=failure.status_code,
        )
        if isinstance(failure, AIProviderHTTPError):
            raise AIProviderHTTPError(
                f"Gemini request failed: {exc.code} {message}",
                status_code=exc.code,
                retryable=failure.retryable,
                retry_after=failure.retry_after,
            ) from exc
        raise failure from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        failure = classify_provider_exception(exc)
        if failure.retryable and attempt < GEMINI_RETRY_ATTEMPTS:
            check_job_deadline()
            time.sleep(gemini_retry_delay_seconds(attempt, failure.retry_after))
            return post_gemini_content(api_key, model, body, attempt + 1, task=task)
        record_provider_call(
            capability=capability,
            provider=provider_settings.provider,
            model=model,
            task=task,
            status="failed",
            duration_ms=round((time.perf_counter() - request_started) * 1000),
            endpoint=GEMINI_BASE_URL,
            attempts=attempt,
            request_fingerprint=request_hash,
            error_category=failure.category,
            http_status=failure.status_code,
        )
        raise failure from exc

    usage = payload.get("usageMetadata") or {}
    record_llm_usage(task, model, usage)

    def record_invalid_output(category: str) -> None:
        record_provider_call(
            capability=capability,
            provider=provider_settings.provider,
            model=model,
            task=task,
            status="succeeded",
            duration_ms=round((time.perf_counter() - request_started) * 1000),
            endpoint=GEMINI_BASE_URL,
            attempts=attempt,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            total_tokens=int(usage.get("totalTokenCount") or 0),
            request_fingerprint=request_hash,
            response_fingerprint=fingerprint(payload),
            error_category=category,
            metrics={
                "estimated_input_tokens": context_budget["estimated_input_tokens"],
                "valid_output": False,
            },
        )

    candidates = payload.get("candidates") or []
    if not candidates:
        record_invalid_output("empty_output")
        raise RuntimeError("Gemini returned no candidates")
    if str(candidates[0].get("finishReason") or "") == "MAX_TOKENS":
        record_invalid_output("truncated_output")
        parts = candidates[0].get("content", {}).get("parts") or []
        raise GeminiOutputTruncated(f"provider response reached maxOutputTokens for {task}",
            partial_text="".join(part.get("text", "") for part in parts if not part.get("thought")))
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if "text" in part).strip()
    if not text:
        record_invalid_output("empty_output")
        raise RuntimeError("Gemini returned no text")
    record_provider_call(
        capability=capability,
        provider=provider_settings.provider,
        model=model,
        task=task,
        status="succeeded",
        duration_ms=round((time.perf_counter() - request_started) * 1000),
        endpoint=GEMINI_BASE_URL,
        attempts=attempt,
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0),
        total_tokens=int(usage.get("totalTokenCount") or 0),
        request_fingerprint=request_hash,
        response_fingerprint=fingerprint(text),
        metrics={"estimated_input_tokens": context_budget["estimated_input_tokens"]},
    )
    logger.info(
        "AI complete job_id=%s provider=%s model=%s task=%s prompt_tokens=%s output_tokens=%s duration_seconds=%.3f",
        getattr(JOB_CONTEXT, "job_id", "unknown"),
        provider_settings.provider,
        model,
        task,
        usage.get("promptTokenCount", 0),
        usage.get("candidatesTokenCount", 0),
        time.perf_counter() - request_started,
    )
    return text


def parse_json_object(raw: str) -> dict[str, Any]:
    return parse_provider_json_object(raw)
