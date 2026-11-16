"""Structural-review candidates and response normalization."""

from __future__ import annotations

from functools import lru_cache
import re
from statistics import median
from typing import Any

from procurement.domain.schemas import ExtractedDocument, LayoutBlock, PageUnit, StructuralPacket
from procurement.domain.text import clean_text, normalize_for_match
from procurement.understanding.layout import coerce_int
from .structure_headings import (
    GENERIC_SECTION_TITLE,
    STRUCTURAL_HEADING_PREFIX,
    STRUCTURAL_NUMBER,
    page_is_table_of_contents,
    structural_heading_parts,
    structural_number_is_date,
    structural_number_is_time,
    structural_title_is_clause_like,
)


STRUCTURAL_REVIEW_MIN_RISK = 60
STRUCTURAL_REVIEW_CLASSIFICATIONS = (
    "section_heading",
    "body_continuation",
    "inline_cross_reference",
    "table_content",
    "header_footer",
    "list_item",
)


def structural_anchor_keys(text: str) -> set[str]:
    cleaned = clean_text(text)
    cleaned = re.sub(r"(?:\.{3,}|-{4,})\s*\d+\s*$", "", cleaned).strip()
    cleaned = re.sub(r"\s+\d+\s*$", "", cleaned).strip() if re.search(r"(?:\.{3,}|-{4,})", text) else cleaned
    keys = {normalize_for_match(cleaned)} if cleaned else set()
    title = re.sub(
        r"^(?:(?:part|section|chapter|annexure|appendix|schedule|format)\s+)?(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z])[.):-]?\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if title and title != cleaned:
        keys.add(normalize_for_match(title))
    return {key for key in keys if len(key) >= 4}


def structural_missing_heading_risk(
    block: LayoutBlock,
    previous: LayoutBlock | None,
    following: LayoutBlock | None,
    body_size: float,
    toc_targets: set[str],
) -> tuple[int, list[str]]:
    text = clean_text(block.text)
    if (
        not text
        or len(text) > 220
        or block.block_type in {"list_item", "cross_reference", "table_fragment"}
        or structural_title_is_clause_like(text)
    ):
        return 0, []
    numbered = STRUCTURAL_NUMBER.match(text)
    if numbered and (structural_number_is_date(numbered.group(1)) or structural_number_is_time(*numbered.groups())):
        return 0, []

    score = 0
    signals: list[str] = []

    def add(points: int, signal: str) -> None:
        nonlocal score
        score += points
        if signal not in signals:
            signals.append(signal)

    if structural_anchor_keys(text) & toc_targets:
        add(75, "matches_table_of_contents_anchor")
    if structural_heading_parts(text, font_size=block.font_size, body_size=body_size, is_bold=block.is_bold):
        add(65, "heading_grammar_was_suppressed")
    if STRUCTURAL_HEADING_PREFIX.match(text):
        add(55, "section_prefix_shape")
    elif numbered:
        add(40, "numbered_title_shape")
    elif re.match(r"^[A-Z][.)]\s+\S", text):
        add(45, "lettered_title_shape")
    if GENERIC_SECTION_TITLE.match(text):
        add(45, "generic_section_semantics")
    letters = [character for character in text if character.isalpha()]
    if 1 < len(text.split()) <= 14 and letters and sum(character.isupper() for character in letters) / len(letters) >= 0.75:
        add(35, "short_uppercase_title_shape")
    if block.is_bold:
        add(20, "bold_body_candidate")
    if block.font_size > 0 and body_size > 0 and block.font_size >= body_size * 1.08:
        add(20, "larger_than_body_candidate")
    if block.duplicate_of:
        add(15, "table_overlap_may_hide_heading")
    if len(block.bbox) == 4:
        before_gap = block.bbox[1] - previous.bbox[3] if previous and len(previous.bbox) == 4 else 0
        after_gap = following.bbox[1] - block.bbox[3] if following and len(following.bbox) == 4 else 0
        if max(before_gap, after_gap) >= 0.012:
            add(15, "surrounding_vertical_whitespace")
    return min(100, score), signals


def structural_review_candidates(
    documents: list[ExtractedDocument],
    structure: StructuralPacket,
    *,
    _compatibility_hooks: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    hooks = {
        "page_is_table_of_contents": page_is_table_of_contents,
        "structural_anchor_keys": structural_anchor_keys,
        "structural_missing_heading_risk": structural_missing_heading_risk,
        "structural_title_is_clause_like": structural_title_is_clause_like,
    }
    if _compatibility_hooks:
        hooks.update(_compatibility_hooks)
    _page_is_table_of_contents = hooks["page_is_table_of_contents"]
    _structural_anchor_keys = hooks["structural_anchor_keys"]
    _structural_missing_heading_risk = hooks["structural_missing_heading_risk"]
    _structural_title_is_clause_like = hooks["structural_title_is_clause_like"]

    sections = {section.section_id: section for section in structure.sections}
    section_by_block = {
        block_id: section
        for section in structure.sections
        for block_id in section.source_block_ids
    }
    children: dict[str, list[str]] = {}
    for section in structure.sections:
        if section.parent_section_id:
            children.setdefault(section.parent_section_id, []).append(section.section_id)
    descendant_cache: dict[str, int] = {}

    def descendant_count(section_id: str, trail: frozenset[str] = frozenset()) -> int:
        if section_id in descendant_cache:
            return descendant_cache[section_id]
        if section_id in trail:
            return 0
        descendants = children.get(section_id, [])
        count = len(descendants) + sum(descendant_count(child, trail | {section_id}) for child in descendants)
        descendant_cache[section_id] = count
        return count

    candidates: list[dict[str, Any]] = []
    for document in documents:
        page_count = max(1, len(document.pages))
        body_sizes = [
            block.font_size
            for page in document.pages
            for block in page.blocks
            if block.font_size > 0 and block.block_type in {"paragraph", "list_item"}
        ]
        body_size = float(median(body_sizes)) if body_sizes else 0.0
        toc_targets = {
            key
            for page in document.pages
            if _page_is_table_of_contents(page)
            for block in page.blocks
            for key in _structural_anchor_keys(block.text)
        }
        for page in document.pages:
            if page.is_blank or _page_is_table_of_contents(page):
                continue
            ordered = [
                block
                for block in sorted(page.blocks, key=lambda item: item.reading_order)
                if block.block_type not in {"header", "footer", "toc_entry"} and block.text.strip()
            ]
            mixed_groups: dict[str, list[tuple[LayoutBlock, bool]]] = {}
            for block in ordered:
                number = block.section_number or block.clause_number
                if re.fullmatch(r"\d+(?:\.\d+)+", number):
                    mixed_groups.setdefault(number.rsplit(".", 1)[0], []).append((block, bool(block.section_number)))
            mixed_ids = {
                block.block_id
                for rows in mixed_groups.values()
                if any(is_heading for _block, is_heading in rows) and any(not is_heading for _block, is_heading in rows)
                for block, _is_heading in rows
            }

            page_blocks: list[dict[str, Any]] = []
            for index, block in enumerate(ordered):
                if block.structural_review.get("status") == "applied":
                    continue
                current_heading = bool(block.section_number or block.heading_level)
                previous = ordered[index - 1] if index else None
                following = ordered[index + 1] if index + 1 < len(ordered) else None
                previous_text = clean_text(previous.text) if previous else ""
                following_text = clean_text(following.text) if following else ""
                text = clean_text(block.text)
                score, signals = (0, []) if current_heading else _structural_missing_heading_risk(
                    block,
                    previous,
                    following,
                    body_size,
                    toc_targets,
                )

                def add(points: int, signal: str) -> None:
                    nonlocal score
                    score += points
                    if signal not in signals:
                        signals.append(signal)

                numeric_heading = bool(re.fullmatch(r"\d+(?:\.\d+)*", block.section_number))
                if current_heading and _structural_title_is_clause_like(text):
                    add(100, "heading_contains_clause_language")
                if block.block_id in mixed_ids:
                    definition_context = bool(
                        re.search(r"\b(?:definitions?|terminology|abbreviations?)\b", " ".join(block.section_path), re.IGNORECASE)
                    )
                    add(60 if definition_context else 20, "mixed_heading_and_clause_roles_for_same_numbering_family")
                if current_heading and (block.duplicate_of or re.search(r"\bpage\s+\d+\s+of\s+\d+\b|classification\s*:", text, re.IGNORECASE)):
                    add(100, "heading_contains_table_or_page_furniture")
                if current_heading and len(block.bbox) == 4 and block.bbox[1] >= 0.93:
                    add(75, "heading_in_footer_region")
                previous_connector = bool(
                    re.search(
                        r"\b(?:the|a|an|of|to|for|with|from|in|as\s+per|refer(?:red)?\s+to|mentioned\s+in)\s*$",
                        previous_text,
                        re.IGNORECASE,
                    )
                )
                following_continuation = bool(re.match(r"^[a-z]", following_text))
                if current_heading and previous_connector and following_continuation:
                    add(25 if numeric_heading else 85, "heading_splits_a_sentence_continuation")
                elif current_heading and (previous_connector or following_continuation) and not block.is_bold:
                    add(10 if numeric_heading else 35, "heading_has_sentence_continuation_context")
                inline_reference = bool(
                    re.fullmatch(
                        r"(?:annexure|appendix|schedule|section|part|format)\s+[A-Z0-9IVXLCDM.-]+",
                        text,
                        re.IGNORECASE,
                    )
                )
                if current_heading and inline_reference and not block.is_bold and (len(block.bbox) != 4 or block.bbox[1] > 0.08):
                    add(65, "standalone_reference_uses_body_typography")
                    if re.search(r"\b(?:as\s+per|refer(?:red)?\s+to|mentioned\s+in|attached\s+as)\b", previous_text[-180:], re.IGNORECASE):
                        add(50, "reference_continues_preceding_clause")
                if current_heading and not block.is_bold and len(text.split()) > 10:
                    add(10, "long_nonbold_heading")

                section = section_by_block.get(block.block_id)
                descendants = descendant_count(section.section_id) if section else 0
                direct_children = len(children.get(section.section_id, [])) if section else 0
                page_span = section.page_end - section.page_start + 1 if section else 0
                if descendants >= 25:
                    add(35, "heading_has_large_descendant_subtree")
                if direct_children >= 15:
                    add(20, "heading_has_many_direct_children")
                if page_span >= max(12, page_count // 3):
                    add(25, "heading_remains_open_for_large_document_span")

                if score < STRUCTURAL_REVIEW_MIN_RISK:
                    continue
                page_blocks.append(
                    {
                        "block_id": block.block_id,
                        "text": text,
                        "bbox": block.bbox,
                        "font_size": block.font_size,
                        "is_bold": block.is_bold,
                        "current_is_heading": current_heading,
                        "current_number": block.section_number,
                        "current_level": block.heading_level or 0,
                        "current_section_id": section.section_id if section else "",
                        "risk_score": min(100, score),
                        "signals": signals,
                        "descendant_count": descendants,
                        "page_span": page_span,
                        "previous_text": previous_text[-500:],
                        "following_text": following_text[:500],
                    }
                )
            if page_blocks:
                candidates.append(
                    {
                        "document_id": document.record.document_id,
                        "file_name": document.record.file_name,
                        "page_id": page.page_id,
                        "page_number": page.page_number,
                        "risk_score": min(100, max(item["risk_score"] for item in page_blocks) + min(10, len(page_blocks) - 1)),
                        "blocks": page_blocks,
                    }
                )
    return sorted(candidates, key=lambda item: (-item["risk_score"], item["document_id"], item["page_number"]))


@lru_cache(maxsize=1)
def structural_page_review_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string"},
                        "is_section_heading": {"type": "boolean"},
                        "classification": {"type": "string", "enum": list(STRUCTURAL_REVIEW_CLASSIFICATIONS)},
                        "canonical_number": {"type": "string"},
                        "canonical_title": {"type": "string"},
                        "heading_level": {"type": "integer"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "block_id",
                        "is_section_heading",
                        "classification",
                        "canonical_number",
                        "canonical_title",
                        "heading_level",
                        "confidence",
                        "reason",
                    ],
                },
            }
        },
        "required": ["reviews"],
    }


