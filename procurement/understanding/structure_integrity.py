"""Integrity metrics for reconstructed document structure."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any

from procurement.domain.schemas import LogicalDocument, PageUnit, StructuralSection
from procurement.domain.text import normalize_for_match
from .structure_blocks import table_row_units


def structural_integrity(
    pages: list[PageUnit],
    logical_documents: list[LogicalDocument],
    sections: list[StructuralSection],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    page_ids = [page.page_id for page in pages]
    page_id_set = set(page_ids)
    logical_refs = Counter(page_id for logical in logical_documents for page_id in logical.page_refs)
    orphan_pages = sorted(page_id_set - set(logical_refs))
    multiply_referenced_pages = sorted(page_id for page_id, count in logical_refs.items() if count != 1)
    invalid_page_refs = sorted(set(logical_refs) - page_id_set)

    block_ids = [block.block_id for page in pages for block in page.blocks]
    table_ids = [table.table_id for page in pages for table in page.tables]
    cell_ids = [cell.cell_id for page in pages for table in page.tables for cell in table.cells]
    duplicate_id_count = sum(
        len(values) - len(set(values))
        for values in (page_ids, block_ids, table_ids, cell_ids)
    )
    section_ids = {section.section_id for section in sections}
    invalid_section_parent_links = [
        section.section_id
        for section in sections
        if section.level > 0 and section.parent_section_id not in section_ids
    ]
    table_row_ids = {
        row_id
        for page in pages
        for row_id, _text in table_row_units(page)
    }
    invalid_duplicate_links = [
        block.block_id
        for page in pages
        for block in page.blocks
        if block.duplicate_of and block.duplicate_of not in table_row_ids
    ]
    invalid_section_links = [
        item_id
        for page in pages
        for item_id, section_id in [
            *((block.block_id, block.section_id) for block in page.blocks if block.block_type not in {"header", "footer"}),
            *((table.table_id, table.section_id) for table in page.tables),
        ]
        if not section_id or section_id not in section_ids
    ]
    reading_order_errors = [
        page.page_id
        for page in pages
        if sorted([block.reading_order for block in page.blocks] + [table.reading_order for table in page.tables])
        != list(range(len(page.blocks) + len(page.tables)))
    ]
    unrepresented_pages = [
        page.page_id
        for page in pages
        if (page.text.strip() or page.tables) and not (page.blocks or page.tables)
    ]
    weak_pages = [page.page_id for page in pages if page.ocr_required]
    unreadable_pages = [page.page_id for page in pages if not page.is_blank and not page.text.strip() and not page.tables]

    token_coverages: list[float] = []
    character_coverages: list[float] = []
    for page in pages:
        normalized_source = normalize_for_match(page.text)
        source_tokens = Counter(re.findall(r"\w+", normalized_source, flags=re.UNICODE))
        if not source_tokens:
            continue
        ir_text = "\n".join(
            [*(block.text for block in page.blocks), *(cell.text for table in page.tables for cell in table.cells)]
        )
        normalized_ir = normalize_for_match(ir_text)
        ir_tokens = Counter(re.findall(r"\w+", normalized_ir, flags=re.UNICODE))
        covered = sum(min(count, ir_tokens[token]) for token, count in source_tokens.items())
        token_coverages.append(covered / max(1, sum(source_tokens.values())))
        source_characters = Counter(character for character in normalized_source if character.isalnum())
        ir_characters = Counter(character for character in normalized_ir if character.isalnum())
        covered_characters = sum(min(count, ir_characters[character]) for character, count in source_characters.items())
        character_coverages.append(covered_characters / max(1, sum(source_characters.values())))

    issue_groups = {
        "orphan_pages": orphan_pages,
        "multiply_referenced_pages": multiply_referenced_pages,
        "invalid_page_refs": invalid_page_refs,
        "invalid_duplicate_links": invalid_duplicate_links,
        "invalid_section_links": invalid_section_links,
        "invalid_section_parent_links": invalid_section_parent_links,
        "reading_order_errors": reading_order_errors,
        "unrepresented_pages": unrepresented_pages,
        "weak_pages": weak_pages,
        "unreadable_pages": unreadable_pages,
    }
    for reason, source_ids in issue_groups.items():
        if source_ids:
            issues.append({"reason": reason, "count": len(source_ids), "source_ids": source_ids[:100]})
    if duplicate_id_count:
        issues.append({"reason": "duplicate_source_ids", "count": duplicate_id_count})

    provenance_error_count = (
        duplicate_id_count
        + len(orphan_pages)
        + len(multiply_referenced_pages)
        + len(invalid_page_refs)
        + len(invalid_duplicate_links)
        + len(invalid_section_links)
        + len(invalid_section_parent_links)
        + len(reading_order_errors)
    )
    return {
        "logical_page_coverage": round((len(page_ids) - len(orphan_pages)) / max(1, len(page_ids)), 4),
        "source_ids_unique": duplicate_id_count == 0,
        "provenance_error_count": provenance_error_count,
        "invalid_duplicate_links": len(invalid_duplicate_links),
        "invalid_section_links": len(invalid_section_links),
        "invalid_section_parent_links": len(invalid_section_parent_links),
        "reading_order_errors": len(reading_order_errors),
        "unrepresented_pages": len(unrepresented_pages),
        "weak_pages_remaining": len(weak_pages),
        "unreadable_pages": len(unreadable_pages),
        "text_token_coverage_min": round(min(token_coverages), 4) if token_coverages else 1.0,
        "text_token_coverage_mean": round(sum(token_coverages) / len(token_coverages), 4) if token_coverages else 1.0,
        "text_character_coverage_min": round(min(character_coverages), 4) if character_coverages else 1.0,
        "text_character_coverage_mean": round(sum(character_coverages) / len(character_coverages), 4) if character_coverages else 1.0,
        "silent_loss_count": provenance_error_count + len(unrepresented_pages),
    }, issues


__all__ = ["structural_integrity"]
