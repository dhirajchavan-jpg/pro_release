"""Block annotations used by structural reconstruction."""

from __future__ import annotations

import re

from procurement.domain.schemas import LayoutBlock, LayoutTableCell, PageUnit
from procurement.domain.text import normalize_for_match


def repeated_furniture_key(text: str) -> str:
    normalized = normalize_for_match(text)
    return re.sub(r"\b\d+\b", "#", normalized)


def mark_repeated_page_furniture(pages: list[PageUnit]) -> None:
    candidates: dict[tuple[str, str], list[tuple[PageUnit, LayoutBlock]]] = {}
    for page in pages:
        for block in page.blocks:
            if len(block.bbox) != 4 or not block.text.strip():
                continue
            region = "header" if block.bbox[1] <= 0.12 else "footer" if block.bbox[3] >= 0.88 else ""
            if not region:
                continue
            key = repeated_furniture_key(block.text)
            if 0 < len(key) <= 180:
                candidates.setdefault((region, key), []).append((page, block))
    threshold = 3
    for (region, _key), rows in candidates.items():
        if len({page.page_id for page, _block in rows}) < threshold:
            continue
        for _page, block in rows:
            block.block_type = region
            block.section_id = ""
            block.section_path = []


def table_row_units(page: PageUnit) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for table in page.tables:
        by_row: dict[int, list[LayoutTableCell]] = {}
        for cell in table.cells:
            by_row.setdefault(cell.row_index, []).append(cell)
        for row_index, cells in sorted(by_row.items()):
            text = " | ".join(cell.text for cell in sorted(cells, key=lambda item: item.column_index) if cell.text)
            if text:
                rows.append((f"{table.table_id}-row-{row_index + 1}", text))
    return rows


def mark_table_block_overlaps(pages: list[PageUnit]) -> None:
    for page in pages:
        rows = [(unit_id, normalize_for_match(text), set(re.findall(r"\w+", normalize_for_match(text)))) for unit_id, text in table_row_units(page)]
        for block in page.blocks:
            block.duplicate_of = ""
            if block.block_type in {"header", "footer", "table"} or not rows:
                continue
            normalized = normalize_for_match(block.text)
            tokens = re.findall(r"\w+", normalized)
            if not normalized or len(tokens) < 2:
                continue
            token_set = set(tokens)
            best: tuple[float, str] | None = None
            for unit_id, row_text, row_tokens in rows:
                contained = normalized in row_text or row_text in normalized
                coverage = len(token_set & row_tokens) / max(1, min(len(token_set), len(row_tokens)))
                score = 1.0 if contained else coverage
                if score >= 0.92 and (best is None or score > best[0]):
                    best = (score, unit_id)
            if best:
                block.duplicate_of = best[1]


__all__ = [
    "repeated_furniture_key",
    "mark_repeated_page_furniture",
    "table_row_units",
    "mark_table_block_overlaps",
]
