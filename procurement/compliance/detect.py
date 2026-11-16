"""Decide which BHEL policy corpus governs a tender."""

from __future__ import annotations

from typing import Any

from procurement.compliance.profile import policy_id_for_profile, policy_scope


def detect_policy(understanding: dict[str, Any]) -> dict[str, Any]:
    profile = understanding.get("tender_profile")
    if not isinstance(profile, dict):
        profile = {}
    policy_id = policy_id_for_profile(profile)
    if policy_id:
        return {
            "policy_id": policy_id,
            "confidence": 1.0,
            "reason": "governing policy selected from reviewed tender profile",
            "source": "tender_profile",
        }
    scope = policy_scope(profile.get("procurement_type"))
    return {
        "policy_id": "",
        "confidence": 0.0,
        "reason": (
            "procurement type spans both policy scopes"
            if scope == "mixed"
            else "procurement type is unresolved"
        ),
        "source": "tender_profile",
        "status": "policy_selection_required",
    }
