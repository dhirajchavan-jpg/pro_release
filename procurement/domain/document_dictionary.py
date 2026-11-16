"""Procurement document vocabulary and deterministic text matching."""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from procurement.domain.text import normalize_for_match


logger = logging.getLogger("procurement_evaluator")


@lru_cache(maxsize=1)
def load_procurement_document_dictionary() -> dict[str, Any]:
    path = Path(
        os.getenv(
            "PROCUREMENT_DOCUMENT_DICTIONARY_FILE",
            "config/procurement_document_dictionary.json",
        )
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Procurement document dictionary unavailable: %s", exc)
        return {"version": "unavailable", "entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        logger.warning("Procurement document dictionary must be a JSON object with entries[]")
        return {"version": "invalid", "entries": []}
    return data


def procurement_document_dictionary_context() -> list[dict[str, Any]]:
    context = []
    for entry in load_procurement_document_dictionary().get("entries", []):
        if not isinstance(entry, dict):
            continue
        context.append(
            {
                "canonical_key": entry.get("canonical_key", ""),
                "display_name": entry.get("display_name", ""),
                "evidence_type": entry.get("evidence_type", "other"),
                "category": entry.get("category", "other"),
                "stage": entry.get("stage", "bid_stage"),
                "aliases": entry.get("aliases", [])[:12],
                "required_when": entry.get("required_when", ""),
                "checks": entry.get("checks", [])[:12],
                "failure_effect": entry.get("failure_effect", ""),
            }
        )
    return context


@lru_cache(maxsize=4096)
def procurement_dictionary_entries_for_text(text: str) -> list[dict[str, Any]]:
    haystack = normalize_for_match(text)
    if not haystack:
        return []
    # Match complete lexical terms, not substrings (for example, the alias
    # "agreement" must not match "agreements").  Tokenizing punctuation also
    # keeps ordinary prose such as "Integrity Pact," and hyphenated aliases
    # discoverable without adding document-specific variants.
    lexical_haystack = " ".join(re.findall(r"\w+", haystack, flags=re.UNICODE))
    haystack_tokens = set(lexical_haystack.split())
    padded_haystack = f" {lexical_haystack} "
    matches = []
    seen: set[str] = set()
    for entry in load_procurement_document_dictionary().get("entries", []):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("canonical_key", ""))
        terms = [
            key.replace("_", " "),
            str(entry.get("display_name", "")),
            *[str(alias) for alias in entry.get("aliases", []) if str(alias).strip()],
        ]
        term_matches = []
        for term in terms:
            normalized_term = normalize_for_match(term)
            if not normalized_term:
                continue
            lexical_term = " ".join(re.findall(r"\w+", normalized_term, flags=re.UNICODE))
            if not lexical_term:
                continue
            if " " not in lexical_term:
                term_matches.append(lexical_term in haystack_tokens)
            else:
                term_matches.append(f" {lexical_term} " in padded_haystack)
        if key and any(term_matches):
            if key not in seen:
                matches.append(entry)
                seen.add(key)
    return matches


def procurement_dictionary_display_names(text: str) -> list[str]:
    return [
        str(entry.get("display_name") or entry.get("canonical_key", "")).strip()
        for entry in procurement_dictionary_entries_for_text(text)
        if str(entry.get("display_name") or entry.get("canonical_key", "")).strip()
    ]
