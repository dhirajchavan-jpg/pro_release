"""Evidence hydration and value-grounding helpers for field results."""

from __future__ import annotations

import re
from typing import Any

from .field_core import (
    _binding_text_match,
    _clean_list,
    _compact_text,
    _contains_phrase,
    _is_placeholder_value,
    _lexical_text,
    _phrase_text,
    _table_cells,
    _tokens,
    _unit_document,
    _unit_page,
    _unit_page_id,
    _unit_text,
)
from .field_retrieval import LocalFieldRetriever
from .schema import RfpFieldSpec


def _hydrate_evidence(
    source_ids: list[str],
    page_ids: list[str],
    source_units: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    for source_id in source_ids:
        unit = source_units[source_id]
        page_id = _unit_page_id(unit)
        if page_id:
            seen_pages.add(page_id)
        result.append(
            {
                "source_id": source_id,
                "document_id": _unit_document(unit),
                "page": _unit_page(unit),
                "page_id": page_id,
                "source_role": str(unit.get("source_role") or "unknown"),
                "source_file": str(unit.get("source_file") or ""),
                "revision_sequence": unit.get("revision_sequence"),
                "document_order": unit.get("document_order"),
                "authority_rank": unit.get("authority_rank"),
                "effective_state": str(unit.get("effective_state") or "effective"),
                "superseded_by_document_id": str(
                    unit.get("superseded_by_document_id") or ""
                ),
                "section_path": [str(item) for item in unit.get("section_path") or []],
                "quote": _unit_text(unit)[:2500],
            }
        )
    for page_id in page_ids:
        if page_id not in seen_pages:
            result.append({"source_id": "", "page_id": page_id, "visual_evidence": True})
    return result


def _money_values(value: Any) -> list[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    text = str(value or "")
    text = re.sub(r"(?<=[\d,])[oO](?=\b|[\d,])", "0", text)
    scale = {
        "": 1.0,
        "lakh": 100_000.0,
        "lakhs": 100_000.0,
        "lac": 100_000.0,
        "lacs": 100_000.0,
        "crore": 10_000_000.0,
        "crores": 10_000_000.0,
        "cr": 10_000_000.0,
        "million": 1_000_000.0,
        "billion": 1_000_000_000.0,
    }
    matches = re.findall(
        r"(?:\b(?:inr|rs\.?|rupees?)\s*[:.-]?\s*|₹\s*)"
        r"([0-9][0-9,]*(?:\.\d+)?)\s*"
        r"(lakhs?|lacs?|crores?|cr|million|billion)?\b",
        text,
        re.I,
    )
    matches.extend(
        re.findall(
            r"\b([0-9][0-9,]*(?:\.\d+)?)\s*"
            r"(lakhs?|lacs?|crores?|cr|million|billion)\b",
            text,
            re.I,
        )
    )
    matches.extend(
        (number, "")
        for number in re.findall(
            r"\b([0-9][0-9,]*(?:\.\d+)?)\s*(?:inr|rs\.?|rupees?)\b",
            text,
            re.I,
        )
    )
    return list(
        dict.fromkeys(
            float(number.replace(",", "")) * scale[str(unit or "").casefold()]
            for number, unit in matches
        )
    )


def _money_value_grounded(
    value: Any,
    source_ids: list[str],
    source_units: dict[str, dict[str, Any]],
    field: RfpFieldSpec,
) -> bool:
    value_text = _phrase_text(str(value or ""))
    zero_claim = (isinstance(value, (int, float)) and value == 0) or value_text in {
        "nil",
        "zero",
        "none",
        "not applicable",
        "not required",
        "no tender fee",
        "no bid fee",
        "no fee",
    }
    if zero_claim:
        source_text = _phrase_text(
            " ".join(_unit_text(source_units[source_id]) for source_id in source_ids)
        )
        return bool(
            re.search(
                r"\bno\s+(?:(?:tender|bid|document|processing)\s+)?fee\b|"
                r"\b(?:(?:tender|bid|document|processing)\s+)?fee\s*"
                r"(?::|-|is|shall\s+be)?\s*(?:nil|zero|not\s+applicable|not\s+required)\b",
                source_text,
                re.I,
            )
        )
    claimed = _money_values(value)
    source_texts = [_unit_text(source_units[source_id]) for source_id in source_ids]
    supported = _money_values(" ".join(source_texts))
    for source_text in source_texts:
        if not _binding_text_match(_lexical_text(source_text), field):
            continue
        supported.extend(
            float(number.replace(",", ""))
            for number in re.findall(
                r"\b(?:amount|value|cost|fee|deposit|security)\b"
                r"[^0-9\n|:]{0,24}(?:\||:|=|\bis\b)\s*"
                r"([0-9][0-9,]*(?:\.\d+)?)\b",
                source_text,
                re.I,
            )
        )
    return bool(
        claimed
        and supported
        and all(
            any(abs(candidate - expected) <= max(1.0, abs(expected) * 1e-6) for candidate in supported)
            for expected in claimed
        )
    )


def _date_values(value: Any) -> list[tuple[int, int, int, str]]:
    text = _compact_text(value)
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    values: list[tuple[int, int, int, str]] = []

    def add(day: str, month: str, year: str, time_value: str = "") -> None:
        numeric_year = int(year)
        if numeric_year < 100:
            numeric_year += 2000
        token = (numeric_year, int(month), int(day), time_value.replace(".", ":"))
        if token not in values:
            values.append(token)

    time_suffix = r"(?:\s*(?:at|@)?\s*(\d{1,2}(?::|\.)\d{2})\s*(?:hrs?|hours?)?)?"
    for match in re.finditer(
        rf"\b(\d{{1,2}})[./-](\d{{1,2}})[./-](\d{{2,4}}){time_suffix}",
        text,
        re.I,
    ):
        add(match.group(1), match.group(2), match.group(3), match.group(4) or "")
    for match in re.finditer(
        rf"\b(\d{{4}})-(\d{{1,2}})-(\d{{1,2}}){time_suffix}",
        text,
        re.I,
    ):
        add(match.group(3), match.group(2), match.group(1), match.group(4) or "")
    month_pattern = "|".join(
        (
            "jan(?:uary)?",
            "feb(?:ruary)?",
            "mar(?:ch)?",
            "apr(?:il)?",
            "may",
            "jun(?:e)?",
            "jul(?:y)?",
            "aug(?:ust)?",
            "sep(?:tember)?",
            "oct(?:ober)?",
            "nov(?:ember)?",
            "dec(?:ember)?",
        )
    )
    for match in re.finditer(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{2,4}}){time_suffix}",
        text,
        re.I,
    ):
        add(
            match.group(1),
            str(months[match.group(2)[:3].casefold()]),
            match.group(3),
            match.group(4) or "",
        )
    for match in re.finditer(
        rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{2,4}}){time_suffix}",
        text,
        re.I,
    ):
        add(
            match.group(2),
            str(months[match.group(1)[:3].casefold()]),
            match.group(3),
            match.group(4) or "",
        )
    return values


