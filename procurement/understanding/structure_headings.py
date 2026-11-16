"""Heading and numbering heuristics for document-structure detection."""

from __future__ import annotations

import re

from procurement.domain.schemas import LayoutBlock, PageUnit, StructuralSection
from procurement.domain.text import clean_text, normalize_for_match


STRUCTURAL_HEADING_PREFIX = re.compile(
    r"^((?i:part|section|chapter|annexure|appendix|schedule|format))\b\s*[-:]?\s*"
    r"((?:\d+(?:\.\d+)*)|[IVXLCDM]+|[A-Z])?\s*(?:[.):-]\s*)?(.*)$",
)
STRUCTURAL_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:[.)])?\s+(.+)$")
GENERIC_SECTION_TITLE = re.compile(
    r"^(?:request for proposal|notice inviting tender|invitation to bid|bid data sheet|"
    r"instructions? to bidders?|definitions?|scope(?: of work)?|eligibility(?: criteria)?|"
    r"pre[- ]qualification(?: criteria)?|qualification(?: criteria|requirements?)|"
    r"technical(?: bid| evaluation| specifications?)|commercial(?: bid| evaluation)|"
    r"financial(?: bid| evaluation)|evaluation(?: criteria| methodology)?|"
    r"general conditions?|special conditions?|payment terms?|service levels?|"
    r"submission(?: instructions?| checklist)?|important dates?|annexures?)\b",
    re.IGNORECASE,
)


def structural_number_level(number: str) -> int:
    parts = number.split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return max(1, len(parts))


def structural_number_is_date(number: str) -> bool:
    parts = number.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return False
    day, month, year = (int(part) for part in parts)
    return 1 <= day <= 31 and 1 <= month <= 12 and len(parts[2]) == 4 and 1900 <= year <= 2200


def structural_number_is_time(number: str, title: str = "") -> bool:
    if not re.fullmatch(r"\d{1,2}\.\d{2}", number):
        return False
    hour, minute = (int(part) for part in number.split("."))
    suffix = clean_text(title).lower().strip(" .:-")
    return 0 <= hour <= 23 and 0 <= minute <= 59 and bool(
        re.match(r"^(?:a\.?m\.?|p\.?m\.?|hrs?|hours?)\b", suffix)
    )


def structural_title_is_clause_like(title: str) -> bool:
    cleaned = clean_text(title)
    letters = [character for character in cleaned if character.isalpha()]
    uppercase_title = bool(letters) and sum(character.isupper() for character in letters) / len(letters) >= 0.75
    if re.search(r"[.;]$", cleaned):
        return True
    if re.search(r"^(?:in this regard|in this case|in case|for this purpose|for the purpose)\b|\b(?:i\.e\.|e\.g\.)", cleaned, re.IGNORECASE):
        return True
    if re.search(r"\b(?:hereinafter referred|need not be|lays down|quoted)\b", cleaned, re.IGNORECASE):
        return True
    if re.match(r"^(?:if|that\s+if|whereas)\b", cleaned, re.IGNORECASE):
        return True
    if re.match(
        r"^the\s+(?:authority|bidder(?:/(?:contractor|contactor))?|contractor|bank|purchaser|vendor)"
        r"(?:\s+further)?(?:,|\s+(?:undertakes?|agrees?|confirms?|declares?|commits?)\b)",
        cleaned,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:shall|will|must|should|may|can|could|would|hereby|has|have|is|are|was|were)\b",
        cleaned,
        re.IGNORECASE,
    ):
        return True
    return len(cleaned) > 100 and not uppercase_title


def page_is_table_of_contents(page: PageUnit) -> bool:
    texts = [clean_text(block.text) for block in page.blocks if block.text.strip()]
    if any(normalize_for_match(text) in {"table of contents", "contents", "index"} for text in texts[:12]):
        return True
    navigation_rows = sum(
        bool(re.search(r"(?:\.{3,}|-{5,})\s*\d+\s*$", text))
        for text in texts
    )
    return navigation_rows >= 3


