"""Strict JSON parsing and structured provider calls."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from procurement.platform.files import write_json_atomic
from procurement.platform.job_context import record_llm_usage
from procurement.platform.provider_usage import fingerprint, record_provider_call

from .errors import AIProviderTruncatedError, classify_provider_exception
from .retry import _provider_attempts, call_with_provider_retry, provider_slot
from .settings import (
    _capability_for_task,
    _provider_timeout,
    api_key_for,
    base_url_for,
    model_for,
    settings,
)
from .transport import ensure_context_budget, openai_client, warmup_chat_if_needed


logger = logging.getLogger("procurement.ai_provider")
_CACHE_VERSION = "provider-json-v1"


def parse_json_object(raw: str) -> dict[str, Any]:
    """Accept one fenced/wrapped JSON object, but never merge or guess data."""
    if not isinstance(raw, str):
        raise ValueError("provider output is not text")
    cleaned = raw.strip().lstrip("\ufeff")
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, count=1, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned, count=1)
    start = cleaned.find("{")
    if start < 0:
        raise json.JSONDecodeError("provider output does not contain a JSON object", cleaned, 0)
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(cleaned[start:])
    trailing = cleaned[start + end :].strip()
    if trailing and any(marker in trailing for marker in ("{", "[")):
        raise json.JSONDecodeError("provider returned multiple JSON values", cleaned, start + end)
    if not isinstance(value, dict):
        raise ValueError("provider returned JSON, but not an object")
    return value


def json_chat(
    task: str,
    prompt: str,
    *,
    timeout: int = 60,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    factory = client_factory or openai_client
    settings_value = settings()
    model = model_for(settings_value, "", task)
    timeout = _provider_timeout(task, timeout)
    provider_attempts = _provider_attempts(task)
    cache_key = fingerprint(
        {
            "version": _CACHE_VERSION,
            "revision": os.getenv("AI_CACHE_REVISION", os.getenv("AI_DEPLOYMENT_REVISION", "1")),
            "provider": settings_value.provider,
            "base_url": base_url_for(settings_value, task).rstrip("/"),
            "model": model,
            "task": task,
            "prompt": prompt,
            "response_format": settings_value.response_format,
        }
    )
    cache_path = Path(os.getenv("AI_PROVIDER_JSON_CACHE_DIR", "runtime/cache/provider-json")) / cache_key[:2] / f"{cache_key}.json"
    if os.getenv("LLM_JSON_CACHE_ENABLED", "1") == "1":
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached.get("version") == _CACHE_VERSION
                and cached.get("request_sha256") == cache_key
                and isinstance(cached.get("result"), dict)
            ):
                logger.info("AI cache hit model=%s task=%s", model, task)
                record_provider_call(
                    capability=_capability_for_task(task),
                    provider=settings_value.provider,
                    model=model,
                    task=task,
                    status="cache_hit",
                    duration_ms=0,
                    endpoint=base_url_for(settings_value, task),
                    cache_status="hit",
                    request_fingerprint=cache_key,
                )
                return cached["result"]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    correction = ""
    attempts = (
        max(1, min(3, int(os.getenv("AI_RERANKER_JSON_ATTEMPTS", "2"))))
        if task == "rerank"
        else 2
    )
    for attempt in range(attempts):
        request: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_tokens": max(
                128,
                int(
                    os.getenv(
                        f"AI_{_capability_for_task(task)}_MAX_OUTPUT_TOKENS",
                        "4096",
                    )
                ),
            ),
            "messages": [
                {
                    "role": "user",
                    "content": prompt + "\nReturn exactly one JSON object and no markdown." + correction,
                }
            ],
        }
        if settings_value.response_format == "json_object":
            request["response_format"] = {"type": "json_object"}
        ensure_context_budget(
            {
                "contents": [{"parts": [{"text": request["messages"][0]["content"]}]}],
                "generationConfig": {"maxOutputTokens": request["max_tokens"]},
            },
            task,
        )
        client = factory(
            replace(settings_value, api_key=api_key_for(settings_value, task)),
            timeout,
            base_url_for(settings_value, task),
            task=task,
        )
        request_started = time.monotonic()
        call_attempts = 0
        request_hash = fingerprint(request)
        last_response: Any | None = None

        def create_completion() -> Any:
            nonlocal call_attempts, last_response
            call_attempts += 1
            response = client.chat.completions.create(**request)
            last_response = response
            if response.choices and str(getattr(response.choices[0], "finish_reason", "") or "").lower() in {"length", "max_tokens"}:
                raise AIProviderTruncatedError(f"{task} response was truncated")
            return response

        try:
            with provider_slot(settings_value, task) as gate:
                warmup_chat_if_needed(gate, client, model, task, provider_attempts)
                response = call_with_provider_retry(
                    create_completion,
                    task=task,
                    attempts=provider_attempts,
                )
                gate.mark_success()
        except Exception as exc:
            failure = classify_provider_exception(exc)
            usage = getattr(last_response, "usage", None)
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            response_content = ""
            if last_response is not None and getattr(last_response, "choices", None):
                response_content = str(
                    getattr(last_response.choices[0].message, "content", "") or ""
                )
            record_provider_call(
                capability=_capability_for_task(task),
                provider=settings_value.provider,
                model=model,
                task=task,
                status="succeeded" if last_response is not None else "failed",
                duration_ms=round((time.monotonic() - request_started) * 1000),
                endpoint=base_url_for(settings_value, task),
                attempts=call_attempts,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=int(
                    getattr(usage, "total_tokens", 0) or input_tokens + output_tokens
                ),
                request_fingerprint=request_hash,
                response_fingerprint=(
                    fingerprint(response_content) if last_response is not None else None
                ),
                error_category=failure.category,
                http_status=failure.status_code,
                metrics={"valid_output": False} if last_response is not None else None,
            )
            if not isinstance(exc, AIProviderTruncatedError):
                raise
            if attempt + 1 >= attempts:
                raise
            correction = "\nThe previous response was truncated. Return a complete JSON object within the output limit."
            continue
        content = str(response.choices[0].message.content or "") if response.choices else ""
        usage = getattr(response, "usage", None)
        usage_values = {
            "promptTokenCount": int(getattr(usage, "prompt_tokens", 0) or 0),
            "candidatesTokenCount": int(getattr(usage, "completion_tokens", 0) or 0),
            "totalTokenCount": int(getattr(usage, "total_tokens", 0) or 0),
        }
        record_llm_usage(task, model, usage_values)
        try:
            result = parse_json_object(content)
        except (json.JSONDecodeError, ValueError) as exc:
            record_provider_call(
                capability=_capability_for_task(task),
                provider=settings_value.provider,
                model=model,
                task=task,
                status="succeeded",
                duration_ms=round((time.monotonic() - request_started) * 1000),
                endpoint=base_url_for(settings_value, task),
                attempts=call_attempts,
                input_tokens=usage_values["promptTokenCount"],
                output_tokens=usage_values["candidatesTokenCount"],
                total_tokens=usage_values["totalTokenCount"],
                request_fingerprint=request_hash,
                response_fingerprint=fingerprint(content),
                metrics={"valid_json": False},
            )
            if attempt + 1 >= attempts:
                raise RuntimeError(f"{task} returned invalid JSON") from exc
            correction = "\nThe previous response was invalid. Return one complete JSON object only."
            continue
        record_provider_call(
            capability=_capability_for_task(task),
            provider=settings_value.provider,
            model=model,
            task=task,
            status="succeeded",
            duration_ms=round((time.monotonic() - request_started) * 1000),
            endpoint=base_url_for(settings_value, task),
            attempts=call_attempts,
            input_tokens=usage_values["promptTokenCount"],
            output_tokens=usage_values["candidatesTokenCount"],
            total_tokens=usage_values["totalTokenCount"],
            request_fingerprint=request_hash,
            response_fingerprint=fingerprint(content),
            metrics={"valid_json": True},
        )
        if os.getenv("LLM_JSON_CACHE_ENABLED", "1") == "1":
            try:
                write_json_atomic(
                    cache_path,
                    {
                        "version": _CACHE_VERSION,
                        "request_sha256": cache_key,
                        "result": result,
                    },
                    indent=None,
                )
            except OSError:
                logger.warning("Unable to write provider JSON cache for %s", task)
        return result
    raise RuntimeError(f"{task} returned invalid JSON")


__all__ = ["json_chat", "parse_json_object"]
