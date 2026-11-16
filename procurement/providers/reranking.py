"""OpenAI-compatible reranking with bounded, auditable degradation."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import replace
from typing import Any, Callable

from procurement.platform.provider_usage import fingerprint, record_provider_call

from .errors import AIConfigurationError, classify_provider_exception
from .json_protocol import json_chat
from .retry import _provider_attempts, call_with_provider_retry, provider_slot
from .settings import _provider_timeout, api_key_for, model_for, settings
from .transport import openai_client


logger = logging.getLogger("procurement.ai_provider")


def _rerank_excerpt(query: str, value: str) -> str:
    maximum = max(500, int(os.getenv("AI_RERANKER_CANDIDATE_CHARS", "1800")))
    if len(value) <= maximum:
        return value
    lowered = value.lower()
    positions = [
        lowered.find(token)
        for token in sorted(set(re.findall(r"[a-z0-9]{4,}", query.lower())), key=len, reverse=True)
        if lowered.find(token) >= 0
    ]
    if not positions:
        return value[:maximum]
    start = max(0, min(positions) - maximum // 4)
    return value[start : start + maximum]


def rerank_texts(
    query: str,
    values: list[str],
    *,
    top_n: int | None = None,
    timeout: int = 60,
    client_factory: Callable[..., Any] | None = None,
    json_chat_call: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not values:
        return []
    requested = len(values) if top_n is None else max(1, min(top_n, len(values)))
    try:
        if os.getenv("AI_RERANKER_API_STYLE", "chat").strip().lower() == "rerank":
            settings_value = settings()
            timeout = _provider_timeout("rerank", timeout)
            factory = client_factory or openai_client
            client = factory(
                replace(settings_value, api_key=api_key_for(settings_value, "rerank")),
                timeout,
                settings_value.reranker_base_url,
                task="rerank",
            )
            selected_model = model_for(settings_value, "", "rerank")
            documents = [_rerank_excerpt(query, value) for value in values]
            request_hash = fingerprint(
                {"model": selected_model, "query": query, "documents": documents, "top_n": requested}
            )
            started = time.monotonic()
            call_attempts = 0

            def create_rerank() -> Any:
                nonlocal call_attempts
                call_attempts += 1
                return client.post(
                    "/rerank",
                    cast_to=object,
                    body={
                        "model": selected_model,
                        "query": query,
                        "documents": documents,
                        "top_n": requested,
                        "return_documents": False,
                    },
                )

            with provider_slot(settings_value, "rerank") as gate:
                recent_failure = gate.recent_failure()
                if recent_failure is not None:
                    raise recent_failure
                try:
                    data = call_with_provider_retry(
                        create_rerank,
                        task="rerank",
                        attempts=_provider_attempts("rerank"),
                    )
                except Exception as exc:
                    failure = classify_provider_exception(exc)
                    gate.mark_failure(failure)
                    record_provider_call(
                        capability="reranker",
                        provider=settings_value.provider,
                        model=selected_model,
                        task="rerank",
                        status="failed",
                        duration_ms=round((time.monotonic() - started) * 1000),
                        endpoint=settings_value.reranker_base_url,
                        attempts=call_attempts,
                        input_units=len(values),
                        output_units=requested,
                        unit_name="document",
                        request_fingerprint=request_hash,
                        error_category=failure.category,
                        http_status=failure.status_code,
                    )
                    raise failure from exc
                gate.mark_success()
            usage = data.get("usage") if isinstance(data, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            record_provider_call(
                capability="reranker",
                provider=settings_value.provider,
                model=selected_model,
                task="rerank",
                status="succeeded",
                duration_ms=round((time.monotonic() - started) * 1000),
                endpoint=settings_value.reranker_base_url,
                attempts=call_attempts,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=int(usage.get("total_tokens") or input_tokens + output_tokens),
                input_units=len(values),
                output_units=requested,
                unit_name="document",
                request_fingerprint=request_hash,
                response_fingerprint=fingerprint(data),
            )
        else:
            ranker = json_chat_call or json_chat
            data = ranker(
                "rerank",
                json.dumps(
                    {
                        "task": "Rank the candidate texts by relevance to the query.",
                        "query": query,
                        "candidates": [
                            {"index": index, "text": _rerank_excerpt(query, value)}
                            for index, value in enumerate(values)
                        ],
                        "required_output": {
                            "indices": [0],
                            "rules": "Return exactly top_n unique candidate indices, best match first.",
                        },
                        "top_n": requested,
                    },
                    ensure_ascii=True,
                ),
                timeout=timeout,
            )
        results = data.get("results")
        indices = data.get("indices")
        if isinstance(indices, list):
            results = [{"index": index, "score": 1.0} for index in indices]
        if not isinstance(results, list):
            raise RuntimeError("reranker response is missing indices")
        seen: set[int] = set()
        normalized: list[dict[str, Any]] = []
        for item in results:
            index = int(item.get("index", -1)) if isinstance(item, dict) else -1
            score = float(item.get("relevance_score", item.get("score", 0))) if isinstance(item, dict) else 0
            if index < 0 or index >= len(values) or index in seen or not 0 <= score <= 1:
                raise RuntimeError("reranker response contains an invalid index or score")
            seen.add(index)
            normalized.append({"index": index, "score": score, "text": values[index]})
        if len(normalized) < requested:
            raise RuntimeError("reranker response did not return enough candidates")
        return normalized[:requested]
    except AIConfigurationError:
        raise
    except Exception as exc:
        failure = classify_provider_exception(exc)
        logger.warning(
            "Reranker degraded to deterministic semantic/BM25 order: category=%s candidates=%s requested=%s",
            failure.category,
            len(values),
            requested,
        )
        return [
            {
                "index": index,
                "score": 0.0,
                "text": values[index],
                "degraded": True,
                "degraded_reason": failure.category,
            }
            for index in range(requested)
        ]


__all__ = ["_rerank_excerpt", "rerank_texts"]
