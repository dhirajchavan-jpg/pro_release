"""Compatibility facade for tender document-structure helpers.

The implementation lives in the small ``structure_*`` modules.  This module
keeps the historical import surface and the reconstruction patch points used
by callers and tests.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import re
from statistics import median
from typing import Any

from procurement.domain.schemas import (
    DocumentClassification,
    ExtractedDocument,
    LayoutBlock,
    LayoutTableCell,
    LogicalDocument,
    PageUnit,
    StructuralPacket,
    StructuralSection,
)
from procurement.domain.text import clean_text, normalize_for_match, unique_items
from procurement.understanding.layout import assign_page_reading_order, coerce_int

from .structure_blocks import (
    mark_repeated_page_furniture,
    mark_table_block_overlaps,
    repeated_furniture_key,
    table_row_units,
)
from .structure_documents import (
    _LOGICAL_DOCUMENT_TYPE_TITLES,
    _logical_document_type_title,
    make_logical_document,
)
from .structure_headings import (
    GENERIC_SECTION_TITLE,
    STRUCTURAL_HEADING_PREFIX,
    STRUCTURAL_NUMBER,
    block_is_inline_list_reference,
    clause_number_from_text,
    page_is_table_of_contents,
    potential_structural_line,
    section_display,
    section_is_numbering_container,
    sequential_list_block_ids,
    structural_heading_parts,
    structural_number_is_date,
    structural_number_is_time,
    structural_number_level,
    structural_title_is_clause_like,
    text_is_numbering_container,
)
from .structure_integrity import structural_integrity
from .structure_reconstruction import (
    reconstruct_document_structure as _reconstruct_document_structure_impl,
)
from .structure_review import (
    STRUCTURAL_REVIEW_CLASSIFICATIONS,
    STRUCTURAL_REVIEW_MIN_RISK,
    normalize_structural_page_reviews,
    structural_anchor_keys,
    structural_missing_heading_risk,
    structural_page_review_response_schema,
    structural_review_candidates as _structural_review_candidates_impl,
)


def reconstruct_document_structure(
    documents: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
) -> StructuralPacket:
    """Reconstruct structure while retaining the historical patch seams."""

    return _reconstruct_document_structure_impl(
        documents,
        logical_documents,
        _compatibility_hooks={
            "assign_page_reading_order": assign_page_reading_order,
            "mark_repeated_page_furniture": mark_repeated_page_furniture,
            "mark_table_block_overlaps": mark_table_block_overlaps,
            "page_is_table_of_contents": page_is_table_of_contents,
            "sequential_list_block_ids": sequential_list_block_ids,
            "clause_number_from_text": clause_number_from_text,
            "potential_structural_line": potential_structural_line,
            "structural_heading_parts": structural_heading_parts,
            "block_is_inline_list_reference": block_is_inline_list_reference,
            "structural_title_is_clause_like": structural_title_is_clause_like,
            "section_display": section_display,
            "section_is_numbering_container": section_is_numbering_container,
            "text_is_numbering_container": text_is_numbering_container,
            "structural_integrity": structural_integrity,
            "structural_review_candidates": structural_review_candidates,
            "clean_text": clean_text,
            "normalize_for_match": normalize_for_match,
            "coerce_int": coerce_int,
        },
    )


def structural_review_candidates(
    documents: list[ExtractedDocument],
    structure: StructuralPacket,
) -> list[dict[str, Any]]:
    """Find review candidates while retaining the historical helper seams."""

    return _structural_review_candidates_impl(
        documents,
        structure,
        _compatibility_hooks={
            "page_is_table_of_contents": page_is_table_of_contents,
            "structural_anchor_keys": structural_anchor_keys,
            "structural_missing_heading_risk": structural_missing_heading_risk,
            "structural_title_is_clause_like": structural_title_is_clause_like,
        },
    )


__all__ = [
    "Counter",
    "lru_cache",
    "re",
    "median",
    "Any",
    "DocumentClassification",
    "ExtractedDocument",
    "LayoutBlock",
    "LayoutTableCell",
    "LogicalDocument",
    "PageUnit",
    "StructuralPacket",
    "StructuralSection",
    "clean_text",
    "normalize_for_match",
    "unique_items",
    "assign_page_reading_order",
    "coerce_int",
    "STRUCTURAL_HEADING_PREFIX",
    "STRUCTURAL_NUMBER",
    "GENERIC_SECTION_TITLE",
    "structural_number_level",
    "structural_number_is_date",
    "structural_number_is_time",
    "structural_title_is_clause_like",
    "page_is_table_of_contents",
    "block_is_inline_list_reference",
    "sequential_list_block_ids",
    "structural_heading_parts",
    "potential_structural_line",
    "clause_number_from_text",
    "make_logical_document",
    "repeated_furniture_key",
    "mark_repeated_page_furniture",
    "table_row_units",
    "mark_table_block_overlaps",
    "section_display",
    "section_is_numbering_container",
    "text_is_numbering_container",
    "structural_integrity",
    "reconstruct_document_structure",
    "STRUCTURAL_REVIEW_MIN_RISK",
    "STRUCTURAL_REVIEW_CLASSIFICATIONS",
    "structural_anchor_keys",
    "structural_missing_heading_risk",
    "structural_review_candidates",
    "structural_page_review_response_schema",
    "normalize_structural_page_reviews",
]
