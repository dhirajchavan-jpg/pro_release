"""OpenAI-compatible transport, request shaping, warm-up, and context guards."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from procurement.platform.provider_usage import fingerprint, record_provider_call

from .cache import cached_openai_client
from .errors import (
    AIConfigurationError,
    AIContextLimitError,
    AIProviderOutputError,
    AIProviderTruncatedError,
    classify_provider_exception,
)
from .retry import (
    _ProviderGate,
    _provider_attempts,
    call_with_provider_retry,
    provider_slot,
)
from .settings import (
    _capability_for_task,
    _is_verifier_task,
    _provider_group,
    _provider_timeout,
    api_key_for,
    base_url_for,
    model_for,
    settings,
    validate,
)


logger = logging.getLogger("procurement.ai_provider")
_PDF_RENDER_LOCK = threading.Lock()


def openai_client(
    settings_value: Any,
    timeout: int,
    base_url: str | None = None,
    *,
    task: str = "primary",
    client_cache: Callable[..., Any] | None = None,
) -> Any:
    deployment_id = (
        os.getenv(f"AI_{_capability_for_task(task)}_DEPLOYMENT_ID", "").strip()
        or os.getenv("AI_DEPLOYMENT_ID", "").strip()
    )
    cache = client_cache or cached_openai_client
    return cache(
        settings_value.api_key or "",
        base_url or settings_value.primary_base_url,
        timeout,
        deployment_id,
    )


def pdf_as_data_url(data: str) -> str:
    try:
        import pypdfium2 as pdfium
        from PIL import Image

        # ponytail: PDFium's Windows DLL crashes under concurrent rendering.
        # Keep provider calls concurrent; serialize only this short native render.
        with _PDF_RENDER_LOCK:
            document = pdfium.PdfDocument(base64.b64decode(data))
            try:
                image = document[0].render(scale=2.0).to_pil()
            finally:
                document.close()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:
        raise AIConfigurationError(f"Unable to convert PDF OCR input for OpenAI-compatible provider: {exc}") from exc


def messages_from_gemini_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for content in body.get("contents") or []:
        parts: list[dict[str, Any]] = []
        for part in content.get("parts") or []:
            if "text" in part:
                parts.append({"type": "text", "text": str(part["text"])})
            elif isinstance(part.get("inline_data"), dict):
                inline = part["inline_data"]
                mime = str(inline.get("mime_type") or "application/octet-stream")
                data = str(inline.get("data") or "")
                url = pdf_as_data_url(data) if mime == "application/pdf" else f"data:{mime};base64,{data}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        if parts:
            messages.append({"role": "assistant" if content.get("role") == "model" else "user", "content": parts})
    if not messages:
        raise AIConfigurationError("AI request has no content")
    return messages


def warmup_chat_if_needed(
    gate: _ProviderGate,
    client: Any,
    model: str,
    task: str,
    attempts: int,
) -> None:
    if _provider_group(task) != "LLM" or os.getenv("AI_LLM_WARMUP_ENABLED", "0") != "1":
        return
    recent_failure = gate.recent_failure()
    if recent_failure is not None:
        raise recent_failure
    ttl = max(1, int(os.getenv("AI_LLM_WARM_TTL_SECONDS", os.getenv("AI_PROVIDER_WARM_TTL_SECONDS", "300"))))
    with gate.state_lock:
        if time.monotonic() - gate.last_success < ttl:
            return
    with gate.warm_lock:
        recent_failure = gate.recent_failure()
        if recent_failure is not None:
            raise recent_failure
        with gate.state_lock:
            if time.monotonic() - gate.last_success < ttl:
                return
        started = time.monotonic()
        logger.info("AI provider warmup started capability=llm model=%s", model)
        call_attempts = 0

        def warm() -> Any:
            nonlocal call_attempts
            call_attempts += 1
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply OK."}],
                temperature=0,
                max_tokens=8,
            )
            if not response.choices:
                raise AIProviderOutputError("OpenAI-compatible warmup returned no response", category="empty_output")
            return response

        try:
            response = call_with_provider_retry(
                warm,
                task="llm_warmup",
                attempts=attempts,
            )
        except Exception as exc:
            failure = classify_provider_exception(exc)
            gate.mark_failure(failure)
            settings_value = settings()
            record_provider_call(
                capability="primary",
                provider=str(getattr(settings_value, "provider", "openai_compatible")),
                model=model,
                task="llm_warmup",
                status="failed",
                duration_ms=round((time.monotonic() - started) * 1000),
                endpoint=getattr(settings_value, "primary_base_url", None),
                attempts=call_attempts,
                error_category=failure.category,
                http_status=failure.status_code,
                request_fingerprint=fingerprint({"model": model, "task": "llm_warmup"}),
            )
            raise failure from exc
        gate.mark_success()
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        settings_value = settings()
        record_provider_call(
            capability="primary",
            provider=str(getattr(settings_value, "provider", "openai_compatible")),
            model=model,
            task="llm_warmup",
            status="succeeded",
            duration_ms=round((time.monotonic() - started) * 1000),
            endpoint=getattr(settings_value, "primary_base_url", None),
            attempts=call_attempts,
            input_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=int(getattr(usage, "total_tokens", 0) or prompt_tokens + output_tokens),
            request_fingerprint=fingerprint({"model": model, "task": "llm_warmup"}),
        )
        logger.info(
            "AI provider warmup complete capability=llm model=%s duration_seconds=%.3f",
            model,
            time.monotonic() - started,
        )


def warmup_llm(
    model: str | None = None,
    timeout: int = 60,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> None:
    """Start the configured OpenAI-compatible LLM before expensive local preparation."""
    if os.getenv("AI_LLM_WARMUP_ENABLED", "0") != "1":
        return
    settings_value = settings()
    selected_model = model or settings_value.model_deep or settings_value.model_fast
    timeout = _provider_timeout("primary", timeout)
    factory = client_factory or openai_client
    client = factory(
        replace(settings_value, api_key=api_key_for(settings_value, "primary")),
        timeout,
        settings_value.primary_base_url,
        task="primary",
    )
    with provider_slot(settings_value, "primary") as gate:
        warmup_chat_if_needed(
            gate,
            client,
            selected_model,
            "primary",
            _provider_attempts("primary"),
        )


def ensure_context_budget(body: dict[str, Any], task: str) -> dict[str, int]:
    """Fail before a provider silently truncates an over-budget request."""
    capability = _capability_for_task(task)
    context = max(
        1024,
        int(
            os.getenv(
                f"AI_{capability}_CONTEXT_TOKENS",
                os.getenv("AI_CONTEXT_TOKENS", "131072"),
            )
        ),
    )
    generation = body.get("generationConfig") or {}
    output = max(
        1,
        int(
            generation.get("maxOutputTokens")
            or os.getenv(f"AI_{capability}_MAX_OUTPUT_TOKENS", "4096")
        ),
    )
    safety_percent = min(40, max(0, int(os.getenv("AI_CONTEXT_SAFETY_PERCENT", "10"))))
    chars_per_token = max(1.0, float(os.getenv("AI_CONTEXT_CHARS_PER_TOKEN", "3")))
    text_chars = 0
    images = 0
    for content in body.get("contents") or []:
        for part in content.get("parts") or []:
            if "text" in part:
                text_chars += len(str(part.get("text") or ""))
            elif part.get("inline_data"):
                images += 1
    estimated_input = int(text_chars / chars_per_token) + images * max(
        1, int(os.getenv("AI_IMAGE_ESTIMATED_TOKENS", "2048"))
    )
    usable = int(context * (100 - safety_percent) / 100) - output
    if estimated_input > usable:
        raise AIContextLimitError(
            f"{capability.lower()} request requires approximately {estimated_input} input tokens; "
            f"configured safe input limit is {usable}. Split the evidence into additional packs."
        )
    return {
        "context_tokens": context,
        "estimated_input_tokens": estimated_input,
        "reserved_output_tokens": output,
        "safe_input_tokens": usable,
    }


def openai_compatible_content(
    settings_value: Any,
    requested_model: str,
    body: dict[str, Any],
    *,
    task: str,
    timeout: int,
    attempts: int,
    retry_delay: Callable[[int], float],
    client_factory: Callable[..., Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    validate(settings_value, need_ocr=("ocr" in task or "signature" in task or "structural" in task))
    model = model_for(settings_value, requested_model, task)
    timeout = _provider_timeout(task, timeout)
    attempts = _provider_attempts(task, attempts)
    ensure_context_budget(body, task)
    messages = messages_from_gemini_body(body)
    generation = body.get("generationConfig") or {}
    response_schema = generation.get("responseSchema")
    if response_schema:
        messages.insert(
            0,
            {
                "role": "system",
                "content": (
                    "Return exactly one JSON object matching this JSON Schema. "
                    "Use only enum values declared by the schema:\n"
                    + json.dumps(response_schema, ensure_ascii=True, separators=(",", ":"))
                ),
            },
        )
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": generation.get("temperature", 0),
    }
    if _capability_for_task(task) == "OCR":
        request["frequency_penalty"] = max(
            -2.0,
            min(2.0, float(os.getenv("AI_OCR_FREQUENCY_PENALTY", "1.0"))),
        )
    if generation.get("maxOutputTokens"):
        request["max_tokens"] = generation["maxOutputTokens"]
    capability = _capability_for_task(task)
    enable_thinking = os.getenv(f"AI_{capability}_ENABLE_THINKING", "").strip().lower()
    if enable_thinking:
        if enable_thinking not in {"0", "1", "false", "true"}:
            raise AIConfigurationError(
                f"AI_{capability}_ENABLE_THINKING must be 0, 1, false, or true"
            )
        request["extra_body"] = {
            "chat_template_kwargs": {
                "enable_thinking": enable_thinking in {"1", "true"}
            }
        }
    response_format = os.getenv(
        f"AI_{capability}_RESPONSE_FORMAT",
        settings_value.response_format,
    ).strip().lower()
    if response_format not in {"json_object", "json_schema", "none"}:
        raise AIConfigurationError(
            f"AI_{capability}_RESPONSE_FORMAT must be json_object, json_schema, or none"
        )
    if response_format == "json_schema" and response_schema:
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": re.sub(r"[^A-Za-z0-9_-]", "_", task)[:64] or "response",
                "strict": False,
                "schema": response_schema,
            },
        }
    elif response_format != "none":
        request["response_format"] = {"type": "json_object"}

    factory = client_factory or openai_client
    client = factory(
        replace(settings_value, api_key=api_key_for(settings_value, task)),
        timeout,
        base_url_for(settings_value, task),
        task=task,
    )
    request_started = time.monotonic()
    call_attempts = 0
    request_fingerprint = fingerprint(request)
    last_response: Any | None = None

    def create_completion() -> Any:
        nonlocal call_attempts, last_response
        call_attempts += 1
        response = client.chat.completions.create(**request)
        last_response = response
        if not response.choices:
            raise AIProviderOutputError("OpenAI-compatible provider returned no text", category="empty_output")
        choice = response.choices[0]
        if str(getattr(choice, "finish_reason", "") or "").lower() in {"length", "max_tokens"}:
            raise AIProviderTruncatedError("provider response was truncated")
        if not choice.message.content:
            raise AIProviderOutputError("OpenAI-compatible provider returned no text", category="empty_output")
        return response

    try:
        with provider_slot(settings_value, task) as gate:
            warmup_chat_if_needed(gate, client, model, task, attempts)
            response = call_with_provider_retry(
                create_completion,
                task=task,
                attempts=attempts,
                retry_delay=retry_delay,
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
            request_fingerprint=request_fingerprint,
            response_fingerprint=(
                fingerprint(response_content) if last_response is not None else None
            ),
            error_category=failure.category,
            http_status=failure.status_code,
            metrics={"valid_output": False} if last_response is not None else None,
        )
        raise
    usage = getattr(response, "usage", None)
    content = str(response.choices[0].message.content)
    usage_values = {
        "promptTokenCount": int(getattr(usage, "prompt_tokens", 0) or 0),
        "candidatesTokenCount": int(getattr(usage, "completion_tokens", 0) or 0),
        "totalTokenCount": int(getattr(usage, "total_tokens", 0) or 0),
    }
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
        request_fingerprint=request_fingerprint,
        response_fingerprint=fingerprint(content),
        metrics={
            "image_count": sum(
                1
                for message in messages
                for part in (message.get("content") if isinstance(message.get("content"), list) else [])
                if isinstance(part, dict) and part.get("type") == "image_url"
            )
        },
    )
    return content, usage_values


__all__ = [
    "_PDF_RENDER_LOCK",
    "ensure_context_budget",
    "messages_from_gemini_body",
    "openai_client",
    "openai_compatible_content",
    "pdf_as_data_url",
    "warmup_chat_if_needed",
    "warmup_llm",
]
