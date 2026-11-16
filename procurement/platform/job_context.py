"""Thread-local deadline and model-usage state for one processing job."""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any


JOB_CONTEXT = threading.local()


def _max_job_seconds() -> int:
    return int(os.getenv("MAX_JOB_SECONDS", "900"))


def _token_costs() -> tuple[float, float]:
    return (
        float(os.getenv("GEMINI_INPUT_COST_PER_MILLION_TOKENS", "0")),
        float(os.getenv("GEMINI_OUTPUT_COST_PER_MILLION_TOKENS", "0")),
    )


def set_job_deadline() -> None:
    max_seconds = _max_job_seconds()
    JOB_CONTEXT.deadline = time.monotonic() + max_seconds if max_seconds > 0 else None


def clear_job_deadline() -> None:
    JOB_CONTEXT.deadline = None


def reset_llm_usage(job_id: str) -> None:
    if not getattr(JOB_CONTEXT, "provider_operation_id", None):
        JOB_CONTEXT.job_id = job_id
    JOB_CONTEXT.llm_usage = []
    JOB_CONTEXT.base_rfp_atoms = []


def check_job_deadline() -> None:
    max_seconds = _max_job_seconds()
    deadline = getattr(JOB_CONTEXT, "deadline", None)
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError(f"processing exceeded {max_seconds} seconds")


def job_bounded_timeout_seconds(configured: float) -> float:
    check_job_deadline()
    deadline = getattr(JOB_CONTEXT, "deadline", None)
    if deadline is not None:
        configured = min(configured, deadline - time.monotonic())
    return max(1.0, float(configured))


def gemini_call_timeout_seconds() -> int:
    configured = int(
        os.getenv(
            "AI_REQUEST_TIMEOUT_SECONDS",
            os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "45"),
        )
    )
    return int(job_bounded_timeout_seconds(configured))


def gemini_retry_delay_seconds(attempt: int, retry_after: float | None = None) -> float:
    retry_base = float(
        os.getenv(
            "AI_RETRY_BASE_SECONDS",
            os.getenv("GEMINI_RETRY_BASE_SECONDS", "5"),
        )
    )
    ceiling = max(retry_base, float(os.getenv("AI_RETRY_MAX_DELAY_SECONDS", "60")))
    delay = min(ceiling, retry_base * (2 ** max(0, attempt - 1)))
    if retry_after is not None:
        delay = max(delay, min(ceiling, max(0.0, float(retry_after))))
    else:
        delay = min(ceiling, delay + random.uniform(0.0, max(0.0, float(os.getenv("AI_RETRY_JITTER_SECONDS", "0.25")))))
    deadline = getattr(JOB_CONTEXT, "deadline", None)
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            check_job_deadline()
        delay = min(delay, max(0.0, remaining))
    return delay


def record_llm_usage(task: str, model: str, usage: dict[str, Any]) -> None:
    if not usage:
        return
    input_cost, output_cost = _token_costs()
    prompt_tokens = int(usage.get("promptTokenCount") or 0)
    output_tokens = int(usage.get("candidatesTokenCount") or 0)
    total_tokens = int(usage.get("totalTokenCount") or prompt_tokens + output_tokens)
    estimated_cost = None
    if input_cost or output_cost:
        estimated_cost = round(
            (prompt_tokens / 1_000_000 * input_cost)
            + (output_tokens / 1_000_000 * output_cost),
            8,
        )
    entry = {
        "task": task,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost,
    }
    usage_log = getattr(JOB_CONTEXT, "llm_usage", None)
    if isinstance(usage_log, list):
        usage_log.append(entry)


def llm_usage_summary() -> dict[str, Any]:
    input_cost, output_cost = _token_costs()
    calls = list(getattr(JOB_CONTEXT, "llm_usage", []) or [])
    total_prompt = sum(int(call.get("prompt_tokens") or 0) for call in calls)
    total_output = sum(int(call.get("output_tokens") or 0) for call in calls)
    estimated_costs = [call.get("estimated_cost_usd") for call in calls if call.get("estimated_cost_usd") is not None]
    return {
        "call_count": len(calls),
        "prompt_tokens": total_prompt,
        "output_tokens": total_output,
        "total_tokens": sum(int(call.get("total_tokens") or 0) for call in calls),
        "estimated_cost_usd": round(sum(float(value) for value in estimated_costs), 8) if estimated_costs else None,
        "input_cost_per_1m_tokens": input_cost or None,
        "output_cost_per_1m_tokens": output_cost or None,
        "calls": calls,
    }
