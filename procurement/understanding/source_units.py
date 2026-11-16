"""Convert extracted tender pages into stable text and table source units."""

from __future__ import annotations

import re
from typing import Any

from procurement.domain.schemas import LayoutTableCell, PageUnit
from procurement.domain.text import normalize_for_match
from procurement.understanding.layout import layout_blocks_from_text
from procurement.understanding.structure import clause_number_from_text


def base_rfp_source_unit_map(pages: list[PageUnit]) -> dict[str, dict[str, Any]]:
    units: dict[str, dict[str, Any]] = {}
    for page in pages:
        blocks = page.blocks or layout_blocks_from_text(page.layout_text or page.text, page.page_id)
        table_text = normalize_for_match("\n".join(table.markdown for table in page.tables))
        table_tokens = set(re.findall(r"\w+", table_text, flags=re.UNICODE))
        for block in blocks:
            normalized = normalize_for_match(block.text)
            block_tokens = re.findall(r"\w+", normalized, flags=re.UNICODE)
            table_overlap = (
                sum(token in table_tokens for token in block_tokens) / len(block_tokens)
                if table_tokens and len(block_tokens) >= 3
                else 0
            )
            if block.block_type == "table" and page.tables:
                continue
            if block.block_type not in {"heading", "header", "footer"} and normalized and normalized in table_text:
                continue
            if block.block_type not in {"heading", "header", "footer"} and table_overlap >= 0.9:
                continue
            if not block.text.strip():
                continue
            units[block.block_id] = {
                "unit_type": block.block_type,
                "text": block.text,
                "document": page.document_id,
                "page": page.page_number,
                "page_id": page.page_id,
                "block": block.block_id,
                "reading_order": block.reading_order,
                "section_id": block.section_id,
                "section_path": block.section_path,
                "clause_number": block.clause_number or block.section_number,
            }
        for table in page.tables:
            rows: dict[int, list[LayoutTableCell]] = {}
            for cell in table.cells:
                rows.setdefault(cell.row_index, []).append(cell)
            headers = [
                cell.text
                for cell in sorted(rows.get(0, []), key=lambda item: item.column_index)
                if cell.text
            ]
            if not rows and table.markdown:
                units[table.table_id] = {
                    "unit_type": "table",
                    "text": table.markdown,
                    "document": page.document_id,
                    "page": page.page_number,
                    "page_id": page.page_id,
                    "table": table.table_id,
                    "section_id": table.section_id,
                    "section_path": table.section_path,
                    "clause_number": clause_number_from_text(table.markdown),
                }
                continue
            for row_index, cells in sorted(rows.items()):
                text = " | ".join(
                    cell.text
                    for cell in sorted(cells, key=lambda item: item.column_index)
                    if cell.text
                )
                if not text:
                    continue
                unit_id = f"{table.table_id}-row-{row_index + 1}"
                units[unit_id] = {
                    "unit_type": "table_header" if row_index == 0 else "table_row",
                    "text": text,
                    "document": page.document_id,
                    "page": page.page_number,
                    "page_id": page.page_id,
                    "table": table.table_id,
                    "section_id": table.section_id,
                    "section_path": table.section_path,
                    "clause_number": clause_number_from_text(text),
                    "table_headers": headers,
                }
        if not any(unit["page_id"] == page.page_id for unit in units.values()) and page.text:
            unit_id = f"{page.page_id}-b1"
            units[unit_id] = {
                "unit_type": "paragraph",
                "text": page.text,
                "document": page.document_id,
                "page": page.page_number,
                "page_id": page.page_id,
                "block": unit_id,
                "section_id": "",
                "section_path": [],
                "clause_number": clause_number_from_text(page.text),
            }
    return units


if __name__ == "__main__":
    page = PageUnit(
        page_id="d1-p1",
        document_id="d1",
        file_name="tender.pdf",
        upload_side="tender",
        page_number=1,
        text="Eligibility: bidder shall submit PAN.",
        extraction_method="native",
    )
    assert base_rfp_source_unit_map([page])["d1-p1-b1"]["page"] == 1