def normalize_structural_page_reviews(data: Any, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    allowed_ids = {item["block_id"] for item in candidate.get("blocks") or []}
    reviews: list[dict[str, Any]] = []
    for item in data.get("reviews") or []:
        if not isinstance(item, dict) or item.get("block_id") not in allowed_ids or not isinstance(item.get("is_section_heading"), bool):
            continue
        is_heading = item["is_section_heading"]
        classification = str(item.get("classification") or ("section_heading" if is_heading else "body_continuation"))
        if classification not in STRUCTURAL_REVIEW_CLASSIFICATIONS:
            classification = "section_heading" if is_heading else "body_continuation"
        if is_heading:
            classification = "section_heading"
        reviews.append(
            {
                "block_id": item["block_id"],
                "is_section_heading": is_heading,
                "classification": classification,
                "canonical_number": clean_text(str(item.get("canonical_number") or "")),
                "canonical_title": clean_text(str(item.get("canonical_title") or "")),
                "heading_level": max(0, min(8, coerce_int(item.get("heading_level"), 0))),
                "confidence": round(max(0.0, min(1.0, float(item.get("confidence") or 0.0))), 4),
                "reason": clean_text(str(item.get("reason") or ""))[:500],
            }
        )
    return reviews


__all__ = [
    "STRUCTURAL_REVIEW_MIN_RISK",
    "STRUCTURAL_REVIEW_CLASSIFICATIONS",
    "structural_anchor_keys",
    "structural_missing_heading_risk",
    "structural_review_candidates",
    "structural_page_review_response_schema",
    "normalize_structural_page_reviews",
]
