"""Public orchestration for deterministic next-action policy treatment."""

from __future__ import annotations

from typing import Any

from .next_ask_cards import assemble_next_ask
from .next_ask_language import apply_next_ask_language
from .next_ask_policy import apply_governing_clauses


def apply_next_ask(
    findings: list[Any],
    *,
    vendor: Any = None,
    tender: Any = None,
) -> dict[str, Any]:
    payload = assemble_next_ask(findings, vendor=vendor, tender=tender)
    apply_governing_clauses(payload)
    apply_next_ask_language(payload["cards"])
    return payload


def apply_to_internal_result(internal: dict[str, Any]) -> dict[str, Any]:
    """Annotate a stored evaluation. Does not change qualifies / recommendation."""
    report = internal.get("report") if isinstance(internal.get("report"), dict) else {}
    payload = apply_next_ask(
        report.get("findings") or [],
        vendor=report.get("vendor") or {},
        tender=report.get("tender") or {},
    )
    handoffs = internal.get("handoffs")
    if not isinstance(handoffs, dict):
        handoffs = {}
        internal["handoffs"] = handoffs
    handoffs["08a_policy_next_ask"] = payload
    extensions = report.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
        report["extensions"] = extensions
    extensions["policy_next_ask"] = payload
    return payload


__all__ = ["apply_next_ask", "apply_to_internal_result"]