def _date_value_grounded(
    value: Any,
    source_ids: list[str],
    source_units: dict[str, dict[str, Any]],
) -> bool:
    claimed = _date_values(value)
    supported = _date_values(
        " ".join(_unit_text(source_units[source_id]) for source_id in source_ids)
    )
    return bool(
        claimed
        and supported
        and all(
            any(
                candidate[:3] == expected[:3]
                and (not expected[3] or candidate[3] == expected[3])
                for candidate in supported
            )
            for expected in claimed
        )
    )


def _explicit_not_applicable_value(
    field: RfpFieldSpec,
    source_ids: list[str],
    source_units: dict[str, dict[str, Any]],
) -> bool:
    phrases = [field.label, *field.retrieval_guidance.aliases]
    fee_field = "fee" in _phrase_text(f"{field.key} {field.label}")
    for source_id in source_ids:
        text = _phrase_text(_unit_text(source_units[source_id]))
        for phrase in phrases:
            normalized = _phrase_text(phrase)
            phrase_pattern = re.escape(normalized)
            if normalized and normalized[-1].isalpha() and not normalized.endswith("s"):
                phrase_pattern += "s?"
            if normalized and re.search(
                rf"\b{phrase_pattern}\b.{{0,48}}\b(?:n ?a|not applicable|nil|not required)\b",
                text,
            ):
                return True
            if (
                fee_field
                and normalized
                and re.search(
                    rf"\basking for any\b.{{0,80}}\b{re.escape(normalized)}\b",
                    text,
                )
            ):
                return True
    return False


def _has_explicit_labeled_value(
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
) -> bool:
    phrases = [field.label, *field.retrieval_guidance.aliases]
    for item in evidence:
        if item.get("source_role") != "base_rfp" or item.get("effective_state") == "superseded":
            continue
        cells = _table_cells(item)
        if len(cells) < 2:
            continue
        label_indexes = [
            index
            for index, cell in enumerate(cells)
            if any(
                _contains_phrase(_phrase_text(cell), phrase)
                for phrase in phrases
                if _phrase_text(phrase)
            )
        ]
        for label_index in label_indexes:
            for candidate in cells[label_index + 1 :]:
                candidate = _compact_text(candidate)
                if (
                    candidate
                    and not _is_placeholder_value(candidate)
                    and not re.fullmatch(r"\d+[.)]?", candidate)
                    and len(_tokens(candidate)) >= 2
                ):
                    return True
    return False


def _request_details(
    raw: dict[str, Any],
    retriever: LocalFieldRetriever,
    evidence: list[dict[str, Any]],
    max_images: int,
) -> tuple[str, list[str], list[str], int, str]:
    request = raw.get("evidence_request") if isinstance(raw.get("evidence_request"), dict) else {}
    modality = str(request.get("modality") or "more_text")
    query = str(request.get("query") or request.get("reason") or "")[:1000]
    source_ids = [
        source_id
        for source_id in _clean_list(request.get("source_ids"), limit=max_images * 4)
        if source_id in retriever.units
    ]
    page_ids = [
        page_id
        for page_id in _clean_list(request.get("page_ids"), limit=max_images)
        if page_id in retriever.page_ids
    ]
    for source_id in source_ids:
        page_id = _unit_page_id(retriever.units[source_id])
        if page_id and page_id not in page_ids:
            page_ids.append(page_id)
        if len(page_ids) >= max_images:
            break
    if not page_ids:
        page_ids = [
            str(item.get("page_id") or "")
            for item in evidence
            if item.get("page_id")
        ][:max_images]
    try:
        adjacent = max(0, min(2, int(request.get("include_adjacent_pages") or 0)))
    except (TypeError, ValueError):
        adjacent = 0
    if adjacent:
        page_ids = retriever.adjacent_page_ids(page_ids, adjacent)[:max_images]
    reason = str(request.get("reason") or "")[:500]
    return modality, source_ids, page_ids, adjacent, query or reason


__all__ = [
    "_hydrate_evidence",
    "_money_values",
    "_money_value_grounded",
    "_date_values",
    "_date_value_grounded",
    "_explicit_not_applicable_value",
    "_has_explicit_labeled_value",
    "_request_details",
]
