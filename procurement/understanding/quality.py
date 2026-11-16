"""Language-neutral text quality checks and deterministic OCR routing."""

from __future__ import annotations

from collections import Counter
import os
import re
from typing import Any

from procurement.domain.schemas import PageUnit
from procurement.domain.text import clean_text


NATIVE_TEXT_INTEGRITY_GATE_ENABLED = os.getenv("NATIVE_TEXT_INTEGRITY_GATE_ENABLED", "1") != "0"
NATIVE_TEXT_ARTIFACT_RATIO = max(0.0, min(1.0, float(os.getenv("NATIVE_TEXT_ARTIFACT_RATIO", "0"))))
NATIVE_TEXT_LONG_TOKEN_CHARS = max(40, int(os.getenv("NATIVE_TEXT_LONG_TOKEN_CHARS", "80")))
NATIVE_TEXT_LONG_TOKEN_RATIO = max(0.0, min(1.0, float(os.getenv("NATIVE_TEXT_LONG_TOKEN_RATIO", "0.25"))))
REPEATED_BOILERPLATE_MIN_PAGES = max(3, int(os.getenv("REPEATED_BOILERPLATE_MIN_PAGES", "3")))
REPEATED_BOILERPLATE_MIN_LINE_CHARS = max(20, int(os.getenv("REPEATED_BOILERPLATE_MIN_LINE_CHARS", "30")))
MAX_DOC_CHARS = int(os.getenv("MAX_DOC_CHARS", "50000"))
OCR_MIN_PAGE_CHARS = int(os.getenv("OCR_MIN_PAGE_CHARS", "40"))


def page_text_quality(text: str) -> dict[str, Any]:
    characters = len(text)
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    alphanumeric = sum(character.isalnum() for character in text)
    controls = sum(not (character.isprintable() or character in "\n\r\t") for character in text)
    replacement = text.count("\ufffd")
    alphanumeric_ratio = alphanumeric / max(1, characters)
    noise_ratio = (controls + replacement) / max(1, characters)
    score = min(
        1.0,
        0.35 * min(1.0, characters / 200)
        + 0.30 * min(1.0, len(words) / 30)
        + 0.25 * min(1.0, alphanumeric_ratio / 0.55)
        + 0.10 * (1.0 - min(1.0, noise_ratio * 10)),
    )
    return {
        "score": round(max(0.0, score), 4),
        "characters": characters,
        "words": len(words),
        "alphanumeric_characters": alphanumeric,
        "alphanumeric_ratio": round(alphanumeric_ratio, 4),
        "noise_ratio": round(noise_ratio, 4),
    }


def native_text_integrity_metrics(text: str, raw_text: str = "") -> dict[str, Any]:
    """Measure language-neutral native extraction integrity before OCR routing."""
    alphanumeric = sum(character.isalnum() for character in text)
    ascii_alphanumeric = sum(character.isascii() and character.isalnum() for character in text)
    non_ascii_alphanumeric = max(0, alphanumeric - ascii_alphanumeric)
    raw_artifacts = sum(
        not (character.isprintable() or character in "\n\r\t")
        for character in raw_text
    )
    raw_artifacts += raw_text.count("\ufffd")
    raw_artifacts += len(re.findall(r"\(cid[:;]?\d*\)", raw_text, flags=re.IGNORECASE))
    raw_length = len(raw_text)
    artifact_ratio = raw_artifacts / max(1, raw_length)
    tokens = re.findall(r"\S+", text)
    long_unbroken_characters = sum(
        len(token)
        for token in tokens
        if len(token) >= NATIVE_TEXT_LONG_TOKEN_CHARS
    )
    long_unbroken_token_ratio = long_unbroken_characters / max(1, len(text))
    integrity_gate = bool(
        NATIVE_TEXT_INTEGRITY_GATE_ENABLED
        and (
            (raw_artifacts > 0 and artifact_ratio >= NATIVE_TEXT_ARTIFACT_RATIO)
            or long_unbroken_token_ratio >= NATIVE_TEXT_LONG_TOKEN_RATIO
        )
    )
    return {
        "ascii_alphanumeric_characters": ascii_alphanumeric,
        "non_ascii_alphanumeric_characters": non_ascii_alphanumeric,
        "raw_artifact_characters": raw_artifacts,
        "raw_artifact_ratio": round(artifact_ratio, 4),
        "long_unbroken_token_ratio": round(long_unbroken_token_ratio, 4),
        "native_text_integrity_gate": integrity_gate,
    }


