"""Shared layout normalization for native extraction and OCR providers."""

from __future__ import annotations

from html.parser import HTMLParser
import math
import re
from typing import Any, Literal

from procurement.domain.schemas import LayoutBlock, LayoutTable, LayoutTableCell, PageUnit
from procurement.domain.text import clean_text
from procurement.understanding.quality import (
    native_text_integrity_metrics,
    page_text_quality,
    validate_ocr_text,
)


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_bbox(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return []
    try:
        result = [float(item) for item in value]
        return result if all(math.isfinite(item) for item in result) else []
    except (TypeError, ValueError):
        return []


def normalized_bbox(value: Any, width: float, height: float) -> list[float]:
    bbox = coerce_bbox(value)
    if not bbox or width <= 0 or height <= 0:
        return []
    return [bbox[0] / width, bbox[1] / height, bbox[2] / width, bbox[3] / height]


def markdown_from_rows(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    escaped = [[str(value).replace("|", "\\|").replace("\n", " ") for value in row] for row in normalized]
    output = ["| " + " | ".join(escaped[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    output.extend("| " + " | ".join(row) + " |" for row in escaped[1:])
    return "\n".join(output)


def layout_block_type(text: str) -> str:
    probe = clean_text(text)
    if len(probe) <= 180 and (
        re.match(r"^(?:section|chapter|annexure|schedule|part|form)\b", probe, re.I)
        or re.match(r"^\d+(?:\.\d+)*[.)]?\s+[A-Z]", probe)
        or (probe.upper() == probe and any(char.isalpha() for char in probe))
    ):
        return "heading"
    if re.match(r"^(?:[-*]|\d+[.)])\s+", probe):
        return "list_item"
    return "paragraph"


def layout_blocks_from_text(text: str, page_id: str) -> list[LayoutBlock]:
    blocks: list[LayoutBlock] = []
    section_path: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        nonlocal pending, section_path
        if not pending:
            return
        block_text = clean_text(" ".join(pending))
        pending = []
        if not block_text:
            return
        block_type = layout_block_type(block_text)
        if block_type == "heading":
            section_path = [block_text]
        blocks.append(
            LayoutBlock(
                block_id=f"{page_id}-b{len(blocks) + 1}",
                block_type=block_type,
                text=block_text,
                reading_order=len(blocks),
                section_path=list(section_path),
            )
        )

    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        if not line:
            flush()
            continue
        line_type = layout_block_type(line)
        if pending and (
            line_type in {"heading", "list_item"}
            or layout_block_type(pending[0]) == "heading"
        ):
            flush()
        pending.append(line)
    flush()
    return blocks


def layout_tables_from_text(text: str, page_id: str) -> list[LayoutTable]:
    tables: list[LayoutTable] = []
    current: list[list[str]] = []

    def flush() -> None:
        nonlocal current
        if len(current) < 2:
            current = []
            return
        width = max(len(row) for row in current)
        if width < 2:
            current = []
            return
        rows = [row + [""] * (width - len(row)) for row in current]
        table_id = f"{page_id}-t{len(tables) + 1}"
        cells = [
            LayoutTableCell(
                cell_id=f"{table_id}-r{row_index + 1}c{column_index + 1}",
                row_index=row_index,
                column_index=column_index,
                text=value,
                is_header=row_index == 0,
            )
            for row_index, row in enumerate(rows)
            for column_index, value in enumerate(row)
        ]
        tables.append(LayoutTable(table_id=table_id, markdown=markdown_from_rows(rows), cells=cells))
        current = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        columns = [clean_text(value) for value in re.split(r"\s{2,}|\s*\|\s*", line) if clean_text(value)]
        if len(columns) >= 2:
            current.append(columns)
        else:
            flush()
    flush()
    return tables


def layout_tables_from_markdown(markdown: str, page_id: str) -> list[LayoutTable]:
    """Convert pdf-inspector Markdown tables into the existing evidence schema."""
    tables: list[LayoutTable] = []
    rows: list[list[str]] = []
    has_separator = False
    expected_width = 0

    def flush() -> None:
        nonlocal rows, has_separator, expected_width
        if has_separator and len(rows) >= 2:
            width = expected_width or max(len(row) for row in rows)
            normalized = []
            for row in rows:
                if len(row) > width:
                    row = row[: width - 1] + [" | ".join(row[width - 1 :])]
                normalized.append(row + [""] * (width - len(row)))
            table_id = f"{page_id}-t{len(tables) + 1}"
            cells = [
                LayoutTableCell(
                    cell_id=f"{table_id}-r{row_index + 1}c{column_index + 1}",
                    row_index=row_index,
                    column_index=column_index,
                    text=value,
                    is_header=row_index == 0,
                )
                for row_index, row in enumerate(normalized)
                for column_index, value in enumerate(row)
            ]
            tables.append(LayoutTable(table_id=table_id, markdown=markdown_from_rows(normalized), cells=cells))
        rows = []
        has_separator = False
        expected_width = 0

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            flush()
            continue
        values = [value.strip().replace(r"\|", "|") for value in re.split(r"(?<!\\)\|", line[1:-1])]
        if values and all(re.fullmatch(r":?-{3,}:?", value) for value in values):
            has_separator = True
            expected_width = len(values)
        else:
            rows.append([clean_text(value) for value in values])
    flush()
    return tables


def assign_page_reading_order(blocks: list[LayoutBlock], tables: list[LayoutTable]) -> None:
    items: list[LayoutBlock | LayoutTable] = [*blocks, *tables]
    if not items:
        return
    use_geometry = all(len(item.bbox) == 4 for item in items)
    if use_geometry:
        ordered = sorted(items, key=lambda item: (item.bbox[1], item.bbox[0], isinstance(item, LayoutTable)))
    else:
        ordered = sorted(items, key=lambda item: (item.reading_order, isinstance(item, LayoutTable)))
    for reading_order, item in enumerate(ordered):
        item.reading_order = reading_order


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            attributes = dict(attrs)
            self.cell = {
                "text": "",
                "row_span": max(1, coerce_int(attributes.get("rowspan"), 1)),
                "column_span": max(1, coerce_int(attributes.get("colspan"), 1)),
                "is_header": tag == "th",
            }
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += "\n"
        elif tag == "img" and self.cell is not None:
            alt = dict(attrs).get("alt") or ""
            if alt:
                self.cell["text"] += f" {alt} "

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.cell["text"] = clean_text(self.cell["text"])
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def table_from_html(html: str, table_id: str, bbox: list[float] | None = None) -> LayoutTable:
    parser = _TableHTMLParser()
    parser.feed(html)
    occupied: set[tuple[int, int]] = set()
    cells: list[LayoutTableCell] = []
    for row_index, row in enumerate(parser.rows):
        column_index = 0
        for item in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            row_span = item["row_span"]
            column_span = item["column_span"]
            cells.append(
                LayoutTableCell(
                    cell_id=f"{table_id}-r{row_index + 1}c{column_index + 1}",
                    row_index=row_index,
                    column_index=column_index,
                    row_span=row_span,
                    column_span=column_span,
                    is_header=item["is_header"] or row_index == 0,
                    text=item["text"],
                )
            )
            for covered_row in range(row_index, row_index + row_span):
                for covered_column in range(column_index, column_index + column_span):
                    if covered_row != row_index or covered_column != column_index:
                        occupied.add((covered_row, covered_column))
            column_index += column_span
    width = max((cell.column_index + cell.column_span for cell in cells), default=0)
    height = max((cell.row_index + cell.row_span for cell in cells), default=0)
    rows = [[""] * width for _ in range(height)]
    for cell in cells:
        rows[cell.row_index][cell.column_index] = cell.text
    return LayoutTable(table_id=table_id, markdown=markdown_from_rows(rows), cells=cells, bbox=bbox or [])


def paddle_layout_block_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {
        "block_label": str(getattr(value, "label", "other") or "other"),
        "block_content": str(getattr(value, "content", "") or ""),
        "block_bbox": list(getattr(value, "bbox", []) or []),
        "block_id": getattr(value, "index", None),
        "block_order": getattr(value, "order_index", None),
    }


def normalize_page_layout_data(data: Any, page_id: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("OCR returned a non-object page layout")
    text, deterministic_metrics = validate_ocr_text(data.get("text") or "")
    block_payload = data.get("blocks") or []
    table_payload = data.get("tables") or []
    if not isinstance(block_payload, list) or len(block_payload) > 5_000:
        raise ValueError("OCR returned an invalid number of layout blocks")
    if not isinstance(table_payload, list) or len(table_payload) > 500:
        raise ValueError("OCR returned an invalid number of tables")
    blocks: list[LayoutBlock] = []
    for index, item in enumerate(block_payload):
        if not isinstance(item, dict):
            continue
        block_text = clean_text(str(item.get("text") or ""))
        if not block_text:
            continue
        blocks.append(
            LayoutBlock(
                block_id=f"{page_id}-b{len(blocks) + 1}",
                block_type=str(item.get("block_type") or layout_block_type(block_text)),
                text=block_text,
                reading_order=int(item.get("reading_order") if isinstance(item.get("reading_order"), int) else index),
                bbox=coerce_bbox(item.get("bbox")),
            )
        )
    if not blocks and text:
        blocks = layout_blocks_from_text(text, page_id)

    tables: list[LayoutTable] = []
    for table_index, item in enumerate(table_payload, start=1):
        if not isinstance(item, dict):
            continue
        table_id = f"{page_id}-t{table_index}"
        cells: list[LayoutTableCell] = []
        cell_payload = item.get("cells") or []
        if not isinstance(cell_payload, list) or len(cell_payload) > 10_000:
            raise ValueError("OCR returned an invalid number of table cells")
        for cell in cell_payload:
            if not isinstance(cell, dict):
                continue
            row_index = max(0, coerce_int(cell.get("row_index")))
            column_index = max(0, coerce_int(cell.get("column_index")))
            if row_index >= 500 or column_index >= 500:
                raise ValueError("OCR returned an invalid table cell position")
            cells.append(
                LayoutTableCell(
                    cell_id=f"{table_id}-r{row_index + 1}c{column_index + 1}",
                    row_index=row_index,
                    column_index=column_index,
                    row_span=max(1, coerce_int(cell.get("row_span"), 1)),
                    column_span=max(1, coerce_int(cell.get("column_span"), 1)),
                    is_header=bool(cell.get("is_header")),
                    text=clean_text(str(cell.get("text") or "")),
                    bbox=coerce_bbox(cell.get("bbox")),
                )
            )
        markdown = str(item.get("markdown") or "").strip()
        if not markdown and cells:
            width = max(cell.column_index for cell in cells) + 1
            height = max(cell.row_index for cell in cells) + 1
            rows = [[""] * width for _ in range(height)]
            for cell in cells:
                rows[cell.row_index][cell.column_index] = cell.text
            markdown = markdown_from_rows(rows)
        tables.append(
            LayoutTable(
                table_id=table_id,
                title=clean_text(str(item.get("title") or "")),
                markdown=markdown,
                reading_order=coerce_int(item.get("reading_order"), table_index),
                cells=cells,
                bbox=coerce_bbox(item.get("bbox")),
            )
        )
    assign_page_reading_order(blocks, tables)
    metrics = dict(data.get("quality_metrics")) if isinstance(data.get("quality_metrics"), dict) else {}
    metrics.update(deterministic_metrics)
    try:
        claimed_quality = float(data.get("quality_score") or deterministic_metrics["score"])
    except (TypeError, ValueError) as exc:
        raise ValueError("OCR returned an invalid quality score") from exc
    if not math.isfinite(claimed_quality):
        raise ValueError("OCR returned an invalid quality score")
    quality_score = min(max(0.0, claimed_quality), float(deterministic_metrics["score"]))
    blank_visual_page = bool(
        not text
        and str(data.get("provider") or "") == "visual_blank_page"
        and metrics.get("blank_visual_page") is True
        and float(metrics.get("visual_standard_deviation") or math.inf) < 2.0
    )
    return {
        "text": text,
        "blocks": blocks,
        "tables": tables,
        "provider": str(data.get("provider") or "gemini_layout_ocr"),
        "quality_score": quality_score,
        "quality_metrics": metrics,
        "blank_visual_page": blank_visual_page,
    }


def clean_layout_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def make_page_unit(
    document_id: str,
    file_name: str,
    side: Literal["tender", "vendor"],
    page_number: int,
    text: str,
    extraction_method: str,
    ocr_required: bool,
    warnings: list[str],
    layout_text: str = "",
    blocks: list[LayoutBlock] | None = None,
    tables: list[LayoutTable] | None = None,
    raw_text: str = "",
) -> PageUnit:
    quality = page_text_quality(text)
    quality.update(native_text_integrity_metrics(text, raw_text))
    if quality["native_text_integrity_gate"]:
        quality["score"] = min(float(quality["score"]), 0.1)
    resolved_blocks = blocks if blocks is not None else layout_blocks_from_text(layout_text or text, f"{document_id}-p{page_number}")
    resolved_tables = tables if tables is not None else []
    assign_page_reading_order(resolved_blocks, resolved_tables)
    return PageUnit(
        page_id=f"{document_id}-p{page_number}",
        document_id=document_id,
        file_name=file_name,
        upload_side=side,
        page_number=page_number,
        text=text,
        layout_text=layout_text or text,
        blocks=resolved_blocks,
        tables=resolved_tables,
        text_chars=len(text),
        extraction_method=extraction_method,
        extraction_quality=quality["score"],
        quality_metrics=quality,
        ocr_required=ocr_required,
        warnings=warnings,
    )
