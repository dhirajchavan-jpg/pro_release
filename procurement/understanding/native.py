"""Native PDF, DOCX, and XLSX layout readers."""

from __future__ import annotations

from pathlib import Path
import re
from statistics import median
from types import SimpleNamespace
from typing import Any, Literal

from procurement.domain.schemas import LayoutBlock, LayoutTable, LayoutTableCell, PageUnit
from procurement.domain.text import clean_text
from procurement.understanding.layout import (
    clean_layout_text,
    layout_block_type,
    layout_blocks_from_text,
    layout_tables_from_markdown,
    make_page_unit,
    markdown_from_rows,
    normalized_bbox,
)
from procurement.understanding.quality import (
    OCR_MIN_PAGE_CHARS,
    mark_repeated_boilerplate_pages_for_ocr,
    native_text_integrity_metrics,
    page_requires_ocr,
    page_text_quality,
)
from procurement.understanding.structure import (
    STRUCTURAL_NUMBER,
    clause_number_from_text,
    potential_structural_line,
    structural_heading_parts,
    structural_title_is_clause_like,
)


def pdfplumber_page_blocks(page: Any, page_id: str) -> list[LayoutBlock]:
    words = page.extract_words(
        use_text_flow=True,
        keep_blank_chars=False,
        extra_attrs=["fontname", "size"],
    )
    if not words:
        return []
    sizes = [float(word.get("size") or 0) for word in words if float(word.get("size") or 0) > 0]
    body_size = float(median(sizes)) if sizes else 0.0
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (float(item.get("top") or 0), float(item.get("x0") or 0))):
        top = float(word.get("top") or 0)
        size = float(word.get("size") or 0)
        tolerance = max(2.0, size * 0.25)
        line = lines[-1] if lines and abs(top - float(lines[-1]["top"])) <= tolerance else None
        if line is None:
            line = {"words": [], "top": top, "bottom": float(word.get("bottom") or top), "x0": float(word.get("x0") or 0), "x1": float(word.get("x1") or 0)}
            lines.append(line)
        line["words"].append(word)
        line["top"] = min(float(line["top"]), top)
        line["bottom"] = max(float(line["bottom"]), float(word.get("bottom") or top))
        line["x0"] = min(float(line["x0"]), float(word.get("x0") or 0))
        line["x1"] = max(float(line["x1"]), float(word.get("x1") or 0))

    normalized_lines: list[dict[str, Any]] = []
    for line in lines:
        line_words = sorted(line["words"], key=lambda item: float(item.get("x0") or 0))
        text = clean_text(" ".join(str(word.get("text") or "") for word in line_words))
        if not text:
            continue
        line_sizes = [float(word.get("size") or 0) for word in line_words if float(word.get("size") or 0) > 0]
        font_size = float(median(line_sizes)) if line_sizes else 0.0
        fonts = [str(word.get("fontname") or "") for word in line_words]
        font_name = max(set(fonts), key=fonts.count) if fonts else ""
        is_bold = sum("bold" in font.lower() for font in fonts) >= max(1, len(fonts) // 2)
        normalized_lines.append(
            {
                **line,
                "text": text,
                "font_size": font_size,
                "font_name": font_name,
                "is_bold": is_bold,
                "heading": structural_heading_parts(text, font_size=font_size, body_size=body_size, is_bold=is_bold),
            }
        )

    groups: list[list[dict[str, Any]]] = []
    for line in normalized_lines:
        previous = groups[-1][-1] if groups else None
        starts_list = bool(re.match(r"^(?:[-*]|[a-zA-Zivxlcdm]+[.)])\s+", line["text"], re.IGNORECASE))
        numbered_clause = bool(STRUCTURAL_NUMBER.match(line["text"])) and line["heading"] is None
        gap = float(line["top"]) - float(previous["bottom"]) if previous else 0.0
        same_heading = bool(
            previous
            and previous["heading"]
            and line["heading"]
            and not line["heading"][0]
            and (
                not previous["heading"][0]
                or abs(float(previous["x0"]) - float(line["x0"])) <= max(48.0, body_size * 4)
            )
            and abs(float(previous["font_size"]) - float(line["font_size"])) <= 1.0
            and gap <= max(6.0, body_size * 0.65)
        )
        heading_continuation = bool(
            previous
            and previous["heading"]
            and not line["heading"]
            and not structural_title_is_clause_like(line["text"])
            and not starts_list
            and not numbered_clause
            and bool(previous["is_bold"]) == bool(line["is_bold"])
            and abs(float(previous["font_size"]) - float(line["font_size"])) <= 1.0
            and len(line["text"]) <= 140
            and gap <= max(6.0, body_size * 0.65)
        )
        paragraph_continuation = bool(
            previous
            and not previous["heading"]
            and not line["heading"]
            and not any(item["heading"] for item in groups[-1])
            and not any(potential_structural_line(item["text"]) for item in groups[-1])
            and not starts_list
            and not numbered_clause
            and not potential_structural_line(line["text"])
            and gap <= max(5.0, body_size * 0.75)
        )
        if same_heading or heading_continuation or paragraph_continuation:
            groups[-1].append(line)
        else:
            groups.append([line])

    blocks: list[LayoutBlock] = []
    for group in groups:
        text = clean_text(" ".join(line["text"] for line in group))
        sizes = [float(line["font_size"]) for line in group if float(line["font_size"]) > 0]
        font_size = float(median(sizes)) if sizes else 0.0
        fonts = [str(line["font_name"]) for line in group if line["font_name"]]
        font_name = max(set(fonts), key=fonts.count) if fonts else ""
        is_bold = sum(bool(line["is_bold"]) for line in group) >= max(1, len(group) // 2)
        heading = structural_heading_parts(text, font_size=font_size, body_size=body_size, is_bold=is_bold)
        block_type = "heading" if heading else layout_block_type(text)
        if block_type == "heading" and heading is None:
            block_type = "paragraph"
        blocks.append(
            LayoutBlock(
                block_id=f"{page_id}-b{len(blocks) + 1}",
                block_type=block_type,
                text=text,
                reading_order=len(blocks),
                bbox=normalized_bbox(
                    [min(float(line["x0"]) for line in group), min(float(line["top"]) for line in group), max(float(line["x1"]) for line in group), max(float(line["bottom"]) for line in group)],
                    float(page.width),
                    float(page.height),
                ),
                font_size=round(font_size, 3),
                font_name=font_name,
                is_bold=is_bold,
                section_number=heading[0] if heading else "",
                heading_level=heading[2] if heading else None,
                clause_number=clause_number_from_text(text) if not heading else "",
            )
        )
    return blocks


def pdf_inspector_page_blocks(items: list[Any], page_id: str, width: float, height: float) -> list[LayoutBlock]:
    """Reuse the proven block builder with pdf-inspector positioned words."""
    words = []
    for item in items:
        if str(getattr(item, "item_type", "text")) != "text":
            continue
        text = clean_text(str(getattr(item, "text", "")))
        if not text:
            continue
        x0 = float(getattr(item, "x", 0.0))
        y = float(getattr(item, "y", 0.0))
        item_width = max(0.0, float(getattr(item, "width", 0.0)))
        item_height = max(0.0, float(getattr(item, "height", 0.0)))
        top = max(0.0, height - y - item_height)
        words.append(
            {
                "text": text,
                "x0": x0,
                "x1": min(width, x0 + item_width),
                "top": top,
                "bottom": min(height, top + item_height),
                "size": float(getattr(item, "font_size", 0.0)),
                "fontname": str(getattr(item, "font", "")),
            }
        )
    if not words:
        return []
    page = SimpleNamespace(width=width, height=height, extract_words=lambda **_kwargs: words)
    return pdfplumber_page_blocks(page, page_id)


def extract_pdf(path: Path, side: Literal["tender", "vendor"], document_id: str, maximum_pages: int) -> tuple[str, list[str], list[PageUnit]]:
    import pdf_inspector
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if len(reader.pages) > maximum_pages:
        raise ValueError(f"PDF contains more than {maximum_pages} pages")

    extracted = pdf_inspector.extract_pages_markdown(str(path))
    positioned = pdf_inspector.extract_text_with_positions(str(path))
    page_results = {int(page.page) + 1: page for page in extracted.pages}
    positioned_page_offset = 1 if positioned and min(int(item.page) for item in positioned) == 0 else 0
    items_by_page: dict[int, list[Any]] = {}
    for item in positioned:
        items_by_page.setdefault(int(item.page) + positioned_page_offset, []).append(item)

    text_parts: list[str] = []
    pages: list[PageUnit] = []
    warnings: list[str] = []
    for index, pdf_page in enumerate(reader.pages, start=1):
        page_id = f"{document_id}-p{index}"
        page_result = page_results.get(index)
        layout_text = clean_layout_text(str(getattr(page_result, "markdown", "") or ""))
        text = clean_text(layout_text)
        inspector_ocr_required = bool(getattr(page_result, "needs_ocr", True))
        raw_text = ""
        if any(character.isalpha() and not character.isascii() for character in text):
            raw_text = pdf_page.extract_text() or ""
        native_metrics = native_text_integrity_metrics(text, raw_text)
        heuristic_ocr_required = page_requires_ocr(text, raw_text)
        ocr_required = bool(inspector_ocr_required or heuristic_ocr_required)
        page_warnings = []
        if ocr_required:
            if inspector_ocr_required or page_text_quality(text)["characters"] < OCR_MIN_PAGE_CHARS:
                page_warnings.append("weak or image-based page; OCR required")
            if native_metrics["native_text_integrity_gate"]:
                page_warnings.append("native text integrity below threshold; OCR required")
        width = float(pdf_page.mediabox.width)
        height = float(pdf_page.mediabox.height)
        blocks = pdf_inspector_page_blocks(items_by_page.get(index, []), page_id, width, height)
        if not blocks and text:
            blocks = layout_blocks_from_text(layout_text, page_id)
        tables = layout_tables_from_markdown(layout_text, page_id)
        for table_index, table in enumerate(tables):
            table.reading_order = len(blocks) + table_index
        if page_warnings:
            warnings.extend(f"page {index}: {warning}" for warning in page_warnings)
        pages.append(
            make_page_unit(
                document_id,
                path.name,
                side,
                index,
                text,
                "pdf_inspector",
                ocr_required,
                page_warnings,
                layout_text,
                blocks,
                tables,
                raw_text=raw_text,
            )
        )
        if text:
            text_parts.append(f"[page {index}]\n{text}")
    for page_number in mark_repeated_boilerplate_pages_for_ocr(pages):
        warnings.append(f"page {page_number}: repeated boilerplate obscures page content; OCR required")
    return clean_text("\n\n".join(text_parts)), warnings, pages


def extract_docx_layout(path: Path, document_id: str = "document") -> tuple[str, list[LayoutBlock], list[LayoutTable]]:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []
    blocks: list[LayoutBlock] = []
    tables: list[LayoutTable] = []
    reading_order = 0
    table_index = 0
    items = doc.iter_inner_content() if hasattr(doc, "iter_inner_content") else [*doc.paragraphs, *doc.tables]
    for item in items:
        if hasattr(item, "rows"):
            table_index += 1
            table_id = f"{document_id}-p1-t{table_index}"
            cells: list[LayoutTableCell] = []
            markdown_rows: list[list[str]] = []
            for row_index, row in enumerate(item.rows):
                values = [clean_text(cell.text) for cell in row.cells]
                markdown_rows.append(values)
                for column_index, value in enumerate(values):
                    cells.append(
                        LayoutTableCell(
                            cell_id=f"{table_id}-r{row_index + 1}c{column_index + 1}",
                            row_index=row_index,
                            column_index=column_index,
                            text=value,
                            is_header=row_index == 0,
                        )
                    )
            markdown = markdown_from_rows(markdown_rows)
            tables.append(LayoutTable(table_id=table_id, markdown=markdown, reading_order=reading_order, cells=cells))
            if markdown:
                parts.append(markdown)
            reading_order += 1
            continue
        text = clean_text(getattr(item, "text", ""))
        if not text:
            continue
        style = getattr(item, "style", None)
        style_name = str(getattr(style, "name", "") or "")
        style_probe = style_name.lower()
        runs = list(getattr(item, "runs", []) or [])
        run_sizes = [run.font.size.pt for run in runs if getattr(run.font, "size", None)]
        font_size = float(median(run_sizes)) if run_sizes else float(getattr(getattr(style, "font", None), "size", 0).pt if getattr(getattr(style, "font", None), "size", None) else 0)
        font_names = [str(run.font.name) for run in runs if getattr(run.font, "name", None)]
        font_name = max(set(font_names), key=font_names.count) if font_names else str(getattr(getattr(style, "font", None), "name", "") or "")
        is_bold = any(run.bold is True for run in runs) or bool(getattr(getattr(style, "font", None), "bold", False))
        heading_match = re.match(r"heading\s*(\d+)", style_probe)
        if heading_match or style_probe in {"title", "subtitle"}:
            block_type = "heading"
            heading_level = int(heading_match.group(1)) if heading_match else 1
        elif style_probe.startswith("list"):
            block_type = "list_item"
            heading_level = None
        else:
            block_type = layout_block_type(text)
            heading_level = None
        parts.append(text)
        blocks.append(
            LayoutBlock(
                block_id=f"{document_id}-p1-b{reading_order + 1}",
                block_type=block_type,
                text=text,
                reading_order=reading_order,
                font_size=round(font_size, 3),
                font_name=font_name,
                is_bold=is_bold,
                heading_level=heading_level,
            )
        )
        reading_order += 1
    return "\n\n".join(parts), blocks, tables


def extract_docx(path: Path) -> str:
    return extract_docx_layout(path)[0]


def extract_xlsx_pages(path: Path, side: Literal["tender", "vendor"], document_id: str) -> tuple[str, list[PageUnit]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    page_units: list[PageUnit] = []
    document_parts: list[str] = []
    try:
        for sheet_index, sheet in enumerate(workbook.worksheets, start=1):
            table_id = f"{document_id}-p{sheet_index}-t1"
            cells: list[LayoutTableCell] = []
            markdown_rows: list[list[str]] = []
            max_column = 0
            for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                values = ["" if value is None else str(value) for value in row]
                while values and not values[-1]:
                    values.pop()
                if not any(values):
                    continue
                max_column = max(max_column, len(values))
                markdown_rows.append(values)
                for column_index, value in enumerate(values):
                    if not value:
                        continue
                    cells.append(
                        LayoutTableCell(
                            cell_id=f"{table_id}-r{row_index + 1}c{column_index + 1}",
                            row_index=row_index,
                            column_index=column_index,
                            text=value,
                            is_header=len(markdown_rows) == 1,
                        )
                    )
            if max_column:
                markdown_rows = [row + [""] * (max_column - len(row)) for row in markdown_rows]
            markdown = markdown_from_rows(markdown_rows)
            sheet_text = clean_text(f"[sheet {sheet.title}]\n{markdown}")
            table = LayoutTable(table_id=table_id, title=sheet.title, markdown=markdown, reading_order=1, cells=cells)
            page = make_page_unit(
                document_id,
                path.name,
                side,
                sheet_index,
                sheet_text,
                "openpyxl",
                False,
                [],
                sheet_text,
                [LayoutBlock(block_id=f"{document_id}-p{sheet_index}-b1", block_type="title", text=sheet.title, reading_order=0)],
                [table],
            )
            page_units.append(page)
            document_parts.append(sheet_text)
    finally:
        workbook.close()
    return clean_text("\n\n".join(document_parts)), page_units


def extract_xlsx(path: Path) -> str:
    return extract_xlsx_pages(path, "tender", "document")[0]
