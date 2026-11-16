"""Text normalization shared by ingestion, retrieval, and evaluation."""

import re
from functools import lru_cache


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@lru_cache(maxsize=8192)
def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


@lru_cache(maxsize=8192)
def keyword_tokens(text: str) -> frozenset[str]:
    stopwords = {
        "the", "and", "for", "with", "from", "this", "that", "must", "shall", "should",
        "document", "documents", "proof", "copy", "valid", "required", "bidder", "vendor",
    }
    return frozenset({
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in stopwords
    })


def unique_items(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def humanize_check_name(check: str) -> str:
    return check.replace("_", " ").strip()


def quote_matches_text(quote: str, text: str) -> bool:
    normalized_quote = normalize_for_match(quote)
    normalized_text = normalize_for_match(text)
    if normalized_quote in normalized_text:
        return True
    quote_tokens = keyword_tokens(normalized_quote)
    text_tokens = keyword_tokens(normalized_text)
    if not quote_tokens:
        return False
    return len(quote_tokens & text_tokens) / len(quote_tokens) >= 0.6
