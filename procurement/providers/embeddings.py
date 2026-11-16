"""OpenAI-compatible embedding calls with bounded concurrency and cache reuse."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from procurement.platform.files import write_json_atomic
from procurement.platform.provider_usage import (
    current_operation_id,
    fingerprint,
    record_provider_call,
)

from .cache import _EMBEDDING_CACHE_LOCK, _EMBEDDING_MEMORY_CACHE
from .errors import AIProviderOutputError, classify_provider_exception
from .retry import _provider_attempts, call_with_provider_retry, provider_slot
from .settings import _provider_int_setting, _provider_timeout, api_key_for, settings, validate
from .transport import openai_client


def embed_texts(
    values: list[str],
    model: str | None = None,
    timeout: int = 45,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> list[list[float]]:
    settings_value = settings()
    validate(settings_value, need_embedding=True)
    timeout = _provider_timeout(
        "embedding",
        max(1, int(os.getenv("AI_EMBEDDING_TIMEOUT_SECONDS", str(timeout)))),
    )
    selected_model = model or settings_value.model_embedding
    cache_enabled = os.getenv("EMBEDDING_CACHE_ENABLED", "1") == "1"
    cache_dir = Path(os.getenv("EMBEDDING_CACHE_DIR", "runtime/cache/embeddings"))
    memory_cache_items = max(0, int(os.getenv("AI_EMBEDDING_MEMORY_CACHE_ITEMS", "4096")))
    expected_dimension = max(0, int(os.getenv("AI_EMBEDDING_DIMENSIONS", "0"))) or None
    cache_identity = "\0".join(
        (
            "embedding-cache-v2",
            settings_value.provider,
            settings_value.embedding_base_url.rstrip("/"),
            selected_model,
            str(expected_dimension or "provider-default"),
            os.getenv("AI_CACHE_REVISION", os.getenv("AI_DEPLOYMENT_REVISION", "1")),
        )
    )
    keys = [
        hashlib.sha256(f"{cache_identity}\0{value}".encode("utf-8")).hexdigest()
        for value in values
    ]
    vectors: list[list[float] | None] = [None] * len(values)

    def validate_vector(vector: Any, *, source: str) -> list[float]:
        if not isinstance(vector, (list, tuple)) or not vector:
            raise AIProviderOutputError(f"embedding provider returned an empty vector ({source})", category="invalid_embedding")
        try:
            normalized = [float(item) for item in vector]
        except (TypeError, ValueError) as exc:
            raise AIProviderOutputError(
                f"embedding provider returned a non-numeric vector ({source})",
                category="invalid_embedding",
            ) from exc
        if any(not math.isfinite(item) for item in normalized):
            raise AIProviderOutputError(
                f"embedding provider returned a non-finite vector ({source})",
                category="invalid_embedding",
            )
        if expected_dimension is not None and len(normalized) != expected_dimension:
            raise AIProviderOutputError(
                f"embedding vector dimension {len(normalized)} does not match configured dimension {expected_dimension}",
                category="embedding_dimension_mismatch",
            )
        return normalized

    if cache_enabled:
        with _EMBEDDING_CACHE_LOCK:
            for index, key in enumerate(keys):
                if key in _EMBEDDING_MEMORY_CACHE:
                    vectors[index] = validate_vector(
                        _EMBEDDING_MEMORY_CACHE[key], source="memory_cache"
                    )
                    _EMBEDDING_MEMORY_CACHE.move_to_end(key)
                    continue
                path = cache_dir / key[:2] / f"{key}.json"
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    vectors[index] = validate_vector(value, source="cache")
                    if memory_cache_items:
                        _EMBEDDING_MEMORY_CACHE[key] = vectors[index]
                        _EMBEDDING_MEMORY_CACHE.move_to_end(key)
                        while len(_EMBEDDING_MEMORY_CACHE) > memory_cache_items:
                            _EMBEDDING_MEMORY_CACHE.popitem(last=False)
                except (OSError, ValueError, TypeError, json.JSONDecodeError, AIProviderOutputError):
                    pass
    missing = [index for index, vector in enumerate(vectors) if vector is None]
    usage_operation_id = current_operation_id()
    cache_hits = len(values) - len(missing)
    if cache_hits:
        record_provider_call(
            capability="embedding",
            provider=settings_value.provider,
            model=selected_model,
            task="embedding_cache",
            status="cache_hit",
            duration_ms=0,
            operation_id=usage_operation_id,
            endpoint=settings_value.embedding_base_url,
            cache_status="hit",
            input_units=cache_hits,
            output_units=cache_hits,
            unit_name="text",
            request_fingerprint=fingerprint([keys[index] for index, vector in enumerate(vectors) if vector is not None]),
        )
    if missing:
        factory = client_factory or openai_client
        client = factory(
            replace(settings_value, api_key=api_key_for(settings_value, "embedding")),
            timeout,
            settings_value.embedding_base_url,
            task="embedding",
        )
        batch_size = max(1, int(os.getenv("AI_EMBEDDING_BATCH_SIZE", "64")))
        missing_by_key: dict[str, list[int]] = {}
        for index in missing:
            missing_by_key.setdefault(keys[index], []).append(index)
        representatives = [indices[0] for indices in missing_by_key.values()]
        batches = [
            representatives[offset : offset + batch_size]
            for offset in range(0, len(representatives), batch_size)
        ]

        def fetch_batch(batch: list[int]) -> tuple[list[int], list[list[float]]]:
            started = time.monotonic()
            call_attempts = 0
            request_hash = fingerprint(
                {"model": selected_model, "values": [values[index] for index in batch]}
            )

            def create_embeddings() -> Any:
                nonlocal call_attempts
                call_attempts += 1
                return client.embeddings.create(
                        model=selected_model,
                        input=[values[index] for index in batch],
                    )

            try:
                with provider_slot(settings_value, "embedding") as gate:
                    response = call_with_provider_retry(
                        create_embeddings,
                        task="embedding",
                        attempts=_provider_attempts("embedding"),
                    )
                    gate.mark_success()
                received = [validate_vector(item.embedding, source="provider") for item in response.data]
                if len(received) != len(batch):
                    raise AIProviderOutputError("embedding provider returned an invalid vector batch", category="invalid_embedding")
            except Exception as exc:
                failure = classify_provider_exception(exc)
                record_provider_call(
                    capability="embedding",
                    provider=settings_value.provider,
                    model=selected_model,
                    task="embedding",
                    status="failed",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    operation_id=usage_operation_id,
                    endpoint=settings_value.embedding_base_url,
                    attempts=call_attempts,
                    input_units=len(batch),
                    unit_name="text",
                    request_fingerprint=request_hash,
                    error_category=failure.category,
                    http_status=failure.status_code,
                )
                raise
            usage = getattr(response, "usage", None)
            input_tokens = int(
                getattr(usage, "prompt_tokens", 0)
                or getattr(usage, "total_tokens", 0)
                or 0
            )
            estimated_tokens = not bool(input_tokens)
            if estimated_tokens:
                chars_per_token = max(1.0, float(os.getenv("AI_CONTEXT_CHARS_PER_TOKEN", "3")))
                input_tokens = round(sum(len(values[index]) for index in batch) / chars_per_token)
            record_provider_call(
                capability="embedding",
                provider=settings_value.provider,
                model=selected_model,
                task="embedding",
                status="succeeded",
                duration_ms=round((time.monotonic() - started) * 1000),
                operation_id=usage_operation_id,
                endpoint=settings_value.embedding_base_url,
                attempts=call_attempts,
                input_tokens=input_tokens,
                total_tokens=input_tokens,
                input_units=len(batch),
                output_units=len(received),
                unit_name="text",
                request_fingerprint=request_hash,
                response_fingerprint=fingerprint(received),
                metrics={
                    "token_count_estimated": estimated_tokens,
                    "vector_dimensions": len(received[0]) if received else 0,
                },
            )
            return batch, received

        concurrency = min(
            len(batches),
            _provider_int_setting("embedding", "CONCURRENCY", 2, maximum=64),
        )
        fetched: list[tuple[list[int], list[list[float]]]] = []
        if concurrency <= 1:
            fetched = [fetch_batch(batch) for batch in batches]
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(fetch_batch, batch) for batch in batches]
                fetched = [future.result() for future in as_completed(futures)]
        for batch, received in fetched:
            for representative, vector in zip(batch, received):
                for index in missing_by_key[keys[representative]]:
                    vectors[index] = vector
        dimensions = {len(vector) for vector in vectors if vector is not None}
        if len(dimensions) > 1:
            raise AIProviderOutputError(
                "embedding provider returned inconsistent vector dimensions",
                category="embedding_dimension_mismatch",
            )
        if cache_enabled:
            with _EMBEDDING_CACHE_LOCK:
                for index in representatives:
                    path = cache_dir / keys[index][:2] / f"{keys[index]}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_json_atomic(path, vectors[index], indent=None)
                    if memory_cache_items:
                        _EMBEDDING_MEMORY_CACHE[keys[index]] = vectors[index]
                        _EMBEDDING_MEMORY_CACHE.move_to_end(keys[index])
                        while len(_EMBEDDING_MEMORY_CACHE) > memory_cache_items:
                            _EMBEDDING_MEMORY_CACHE.popitem(last=False)
    if any(vector is None for vector in vectors):
        raise AIProviderOutputError("embedding provider did not return all requested vectors", category="invalid_embedding")
    return [vector for vector in vectors if vector is not None]


__all__ = ["embed_texts"]
