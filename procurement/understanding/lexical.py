"""Shared lexical normalization for tender retrieval and grounding."""

from __future__ import annotations

from functools import lru_cache
import re
from typing import Any


_WORD_RE = re.compile(r"[a-z0-9]+", flags=re.IGNORECASE)
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "bid",
    "bidder",
    "by",
    "can",
    "criteria",
    "details",
    "do",
    "does",
    "for",
    "from",
    "has",
    "have",
    "if",
    "in",
    "indicating",
    "id",
    "is",
    "it",
    "must",
    "no",
    "number",
    "of",
    "on",
    "or",
    "required",
    "requirement",
    "rfp",
    "shall",
    "should",
    "tender",
    "that",
    "the",
    "their",
    "this",
    "to",
    "whether",
    "with",
}


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _lexical_text(value: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(value or "")).replace("_", " ")


@lru_cache(maxsize=16384)
def _token_tuple(value: str) -> tuple[str, ...]:
    original = value.replace("_", " ")
    return tuple(
        stemmed
        for word in dict.fromkeys(
            [
                *_WORD_RE.findall(original.casefold()),
                *_WORD_RE.findall(_lexical_text(original).casefold()),
            ]
        )
        if word not in _STOP_WORDS
        if (stemmed := _stem(word)) not in _STOP_WORDS and len(stemmed) > 1
    )


def _tokens(value: str) -> list[str]:
    return list(_token_tuple(str(value or "")))


@lru_cache(maxsize=16384)
def _cached_phrase_text(value: str) -> str:
    return " ".join(_WORD_RE.findall(value.casefold().replace("_", " ")))


def _phrase_text(value: str) -> str:
    return _cached_phrase_text(str(value or ""))


def _contains_phrase(searchable: str, phrase: Any) -> bool:
    normalized = _phrase_text(str(phrase or ""))
    return bool(normalized) and f" {normalized} " in f" {searchable} "


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
