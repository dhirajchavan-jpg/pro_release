"""Document hierarchy reconstruction from classified layout units."""

from __future__ import annotations

from statistics import median
import re
from typing import Any

from procurement.domain.schemas import (
    ExtractedDocument,
    LogicalDocument,
    StructuralPacket,
    StructuralSection,
)
from procurement.domain.text import clean_text, normalize_for_match
from procurement.understanding.layout import assign_page_reading_order, coerce_int
from .structure_blocks import mark_repeated_page_furniture, mark_table_block_overlaps
from .structure_headings import (
    block_is_inline_list_reference,
    clause_number_from_text,
    page_is_table_of_contents,
    potential_structural_line,
    section_display,
    section_is_numbering_container,
    sequential_list_block_ids,
    structural_heading_parts,
    structural_title_is_clause_like,
    text_is_numbering_container,
)
from .structure_integrity import structural_integrity
from .structure_review import structural_review_candidates


def reconstruct_document_structure(
    documents: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
    *,
    _compatibility_hooks: dict[str, Any] | None = None,
) -> StructuralPacket:
    hooks = {
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
    }
    if _compatibility_hooks:
        hooks.update(_compatibility_hooks)

    _assign_page_reading_order = hooks["assign_page_reading_order"]
    _mark_repeated_page_furniture = hooks["mark_repeated_page_furniture"]
    _mark_table_block_overlaps = hooks["mark_table_block_overlaps"]
    _page_is_table_of_contents = hooks["page_is_table_of_contents"]
    _sequential_list_block_ids = hooks["sequential_list_block_ids"]
    _clause_number_from_text = hooks["clause_number_from_text"]
    _potential_structural_line = hooks["potential_structural_line"]
    _structural_heading_parts = hooks["structural_heading_parts"]
    _block_is_inline_list_reference = hooks["block_is_inline_list_reference"]
    _structural_title_is_clause_like = hooks["structural_title_is_clause_like"]
    _section_display = hooks["section_display"]
    _section_is_numbering_container = hooks["section_is_numbering_container"]
    _text_is_numbering_container = hooks["text_is_numbering_container"]
    _structural_integrity = hooks["structural_integrity"]
    _structural_review_candidates = hooks["structural_review_candidates"]
    _clean_text = hooks["clean_text"]
    _normalize_for_match = hooks["normalize_for_match"]
    _coerce_int = hooks["coerce_int"]

    page_lookup = {page.page_id: page for document in documents for page in document.pages}
    all_pages = [page for document in documents for page in document.pages]
    for page in all_pages:
        _assign_page_reading_order(page.blocks, page.tables)
    for document in documents:
        _mark_repeated_page_furniture(document.pages)
    _mark_table_block_overlaps(all_pages)

    sections: list[StructuralSection] = []
    unresolved: list[dict[str, Any]] = []
    for logical in logical_documents:
        pages = sorted(
            (page_lookup[page_id] for page_id in logical.page_refs if page_id in page_lookup),
            key=lambda page: (page.document_id, page.page_number),
        )
        if not pages:
            continue
        body_sizes = [
            block.font_size
            for page in pages
            for block in page.blocks
            if block.font_size > 0 and block.block_type in {"paragraph", "list_item"}
        ]
        body_size = float(median(body_sizes)) if body_sizes else 0.0
        root = StructuralSection(
            section_id=f"{logical.logical_document_id}-ROOT",
            logical_document_id=logical.logical_document_id,
            document_id=pages[0].document_id,
            title=logical.title,
            level=0,
            path=[logical.title],
            page_start=pages[0].page_number,
            page_end=pages[-1].page_number,
            confidence=1.0,
        )
        stack: list[StructuralSection] = [root]
        number_lookup: dict[str, StructuralSection] = {}
        logical_sections: list[StructuralSection] = [root]
        sections.append(root)
        unresolved_blocks = 0

        for page in pages:
            toc_page = _page_is_table_of_contents(page)
            list_block_ids = _sequential_list_block_ids(page)
            if toc_page:
                stack = [root]
            page_default_section = stack[-1]
            page_contexts: list[tuple[int, str, list[str]]] = []
            ordered_blocks = sorted(page.blocks, key=lambda item: item.reading_order)
            for block_index, block in enumerate(ordered_blocks):
                declared_heading_level = block.heading_level
                declared_block_type = block.block_type
                structural_review = block.structural_review if isinstance(block.structural_review, dict) else {}
                block.section_id = ""
                block.section_path = []
                block.section_number = ""
                block.heading_level = None
                block.clause_number = _clause_number_from_text(block.text)
                if block.block_type in {"header", "footer"}:
                    continue
                if toc_page:
                    block.block_type = "toc_entry"
                elif block.block_id in list_block_ids:
                    block.block_type = "list_item"
                reviewed_heading = structural_review.get("is_section_heading") if structural_review.get("status") == "applied" else None
                if reviewed_heading is False:
                    classification = str(structural_review.get("classification") or "body_continuation")
                    if classification == "inline_cross_reference":
                        block.block_type = "cross_reference"
                    elif classification == "list_item":
                        block.block_type = "list_item"
                    elif classification == "table_content":
                        block.block_type = "table_fragment"
                    elif classification == "header_footer":
                        block.block_type = "footer" if len(block.bbox) == 4 and block.bbox[1] >= 0.5 else "header"
                        block.clause_number = ""
                        continue
                    else:
                        block.block_type = "paragraph"
                    heading = None
                elif reviewed_heading is True and not toc_page and block.block_id not in list_block_ids:
                    heading = (
                        _clean_text(str(structural_review.get("canonical_number") or "")),
                        _clean_text(str(structural_review.get("canonical_title") or block.text)),
                        max(1, _coerce_int(structural_review.get("heading_level"), 1)),
                        max(0.0, min(1.0, float(structural_review.get("confidence") or 0.8))),
                    )
                else:
                    duplicate_is_structural = bool(
                        block.duplicate_of
                        and (declared_block_type == "heading" or _potential_structural_line(block.text))
                    )
                    heading = None if (block.duplicate_of and not duplicate_is_structural) or toc_page or block.block_id in list_block_ids else _structural_heading_parts(
                        block.text,
                        font_size=block.font_size,
                        body_size=body_size,
                        is_bold=block.is_bold,
                    )
                if heading and reviewed_heading is not True and _block_is_inline_list_reference(
                    block,
                    ordered_blocks[block_index - 1] if block_index else None,
                    ordered_blocks[block_index + 1] if block_index + 1 < len(ordered_blocks) else None,
                ):
                    heading = None
                    block.block_type = "cross_reference"
                first_page_centered_title = bool(
                    heading
                    and reviewed_heading is not True
                    and not heading[0]
                    and heading[3] <= 0.72
                    and page.page_id == pages[0].page_id
                    and len(block.bbox) == 4
                    and block.bbox[0] > 0.18
                )
                if first_page_centered_title:
                    heading = None
                    block.block_type = "title"
                if (
                    heading is None
                    and reviewed_heading is not False
                    and not block.duplicate_of
                    and not toc_page
                    and declared_block_type == "heading"
                    and declared_heading_level is not None
                    and page.extraction_method == "python_docx"
                    and not _structural_title_is_clause_like(block.text)
                ):
                    heading = ("", _clean_text(block.text), max(1, declared_heading_level), 0.96)
                elif (
                    heading is None
                    and reviewed_heading is not False
                    and not block.duplicate_of
                    and not toc_page
                    and declared_block_type == "heading"
                    and "ocr" in page.extraction_method
                    and len(block.text) <= 180
                    and not _structural_title_is_clause_like(block.text)
                ):
                    heading = ("", _clean_text(block.text), min(6, len(stack) + 1), 0.72)
                if heading is not None:
                    number, title, level, confidence = heading
                    display = _section_display(number, title)
                    if stack and _normalize_for_match(_section_display(stack[-1].number, stack[-1].title)) == _normalize_for_match(display):
                        section = stack[-1]
                        section.page_end = max(section.page_end, page.page_number)
                        if block.block_id not in section.source_block_ids:
                            section.source_block_ids.append(block.block_id)
                    else:
                        parent: StructuralSection | None = None
                        pure_numeric = bool(re.fullmatch(r"\d+(?:\.\d+)*", number))
                        container = next((item for item in reversed(stack) if _section_is_numbering_container(item)), None)
                        if pure_numeric and level == 1 and container is not None and not _text_is_numbering_container(number, title):
                            parent = container
                            level = container.level + 1
                        elif not number and level == 1 and container is not None and not _text_is_numbering_container(number, title):
                            parent = container
                            level = container.level + 1
                        elif number and "." in number and not number.startswith(("FORMAT ", "SECTION ")):
                            parent = number_lookup.get(number.rsplit(".", 1)[0])
                        if parent is not None and all(item.section_id != parent.section_id for item in stack):
                            parent = None
                        if parent is not None:
                            level = parent.level + 1
                            while stack and stack[-1].section_id != parent.section_id:
                                stack.pop()
                        else:
                            while stack and stack[-1].level >= level:
                                stack.pop()
                            parent = stack[-1] if stack else None
                        section = StructuralSection(
                            section_id=f"{logical.logical_document_id}-S{len(logical_sections)}",
                            logical_document_id=logical.logical_document_id,
                            document_id=page.document_id,
                            number=number,
                            title=title,
                            level=max(1, level),
                            parent_section_id=parent.section_id if parent else "",
                            path=[*(parent.path if parent else []), display],
                            page_start=page.page_number,
                            page_end=page.page_number,
                            source_block_ids=[block.block_id],
                            confidence=confidence,
                        )
                        logical_sections.append(section)
                        sections.append(section)
                        stack.append(section)
                        if section.level == 1:
                            number_lookup.clear()
                        if number and re.fullmatch(r"\d+(?:\.\d+)*", number):
                            number_lookup[number] = section
                    block.block_type = "heading"
                    block.section_id = section.section_id
                    block.section_number = section.number
                    block.heading_level = section.level
                    block.clause_number = ""
                    block.section_path = list(section.path)
                elif stack:
                    block.section_id = stack[-1].section_id
                    block.section_path = list(stack[-1].path)
                    for section in stack:
                        section.page_end = max(section.page_end, page.page_number)
                else:
                    unresolved_blocks += 1
                if block.section_path:
                    page_contexts.append((block.reading_order, block.section_id, list(block.section_path)))

            for table in page.tables:
                table.section_id = page_default_section.section_id
                table.section_path = list(page_default_section.path)
                preceding = [context for context in page_contexts if context[0] <= table.reading_order]
                if preceding:
                    _order, table.section_id, table.section_path = max(preceding, key=lambda item: item[0])
        if unresolved_blocks:
            unresolved.append(
                {
                    "logical_document_id": logical.logical_document_id,
                    "reason": "content_before_first_resolved_section_or_unclassified_heading",
                    "block_count": unresolved_blocks,
                }
            )

    blocks = [block for page in all_pages for block in page.blocks if block.text.strip()]
    tables = [table for page in all_pages for table in page.tables]
    content_blocks = [block for block in blocks if block.block_type not in {"header", "footer"}]
    integrity_metrics, integrity_issues = _structural_integrity(all_pages, logical_documents, sections)
    unresolved.extend(integrity_issues)
    metrics = {
        "documents_total": len(logical_documents),
        "pages_total": len(all_pages),
        "pages_with_content": sum(bool(page.text.strip() or page.tables) for page in all_pages),
        "blank_pages": sum(page.is_blank for page in all_pages),
        "toc_pages": sum(_page_is_table_of_contents(page) for page in all_pages),
        "blocks_total": len(blocks),
        "content_blocks_total": len(content_blocks),
        "tables_total": len(tables),
        "cells_total": sum(len(table.cells) for table in tables),
        "sections_total": len(sections),
        "document_roots_total": sum(section.level == 0 for section in sections),
        "explicit_sections_total": sum(section.level > 0 for section in sections),
        "clause_like_sections": sum(section.level > 0 and _structural_title_is_clause_like(section.title) for section in sections),
        "blocks_with_section": sum(bool(block.section_id) for block in content_blocks),
        "tables_with_section": sum(bool(table.section_id) for table in tables),
        "numbered_blocks": sum(bool(block.clause_number or block.section_number) for block in content_blocks),
        "duplicate_blocks": sum(bool(block.duplicate_of) for block in content_blocks),
        "headers_footers": sum(block.block_type in {"header", "footer"} for block in blocks),
        "unresolved_blocks": sum(not block.section_id for block in content_blocks),
        "page_coverage": round(sum(bool(page.text.strip() or page.tables or page.is_blank) for page in all_pages) / max(1, len(all_pages)), 4),
        "content_page_coverage": round(sum(bool(page.text.strip() or page.tables) for page in all_pages) / max(1, len(all_pages)), 4),
        "block_section_coverage": round(sum(bool(block.section_id) for block in content_blocks) / max(1, len(content_blocks)), 4),
        "table_section_coverage": round(sum(bool(table.section_id) for table in tables) / max(1, len(tables)), 4),
        **integrity_metrics,
    }
    packet = StructuralPacket(sections=sections, metrics=metrics, unresolved=unresolved)
    risk_pages = len(_structural_review_candidates(documents, packet))
    packet.metrics["structural_risk_pages_remaining"] = risk_pages
    packet.metrics["structural_failure_count"] = (
        packet.metrics["silent_loss_count"]
        + packet.metrics["weak_pages_remaining"]
        + packet.metrics["unreadable_pages"]
        + risk_pages
    )
    return packet


__all__ = ["reconstruct_document_structure"]
