from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "LANGGRAPH_MULTI_AGENT_IMPLEMENTATION_PLAN.md"
OUTPUT = ROOT / "docs" / "LangGraph_Multi_Agent_Implementation_Plan.docx"

NAVY = "17365D"
BLUE = "2F75B5"
PALE_BLUE = "EAF2F8"
PALE_GRAY = "F3F5F7"
MID_GRAY = "D9E1E8"
TEXT = RGBColor(31, 31, 31)


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top=90, start=100, bottom=90, end=100) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for edge, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_keep_with_next(paragraph, value=True) -> None:
    paragraph.paragraph_format.keep_with_next = value


def remove_paragraph_borders(paragraph_or_style) -> None:
    p_pr = paragraph_or_style._element.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is not None:
        p_pr.remove(p_bdr)


def style_run(run, *, bold=None, italic=None, code=False, color=None, size=None) -> None:
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    font_name = "Consolas" if code else "Aptos"
    run.font.name = font_name
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), font_name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), font_name)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    if size:
        run.font.size = Pt(size)


INLINE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))")


def add_inline(paragraph, text: str, *, size=None) -> None:
    cursor = 0
    for match in INLINE.finditer(text):
        if match.start() > cursor:
            style_run(paragraph.add_run(text[cursor:match.start()]), size=size)
        token = match.group(0)
        if token.startswith("`"):
            style_run(paragraph.add_run(token[1:-1]), code=True, color="1F4E79", size=size or 9.5)
        elif token.startswith("**"):
            style_run(paragraph.add_run(token[2:-2]), bold=True, size=size)
        else:
            link_match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token)
            label, url = link_match.groups() if link_match else (token, "")
            style_run(paragraph.add_run(f"{label} ({url})"), color=BLUE, size=size)
        cursor = match.end()
    if cursor < len(text):
        style_run(paragraph.add_run(text[cursor:]), size=size)


def configure_styles(doc: Document) -> None:
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Aptos"
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = TEXT
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.08

    title = styles["Title"]
    title.font.name = "Aptos Display"
    title.font.size = Pt(26)
    title.font.bold = True
    title.font.color.rgb = RGBColor(0, 0, 0)
    title.paragraph_format.space_after = Pt(14)
    remove_paragraph_borders(title)

    for name, size, before, after in (
        ("Heading 1", 18, 18, 8),
        ("Heading 2", 14, 14, 6),
        ("Heading 3", 11.5, 10, 4),
    ):
        style = styles[name]
        style.font.name = "Aptos Display"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    for name in ("List Bullet", "List Number"):
        style = styles[name]
        style.font.name = "Aptos"
        style.font.size = Pt(10.25)
        style.paragraph_format.space_after = Pt(3)


def add_header_footer(doc: Document) -> None:
    for section in doc.sections:
        section.top_margin = Inches(0.72)
        section.bottom_margin = Inches(0.7)
        section.left_margin = Inches(0.75)
        section.right_margin = Inches(0.75)
        section.header_distance = Inches(0.3)
        section.footer_distance = Inches(0.35)

        header = section.header
        hp = header.paragraphs[0]
        hp.text = "Procurement orchestration implementation plan"
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for run in hp.runs:
            style_run(run, color="666666", size=8)

        footer = section.footer
        fp = footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        style_run(fp.add_run("Implementation blueprint  |  12 October 2026"), color="777777", size=8)


def add_cover(doc: Document) -> None:
    p = doc.add_paragraph(style="Title")
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    remove_paragraph_borders(p)
    p.add_run("LangGraph Multi Agent Implementation Plan")

    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(18)
    style_run(subtitle.add_run("Procurement Evaluation Platform"), bold=True, size=14, color=NAVY)

    intro = doc.add_paragraph()
    intro.paragraph_format.space_after = Pt(14)
    add_inline(
        intro,
        "A production implementation blueprint for introducing LangGraph and LangChain orchestration while preserving the existing procurement logic, audit controls, human review gates, provider behavior, and cost profile.",
        size=11,
    )

    decision = doc.add_paragraph()
    decision.paragraph_format.space_after = Pt(8)
    style_run(decision.add_run("Recommended decision  "), bold=True, color=NAVY, size=11)
    add_inline(
        decision,
        "Use a deterministic LangGraph control plane around the current durable worker and bounded specialist functions. Do not add an LLM supervisor.",
        size=11,
    )

    meta = doc.add_paragraph()
    meta.paragraph_format.space_before = Pt(18)
    meta.paragraph_format.space_after = Pt(0)
    style_run(meta.add_run("Prepared against repository state on 12 October 2026"), color="666666", size=9.5)

    scope = doc.add_paragraph()
    style_run(scope.add_run("Status  Implementation blueprint only  No business logic changes"), color="666666", size=9.5)

    doc.add_page_break()


def add_contents(doc: Document) -> None:
    doc.add_heading("Contents", level=1)
    entries = [
        "1 Executive decision",
        "2 Why this pattern fits this repository",
        "3 Target architecture",
        "4 Agent catalog and responsibilities",
        "5 State design and routing",
        "6 Human review persistence and durability",
        "7 LangChain usage and file level implementation",
        "8 Parallelism and cost controls",
        "9 Observability and security",
        "10 Migration testing rollout and definition of done",
    ]
    for item in entries:
        p = doc.add_paragraph(style="List Bullet")
        add_inline(p, item)
    doc.add_page_break()