def page_requires_ocr(text: str, raw_text: str = "") -> bool:
    quality = page_text_quality(text)
    return bool(
        quality["characters"] < OCR_MIN_PAGE_CHARS
        or quality["words"] < 8
        or quality["alphanumeric_characters"] < 25
        or quality["noise_ratio"] > 0.15
        or native_text_integrity_metrics(text, raw_text)["native_text_integrity_gate"]
    )


def mark_repeated_boilerplate_pages_for_ocr(pages: list[PageUnit]) -> list[int]:
    """Route pages containing only repeated headers or footers through OCR."""
    if len(pages) < REPEATED_BOILERPLATE_MIN_PAGES:
        return []

    normalized_lines_by_page: list[list[tuple[str, str]]] = []
    line_occurrences: Counter[str] = Counter()
    for page in pages:
        normalized_lines = []
        for line in page.text.splitlines():
            original = clean_text(line)
            normalized = original.casefold()
            if len(normalized) < REPEATED_BOILERPLATE_MIN_LINE_CHARS:
                continue
            normalized_lines.append((original, normalized))
        normalized_lines_by_page.append(normalized_lines)
        line_occurrences.update({normalized for _original, normalized in normalized_lines})

    repeated_lines = {
        line
        for line, count in line_occurrences.items()
        if count >= REPEATED_BOILERPLATE_MIN_PAGES
    }
    if not repeated_lines:
        return []

    routed_pages: list[int] = []
    for page, normalized_lines in zip(pages, normalized_lines_by_page):
        if not any(normalized in repeated_lines for _original, normalized in normalized_lines):
            continue
        substantive_text = clean_text(
            "\n".join(
                original
                for original, normalized in normalized_lines
                if normalized not in repeated_lines
            )
        )
        if not page_requires_ocr(substantive_text):
            continue
        remaining_quality = page_text_quality(substantive_text)
        page.ocr_required = True
        page.extraction_quality = min(page.extraction_quality, float(remaining_quality["score"]))
        page.quality_metrics["repeated_boilerplate_only"] = True
        page.quality_metrics["substantive_characters_after_boilerplate"] = len(substantive_text)
        warning = "repeated boilerplate obscures page content; OCR required"
        if warning not in page.warnings:
            page.warnings.append(warning)
        routed_pages.append(page.page_number)
    return routed_pages


def validate_ocr_text(value: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, str):
        raise ValueError("OCR returned non-text page content")
    text = clean_text(value)
    if "\ufffd" in text:
        raise ValueError("OCR returned text containing undecodable characters")
    if len(text) > MAX_DOC_CHARS:
        raise ValueError("OCR page text exceeds the configured maximum")
    if text.lower() in {"null", "none", "undefined", "n/a"}:
        raise ValueError("OCR returned placeholder page content")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 8:
        repeated = max(Counter(lines).values())
        if repeated / len(lines) >= 0.6 and len(max(lines, key=len)) >= 20:
            raise ValueError("OCR returned suspiciously repetitive page content")
    metrics = page_text_quality(text)
    if native_text_integrity_metrics(text)["native_text_integrity_gate"]:
        raise ValueError("OCR returned corrupted or cipher-like page content")
    if text and (
        metrics["alphanumeric_characters"] == 0
        or metrics["noise_ratio"] > 0.2
    ):
        raise ValueError("OCR returned malformed page content")
    return text, metrics
