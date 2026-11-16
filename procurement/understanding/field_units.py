"""Structural accessors for immutable tender source units."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any

from .lexical import _compact_text, _phrase_text, _tokens


_REFERENCE_RE = re.compile(
    r"\b(format|annexure|appendix|schedule|clause|section)\s*"
    r"(?:no\.?\s*)?([a-z0-9]+(?:\.[a-z0-9]+){0,4})\b",
    re.IGNORECASE,
)

_CLAUSE_LOCATOR_RE = re.compile(
    r"\b(\d+(?:\.\d+)+)\s*(?:\(\s*([a-z0-9ivxlcdm]+)\s*\)|([a-z]))?\b",
    re.IGNORECASE,
)

_CLAUSE_POINT_RE = re.compile(
    r"\b(\d+(?:\.\d+)+)\b.{0,100}?\b(?:point|item|row|criterion)\s*"
    r"(?:no\.?\s*)?(\d+|[a-z])\b",
    re.IGNORECASE,
)

_SECTION_SCOPE_RE = re.compile(
    r"^\s*(?:(?:section|clause|article)\s*)?(\d+(?:\.\d+)+)\b",
    re.IGNORECASE,
)

_INLINE_SECTION_SCOPE_RE = re.compile(
    r"(?:^|\b(?:section|clause|article)\s*(?:no\.?\s*)?)(\d+(?:\.\d+)+)\b",
    re.IGNORECASE,
)


def _is_formal_revision_row(
    unit: dict[str, Any],
    formal_revision_sections: set[str],
) -> bool:
    cells = _table_cells(unit)
    headers = [_phrase_text(item) for item in unit.get("table_headers") or []]
    has_existing = any("existing" in header or "current clause" in header for header in headers)
    has_revised = any(
        "revised" in header or "modified clause" in header or "amended clause" in header
        for header in headers
    )
    if unit.get("source_role") != "post_base_update":
        return False
    if unit.get("unit_type") == "table_header":
        return _implicit_parallel_revision_cells(unit) is not None
    return bool(
        unit.get("unit_type") == "table_row"
        and (
            3 <= len(cells) <= 5
            or (has_existing and has_revised and _formal_revision_cells(unit) is not None)
        )
        and (
            (has_existing and has_revised)
            or str(unit.get("section_id") or "") in formal_revision_sections
        )
    )


def _clean_list(value: Any, *, limit: int = 50) -> list[str]:
    if not isinstance(value, list) or limit <= 0:
        return []
    result: list[str] = []
    for item in value:
        text = _compact_text(item)
        if text and text not in result:
            result.append(text[:500])
        if len(result) >= limit:
            break
    return result


def _unit_text(unit: dict[str, Any]) -> str:
    return str(unit.get("text") or "").strip()


def _unit_page(unit: dict[str, Any]) -> int:
    try:
        return int(unit.get("page") or unit.get("page_number") or 0)
    except (TypeError, ValueError):
        return 0


def _unit_page_id(unit: dict[str, Any]) -> str:
    return str(unit.get("page_id") or "")


def _unit_document(unit: dict[str, Any]) -> str:
    return str(unit.get("document") or unit.get("document_id") or "")


def _table_cells(unit: dict[str, Any]) -> list[str]:
    if str(unit.get("unit_type") or "") not in {"table_row", "table_header"}:
        return []
    return [part.strip() for part in _unit_text(unit).split(" | ") if part.strip()]


def _table_header_index(headers: list[str], *needles: str) -> int | None:
    normalized = [_phrase_text(header) for header in headers]
    return next(
        (
            index
            for index, header in enumerate(normalized)
            if any(needle in header for needle in needles)
        ),
        None,
    )


def _issuer_response_text(unit: dict[str, Any]) -> str:
    existing = _compact_text(
        unit.get("issuer_response") or unit.get("issuer_response_or_revised_text")
    )
    if existing:
        return existing
    headers = [str(value) for value in unit.get("table_headers") or []]
    response_index = _table_header_index(
        headers, "issuer response", "bank response", "client response", "response", "amendment"
    )
    cells = _table_cells(unit)
    if response_index is None or not cells:
        return ""
    response = cells[response_index] if response_index < len(cells) else cells[-1]
    return _compact_text(
        " ".join(filter(None, [response, str(unit.get("table_continuation_text") or "")]))
    )


def _merge_table_row_continuations(
    parent: dict[str, Any],
    continuations: list[dict[str, Any]],
) -> str:
    headers = [str(value) for value in parent.get("table_headers") or []]
    parent_cells = _table_cells(parent)
    if not headers or len(parent_cells) != len(headers):
        return _compact_text(
            " ".join([_unit_text(parent), *[_unit_text(item) for item in continuations]])
        )
    merged = list(parent_cells)
    requirement_index = _table_header_index(
        headers, "requirement", "criterion", "criteria", "description", "particulars"
    )
    proof_index = _table_header_index(
        headers, "compliance", "evidence", "proof", "document", "submission"
    )
    for continuation in continuations:
        cells = _table_cells(continuation)
        if not cells:
            continue
        if len(cells) == 1 and requirement_index is not None:
            target_indexes = [requirement_index]
        elif (
            len(cells) == 2
            and requirement_index is not None
            and proof_index is not None
            and requirement_index != proof_index
        ):
            target_indexes = [requirement_index, proof_index]
        else:
            start = max(0, len(headers) - len(cells))
            target_indexes = list(range(start, min(len(headers), start + len(cells))))
        for target_index, cell in zip(target_indexes, cells):
            separator = "; " if target_index == proof_index else " "
            merged[target_index] = _compact_text(
                f"{merged[target_index]}{separator}{cell}"
            )
    return " | ".join(merged)


def _implicit_parallel_revision_cells(unit: dict[str, Any]) -> tuple[str, str] | None:
    if (
        unit.get("source_role") != "post_base_update"
        or unit.get("unit_type") not in {"table_header", "table_row"}
    ):
        return None
    cells = _table_cells(unit) if "table_cells" not in unit else list(unit.get("table_cells") or [])
    if not 4 <= len(cells) <= 12:
        return None
    repeated_pairs: list[tuple[int, float, int, int]] = []
    for old_index, old_cell in enumerate(cells[:-2]):
        old_normalized = _phrase_text(str(old_cell))
        if len(old_normalized) < 12 or len(_tokens(old_normalized)) < 2:
            continue
        for revised_index in range(old_index + 2, len(cells) - 1):
            revised_normalized = _phrase_text(str(cells[revised_index]))
            similarity = SequenceMatcher(
                None, old_normalized, revised_normalized
            ).ratio()
            if old_normalized != revised_normalized and similarity < 0.88:
                continue
            width = min(revised_index - old_index, len(cells) - revised_index)
            if width >= 2:
                repeated_pairs.append(
                    (width, similarity, old_index, revised_index)
                )
    if repeated_pairs:
        width, _similarity, old_index, revised_index = max(repeated_pairs)
        old_cells = cells[old_index : old_index + width]
        revised_cells = cells[revised_index : revised_index + width]
        repeated_anchor_layout = True
    elif len(cells) % 2 == 0:
        width = len(cells) // 2
        old_cells = cells[:width]
        revised_cells = cells[width:]
        repeated_anchor_layout = False
    else:
        return None
    if unit.get("unit_type") == "table_row" and not repeated_anchor_layout:
        old_serial = re.sub(r"[^0-9a-z]", "", _compact_text(cells[0]).casefold())
        revised_serial = re.sub(
            r"[^0-9a-z]", "", _compact_text(cells[width]).casefold()
        )
        if old_serial == revised_serial and re.fullmatch(r"\d+[a-z]?", old_serial, re.I):
            return None
    previous = _compact_text(" | ".join(str(cell) for cell in old_cells))
    revised = _compact_text(" | ".join(str(cell) for cell in revised_cells))
    if not previous or not revised or _phrase_text(previous) == _phrase_text(revised):
        return None
    old_tokens, revised_tokens = set(_tokens(previous)), set(_tokens(revised))
    if not old_tokens or not revised_tokens:
        return None
    total_chars = len(previous) + len(revised)
    headers = [_phrase_text(str(item)) for item in unit.get("table_headers") or []]
    labelled_revision_row = unit.get("unit_type") == "table_row" and any(
        "existing" in header for header in headers
    ) and any("revised" in header or "amended" in header for header in headers)
    if total_chars < (60 if labelled_revision_row else 150):
        return None
    containment = len(old_tokens & revised_tokens) / min(len(old_tokens), len(revised_tokens))
    pair_matches = 0
    for old_cell, revised_cell in zip(old_cells, revised_cells):
        old_cell_tokens = set(_tokens(str(old_cell)))
        revised_cell_tokens = set(_tokens(str(revised_cell)))
        if (
            old_cell_tokens
            and revised_cell_tokens
            and len(old_cell_tokens & revised_cell_tokens)
            / min(len(old_cell_tokens), len(revised_cell_tokens))
            >= 0.25
        ):
            pair_matches += 1
    minimum_containment = (
        0.55
        if labelled_revision_row or repeated_anchor_layout
        else 0.8
        if total_chars < 500
        else 0.55
    )
    if containment < minimum_containment or pair_matches < max(2, width - 1):
        return None
    return previous, revised


def _formal_revision_cells(unit: dict[str, Any]) -> tuple[str, str] | None:
    explicit_existing = _compact_text(unit.get("existing_text"))
    explicit_revised = _compact_text(unit.get("revised_text"))
    if explicit_existing and explicit_revised:
        return explicit_existing, explicit_revised
    cells = _table_cells(unit) if "table_cells" not in unit else list(unit.get("table_cells") or [])
    normalized_cells = {_phrase_text(str(cell)) for cell in cells}
    if "existing clause" in normalized_cells and "revised clause" in normalized_cells:
        return None
    headers = [str(item) for item in unit.get("table_headers") or []]
    existing_index = _table_header_index(headers, "existing", "current clause")
    revised_index = _table_header_index(headers, "revised", "modified clause", "amended clause")
    implicit_revision = _implicit_parallel_revision_cells(unit)
    if implicit_revision:
        return implicit_revision
    if (
        existing_index is not None
        and revised_index is not None
        and len(cells) >= 4
        and len(cells) % 2 == 0
    ):
        half = len(cells) // 2
        old_serial = re.sub(r"[^0-9a-z]", "", _compact_text(cells[0]).casefold())
        revised_serial = re.sub(
            r"[^0-9a-z]", "", _compact_text(cells[half]).casefold()
        )
        if (
            old_serial
            and old_serial == revised_serial
            and re.fullmatch(r"\d+[a-z]?", old_serial, re.I)
        ):
            return (
                _compact_text(" | ".join(str(cell) for cell in cells[:half])),
                _compact_text(" | ".join(str(cell) for cell in cells[half:])),
            )
    serial_index = _table_header_index(headers, "s no", "sl no", "serial no")
    page_index = _table_header_index(headers, "page")
    if serial_index is not None and serial_index < len(cells):
        if not re.fullmatch(r"\d+[a-z]?(?:\.\d+)*[.)]?", _compact_text(cells[serial_index]), re.I):
            return None
    if page_index is not None and page_index < len(cells):
        if not re.fullmatch(
            r"\d+(?:\s*[-/,]\s*\d+)*[a-z]?",
            _compact_text(cells[page_index]),
            re.I,
        ):
            return None
    if (
        len(cells) == len(headers)
        and existing_index is not None
        and revised_index is not None
        and existing_index < len(cells)
        and revised_index < len(cells)
    ):
        previous, revised = cells[existing_index], cells[revised_index]
        if _phrase_text(previous) in {"existing", "existing clause", "current clause"}:
            return None
        if _phrase_text(revised) in {"revised", "revised clause", "modified clause"}:
            return None
        return previous, revised
    if (
        unit.get("formal_revision") is True
        and len(cells) >= 3
        and existing_index is None
        and revised_index is None
    ):
        return cells[-2], cells[-1]
    return None


def _paired_nested_revision_cells(
    unit: dict[str, Any],
    continuations: list[dict[str, Any]],
) -> tuple[str, str, list[str]] | None:
    headers = [str(item) for item in unit.get("table_headers") or []]
    cells = _table_cells(unit)
    existing_index = _table_header_index(headers, "existing", "current clause")
    revised_index = _table_header_index(
        headers, "revised", "modified clause", "amended clause"
    )
    if existing_index is not None and revised_index is not None:
        nested_headers = cells[existing_index + 1 :]
        nested_header_text = {_phrase_text(cell) for cell in nested_headers}
        looks_like_nested_headers = (
            len(nested_headers) >= 2
            and any("eligibility" in value or "parameter" in value for value in nested_header_text)
            and any(
                "requirement" in value or "criteria" in value
                for value in nested_header_text
            )
        )
        revised_row_index = next(
            (
                index
                for index, continuation in enumerate(continuations)
                if len(_table_cells(continuation)) == len(nested_headers)
            ),
            None,
        )
        if looks_like_nested_headers and revised_row_index is not None:
            revised_unit = continuations[revised_row_index]
            revised_cells = _table_cells(revised_unit)
            source_ids = [str(revised_unit.get("source_id") or "")]
            for continuation in continuations[revised_row_index + 1 :]:
                continuation_cells = _table_cells(continuation)
                if len(continuation_cells) != 1:
                    break
                revised_cells[-1] = _compact_text(
                    f"{revised_cells[-1]} {continuation_cells[0]}"
                )
                source_ids.append(str(continuation.get("source_id") or ""))
            return (
                _compact_text(cells[existing_index]),
                _compact_text(" | ".join(revised_cells)),
                [source_id for source_id in source_ids if source_id],
            )
    if (
        existing_index is None
        or revised_index is None
        or len(cells) <= len(headers)
        or (len(cells) - existing_index) % 2
    ):
        return None
    width = (len(cells) - existing_index) // 2
    if width < 2:
        return None
    old_headers = [_phrase_text(cell) for cell in cells[existing_index : existing_index + width]]
    revised_headers = [_phrase_text(cell) for cell in cells[existing_index + width :]]
    if old_headers != revised_headers:
        return None
    rows = [
        (continuation, _table_cells(continuation))
        for continuation in continuations
        if len(_table_cells(continuation)) == width
    ]
    if len(rows) < 2:
        return None
    old_unit, old_cells = rows[0]
    revised_unit, revised_cells = rows[1]
    old_serial = re.sub(r"[^0-9a-z]", "", _compact_text(old_cells[0]).casefold())
    revised_serial = re.sub(
        r"[^0-9a-z]", "", _compact_text(revised_cells[0]).casefold()
    )
    if not old_serial or old_serial != revised_serial:
        return None
    source_ids = [
        str(old_unit.get("source_id") or ""),
        str(revised_unit.get("source_id") or ""),
    ]
    for continuation, continuation_cells in rows[2:]:
        continuation_serial = re.sub(
            r"[^0-9a-z]", "", _compact_text(continuation_cells[0]).casefold()
        )
        if continuation_serial == old_serial or width % 2:
            break
        tail_width = width // 2
        for index in range(tail_width):
            old_cells[-tail_width + index] = _compact_text(
                f"{old_cells[-tail_width + index]} {continuation_cells[index]}"
            )
            revised_cells[-tail_width + index] = _compact_text(
                f"{revised_cells[-tail_width + index]} {continuation_cells[tail_width + index]}"
            )
        source_ids.append(str(continuation.get("source_id") or ""))
    return (
        _compact_text(" | ".join(old_cells)),
        _compact_text(" | ".join(revised_cells)),
        [source_id for source_id in source_ids if source_id],
    )


def _section_scope_key(unit: dict[str, Any]) -> str:
    cells = _table_cells(unit)
    probes = [str(unit.get("clause_number") or ""), *(cells[:2] if cells else [])]
    for probe in probes:
        if match := _INLINE_SECTION_SCOPE_RE.search(probe):
            return match.group(1)
    for part in reversed([str(item) for item in unit.get("section_path") or []]):
        if match := _SECTION_SCOPE_RE.search(part):
            return match.group(1)
    return ""


def _is_explicit_full_replacement(unit: dict[str, Any]) -> bool:
    own_text = _phrase_text(_unit_text(unit))
    return any(
        cue in own_text
        for cue in (
            "entire section",
            "in its entirety",
            "section is replaced",
            "section shall be replaced",
            "section stands substituted",
            "section shall stand substituted",
            "revised section",
        )
    )


def _is_replacement_scope(unit: dict[str, Any]) -> bool:
    if unit.get("source_role") != "post_base_update" or not _section_scope_key(unit):
        return False
    own_text = _phrase_text(_unit_text(unit))
    unit_type = str(unit.get("unit_type") or "").casefold()
    heading_replacement = unit_type in {
        "heading",
        "header",
        "section_heading",
        "title",
    } and any(cue in own_text for cue in ("revised", "replacement", "substituted"))
    return heading_replacement or _is_explicit_full_replacement(unit)


def _scope_contains(parent: str, child: str) -> bool:
    return bool(parent and child and (child == parent or child.startswith(f"{parent}.")))


def _clause_locator_keys(value: str) -> set[str]:
    result: set[str] = set()
    for match in _CLAUSE_LOCATOR_RE.finditer(value):
        base = match.group(1).casefold()
        suffix = (match.group(2) or match.group(3) or "").casefold()
        result.add(f"{base}.{suffix}" if suffix else base)
    for match in _CLAUSE_POINT_RE.finditer(value):
        base = match.group(1).casefold()
        result.discard(base)
        result.add(f"{base}.{match.group(2).casefold()}")
    return result


def _unit_clause_locator_keys(unit: dict[str, Any]) -> set[str]:
    scope = str(unit.get("section_scope") or _section_scope_key(unit) or "")
    clause = re.sub(
        r"[^a-z0-9.]",
        "",
        _phrase_text(str(unit.get("clause_number") or "")),
    )
    if not clause:
        cells = _table_cells(unit)
        row_marker = re.sub(
            r"[^a-z0-9]",
            "",
            _phrase_text(cells[0] if cells else ""),
        )
        if (
            row_marker not in {"", "s", "sl", "no", "num", "number"}
            and re.fullmatch(r"\d+|[a-z]|[ivxlcdm]+", row_marker)
        ):
            clause = row_marker
    direct = _clause_locator_keys(str(unit.get("clause_number") or ""))
    if scope and clause and clause != scope and re.fullmatch(r"[a-z0-9ivxlcdm]+", clause):
        direct.add(f"{scope}.{clause}")
    elif scope and not direct:
        direct.add(scope)
    return direct


def _reference_keys(value: str) -> list[str]:
    return [
        f"{kind.casefold()} {identifier.casefold()}"
        for kind, identifier in _REFERENCE_RE.findall(value)
    ]


def _unit_reference_keys(unit: dict[str, Any]) -> list[str]:
    values = [
        _unit_text(unit),
        *[str(item) for item in unit.get("section_path") or []],
    ]
    return list(dict.fromkeys(key for value in values for key in _reference_keys(value)))[:20]