def add_architecture_visual(doc: Document) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(7)
    style_run(p.add_run("Complete project flow"), bold=True, color=NAVY, size=10.5)

    rows = [
        ("1", "Load and pin", "Load project, tender version, bids, revisions, and current operations"),
        ("2", "Understand tender", "Extract, OCR, structure, field understanding, and tender review"),
        ("3", "Check compliance", "Run deterministic rules and bounded semantic audit, then resolve findings"),
        ("4", "Freeze contract", "Build, review, and freeze the existing evaluation checklist"),
        ("5", "Classify bids", "Extract and classify bidder packets, quarantine price content, and approve"),
        ("6", "Evaluate bidders", "Retrieve evidence, run specialists, verify, validate, and judge criteria"),
        ("7", "Generate report", "Refresh the existing recommendation and generate committee reports"),
        ("8", "Committee review", "Human decision remains final; no automatic procurement award"),
    ]
    table = doc.add_table(rows=1, cols=3)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    widths = (0.5, 1.6, 4.95)
    headers = ("Step", "Stage", "Authoritative behavior")
    for idx, (cell, value) in enumerate(zip(table.rows[0].cells, headers)):
        cell.width = Inches(widths[idx])
        set_cell_shading(cell, NAVY)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        set_cell_margins(cell)
        cell.text = value
        for run in cell.paragraphs[0].runs:
            style_run(run, bold=True, color="FFFFFF", size=9)
    set_repeat_table_header(table.rows[0])
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for idx, (cell, value) in enumerate(zip(cells, values)):
            cell.width = Inches(widths[idx])
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            set_cell_margins(cell)
            if row_index % 2:
                set_cell_shading(cell, PALE_BLUE)
            cell.text = value
            for run in cell.paragraphs[0].runs:
                style_run(run, bold=(idx == 1), size=8.7)
            if idx == 0:
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    after = doc.add_paragraph()
    after.paragraph_format.space_before = Pt(7)
    add_inline(after, "Every transition reloads authoritative PostgreSQL state. Failed or retryable operations continue to use the current operation lifecycle.")


def add_markdown_table(doc: Document, lines: list[str]) -> None:
    parsed = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    if len(parsed) < 2:
        return
    headers = parsed[0]
    body = parsed[2:] if all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in parsed[1]) else parsed[1:]
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    for cell, value in zip(table.rows[0].cells, headers):
        set_cell_shading(cell, NAVY)
        set_cell_margins(cell)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        cell.text = ""
        add_inline(cell.paragraphs[0], value, size=8.3)
        for run in cell.paragraphs[0].runs:
            run.font.color.rgb = RGBColor(255, 255, 255)
            run.bold = True
    set_repeat_table_header(table.rows[0])
    for row_index, values in enumerate(body):
        cells = table.add_row().cells
        for idx, cell in enumerate(cells):
            value = values[idx] if idx < len(values) else ""
            set_cell_margins(cell)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if row_index % 2:
                set_cell_shading(cell, PALE_BLUE)
            cell.text = ""
            add_inline(cell.paragraphs[0], value, size=8.1)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(1)


def add_code_block(doc: Document, lines: list[str]) -> None:
    if not lines:
        return
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.18)
    p.paragraph_format.right_indent = Inches(0.12)
    p.paragraph_format.space_before = Pt(3)
    p.paragraph_format.space_after = Pt(7)
    p.paragraph_format.line_spacing = 1.0
    p_pr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), PALE_GRAY)
    p_pr.append(shd)
    for index, line in enumerate(lines):
        run = p.add_run(line)
        style_run(run, code=True, size=7.8)
        if index < len(lines) - 1:
            run.add_break()


def build() -> None:
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    doc = Document()
    configure_styles(doc)
    add_header_footer(doc)
    add_cover(doc)
    add_contents(doc)

    i = 0
    skipped_source_title = False
    while i < len(lines):
        raw = lines[i]
        text = raw.strip()
        if not text:
            i += 1
            continue

        if text.startswith("# ") and not skipped_source_title:
            skipped_source_title = True
            i += 1
            continue
        if text.startswith("Status:") or text.startswith("Prepared against"):
            i += 1
            continue

        if text.startswith("```"):
            lang = text[3:].strip().lower()
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i].rstrip())
                i += 1
            i += 1
            if lang == "mermaid":
                add_architecture_visual(doc)
            else:
                add_code_block(doc, block)
            continue

        if text.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            add_markdown_table(doc, table_lines)
            continue

        heading = re.match(r"^(#{2,4})\s+(.+)$", text)
        if heading:
            hashes, heading_text = heading.groups()
            level = min(len(hashes) - 1, 3)
            p = doc.add_heading(heading_text, level=level)
            set_keep_with_next(p)
            i += 1
            continue

        bullet = re.match(r"^-\s+(.+)$", text)
        if bullet:
            p = doc.add_paragraph(style="List Bullet")
            add_inline(p, bullet.group(1))
            i += 1
            continue

        numbered = re.match(r"^\d+\.\s+(.+)$", text)
        if numbered:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.23)
            p.paragraph_format.first_line_indent = Inches(-0.18)
            p.paragraph_format.space_after = Pt(3)
            number = text.split(".", 1)[0]
            style_run(p.add_run(f"{number}. "), bold=True)
            add_inline(p, numbered.group(1))
            i += 1
            continue

        paragraph_lines = [text]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or nxt.startswith(("#", "- ", "```", "|")) or re.match(r"^\d+\.\s+", nxt):
                break
            paragraph_lines.append(nxt)
            i += 1
        p = doc.add_paragraph()
        add_inline(p, " ".join(paragraph_lines))

    # Set document core properties without personal metadata.
    props = doc.core_properties
    props.title = "LangGraph Multi Agent Implementation Plan"
    props.subject = "Procurement evaluation orchestration implementation blueprint"
    props.author = "Procurement Engineering"
    props.keywords = "LangGraph, LangChain, procurement, multi agent, orchestration"

    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