def block_is_inline_list_reference(
    block: LayoutBlock,
    previous: LayoutBlock | None,
    following: LayoutBlock | None,
) -> bool:
    if previous is None or following is None or len(block.text) > 60:
        return False
    if len(block.bbox) != 4 or len(previous.bbox) != 4 or len(following.bbox) != 4:
        return False
    previous_number = clause_number_from_text(previous.text)
    following_number = clause_number_from_text(following.text)
    if not previous_number.isdigit() or not following_number.isdigit():
        return False
    if int(following_number) != int(previous_number) + 1:
        return False
    list_margin = (previous.bbox[0] + following.bbox[0]) / 2
    return abs(previous.bbox[0] - following.bbox[0]) <= 0.03 and block.bbox[0] >= list_margin + 0.015


def sequential_list_block_ids(page: PageUnit) -> set[str]:
    def roman_value(value: str) -> int:
        total = 0
        previous = 0
        for character in reversed(value):
            current = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}.get(character, 0)
            total += -current if current < previous else current
            previous = max(previous, current)
        return total

    candidates: list[tuple[LayoutBlock, str, int]] = []
    for block in sorted(page.blocks, key=lambda item: item.reading_order):
        number = clause_number_from_text(block.text)
        if number.isdigit() and len(block.bbox) == 4:
            candidates.append((block, "number", int(number)))
        else:
            prefixed = re.match(
                r"^(section|annexure|appendix|schedule|format)\s*[-:]?\s*([IVXLCDM]+|\d+|[A-Z])\b",
                block.text,
                re.IGNORECASE,
            )
            if prefixed and len(block.bbox) == 4:
                prefix, marker = prefixed.groups()
                marker = marker.upper()
                value = int(marker) if marker.isdigit() else roman_value(marker) if re.fullmatch(r"[IVXLCDM]+", marker) else ord(marker) - 64
                candidates.append((block, prefix.lower(), value))
            else:
                candidates.append((block, "", -1))
    list_ids: set[str] = set()
    run: list[tuple[LayoutBlock, str, int]] = []

    def flush() -> None:
        if len(run) >= 3:
            list_ids.update(block.block_id for block, _group, _number in run)

    for block, group, number in candidates:
        if (
            number >= 0
            and (
                not run
                or (
                    group == run[-1][1]
                    and number == run[-1][2] + 1
                    and abs(block.bbox[0] - run[-1][0].bbox[0]) <= 0.03
                )
            )
        ):
            run.append((block, group, number))
            continue
        flush()
        run = [(block, group, number)] if number >= 0 else []
    flush()
    return list_ids


