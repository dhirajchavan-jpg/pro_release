"""Structured evidence verification through the configured provider."""

from __future__ import annotations

import json
from typing import Any, Callable

from .json_protocol import json_chat


def verify_evidence(
    claim: str,
    evidence: list[str],
    *,
    timeout: int = 60,
    json_chat_call: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    verifier = json_chat_call or json_chat
    data = verifier(
        "verify_evidence",
        json.dumps(
            {
                "task": "Verify whether the evidence supports the claim.",
                "claim": claim,
                "evidence": evidence,
                "required_output": {
                    "verdict": "supported|contradicted|insufficient",
                    "reason": "short source-grounded reason",
                },
            },
            ensure_ascii=True,
        ),
        timeout=timeout,
    )
    if data.get("verdict") not in {"supported", "contradicted", "insufficient"}:
        raise RuntimeError("verifier response contains an invalid verdict")
    return data


__all__ = ["verify_evidence"]