def structural_heading_parts(
    text: str,
    *,
    font_size: float = 0.0,
    body_size: float = 0.0,
    is_bold: bool = False,
) -> tuple[str, str, int, float] | None:
    cleaned = clean_text(text).strip(" :-")
    if not cleaned or len(cleaned) > 220:
        return None
    visual = is_bold or (font_size > 0 and body_size > 0 and font_size >= body_size * 1.08)
    prefixed = STRUCTURAL_HEADING_PREFIX.match(cleaned)
    if prefixed:
        prefix, number, title = prefixed.groups()
        combined = f"{number or ''}{title}"
        if number and number.isalpha() and (
            (title and title[0].islower())
            or re.match(r"^(?:FOR|OF|TO|BY|AND)\b", combined, re.IGNORECASE)
        ):
            title = combined
            number = ""
        roman_continuation = re.match(r"^([IVXLCDM])\b\s*(.*)$", title) if number and re.fullmatch(r"[IVXLCDM]+", number) else None
        if roman_continuation:
            number = f"{number}{roman_continuation.group(1)}"
            title = roman_continuation.group(2).strip()
            suffix = re.match(r"^[-\u2013]\s*([A-Z0-9]+)\b\s*(.*)$", title)
            if suffix:
                number = f"{number}-{suffix.group(1)}"
                title = suffix.group(2).strip()
        if prefix.islower() and not visual:
            return None
        if number and number.isalpha() and number != number.upper():
            title = f"{number}{title}"
            number = ""
        if title and structural_title_is_clause_like(title):
            return None
        if prefix.lower() == "format" and not visual and cleaned.upper() != cleaned:
            return None
        if not visual and ((not number and not title) or len(title.split()) > 8):
            return None
        display_number = " ".join(part for part in (prefix.upper(), number or "") if part)
        display_title = title.strip(" :-")
        level = 1 if prefix.lower() in {"part", "chapter", "annexure", "appendix"} else max(1, (number or "").count(".") + 1)
        return display_number, display_title, level, 0.98
    numbered = STRUCTURAL_NUMBER.match(cleaned)
    if numbered:
        number, title = numbered.groups()
        if structural_number_is_date(number) or structural_number_is_time(number, title):
            return None
        level = structural_number_level(number)
        sentence_like = structural_title_is_clause_like(title)
        title_letters = [character for character in title if character.isalpha()]
        if not title_letters:
            return None
        uppercase_title = bool(title_letters) and sum(character.isupper() for character in title_letters) / len(title_letters) >= 0.75
        short_title = len(title.split()) <= 14 and len(title) <= 100
        if (
            (visual and not sentence_like)
            or (level >= 2 and short_title and not sentence_like)
            or (level == 1 and GENERIC_SECTION_TITLE.match(title))
            or (number.endswith(".0") and uppercase_title and not sentence_like)
        ):
            return number, title.strip(" :-"), level, 0.94 if visual else 0.82
        return None
    if GENERIC_SECTION_TITLE.match(cleaned) and not cleaned[0].islower() and not structural_title_is_clause_like(cleaned):
        return "", cleaned, 1, 0.86
    letters = [character for character in cleaned if character.isalpha()]
    upper_ratio = sum(character.isupper() for character in letters) / max(1, len(letters))
    visually_distinct = is_bold and font_size > 0 and body_size > 0 and font_size >= body_size * 1.12
    if (
        visually_distinct
        and cleaned.upper() not in {"AND", "OR", "FOR", "OF", "TO", "BETWEEN"}
        and len(cleaned) <= 140
        and upper_ratio >= 0.55
        and not structural_title_is_clause_like(cleaned)
    ):
        return "", cleaned, 1, 0.72
    return None


def potential_structural_line(text: str) -> bool:
    """Keep plausible headings physically separate until document-level review."""
    cleaned = clean_text(text).strip(" :-")
    if not cleaned or len(cleaned) > 220 or structural_title_is_clause_like(cleaned):
        return False
    numbered = STRUCTURAL_NUMBER.match(cleaned)
    if numbered and not structural_number_is_date(numbered.group(1)) and not structural_number_is_time(*numbered.groups()):
        return True
    if STRUCTURAL_HEADING_PREFIX.match(cleaned) or GENERIC_SECTION_TITLE.match(cleaned):
        return True
    if re.match(r"^[A-Z][.)]\s+\S", cleaned):
        return True
    letters = [character for character in cleaned if character.isalpha()]
    return 1 < len(cleaned.split()) <= 14 and bool(letters) and sum(character.isupper() for character in letters) / len(letters) >= 0.75


def clause_number_from_text(text: str) -> str:
    cleaned = clean_text(text)
    match = re.match(r"^((?:\d+(?:\.\d+)*)|[a-z]|[ivxlcdm]+)(?:[.)])\s+", cleaned, re.IGNORECASE)
    if match:
        return match.group(1)
    decimal = STRUCTURAL_NUMBER.match(cleaned)
    return (
        decimal.group(1)
        if decimal
        and not structural_number_is_date(decimal.group(1))
        and not structural_number_is_time(decimal.group(1), decimal.group(2))
        else ""
    )


def section_display(number: str, title: str) -> str:
    return clean_text(" ".join(part for part in (number, title) if part))


def section_is_numbering_container(section: StructuralSection) -> bool:
    return text_is_numbering_container(section.number, section.title)


def text_is_numbering_container(number: str, title: str) -> bool:
    probe = section_display(number, title)
    return bool(
        re.match(r"^(?:annexures?|appendix|schedule|format|part|section)\b", probe, re.IGNORECASE)
        or re.match(r"^(?:annexures?|appendix|schedule|part|section)\b", title, re.IGNORECASE)
        or re.match(r"^(?:general|special|particular) conditions?\b", title, re.IGNORECASE)
    )


__all__ = [
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
    "section_display",
    "section_is_numbering_container",
    "text_is_numbering_container",
]
