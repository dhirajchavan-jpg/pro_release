"""Field matching and deterministic value interpretation."""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
import re
from typing import Any

from .lexical import (
    _STOP_WORDS,
    _WORD_RE,
    _compact_text,
    _contains_phrase,
    _lexical_text,
    _phrase_text,
    _stem,
    _tokens,
)
from .schema import RfpFieldSpec, RfpSectionSpec
from .field_units import _table_cells, _unit_text


_PROOF_QUERY = (
    "supporting document certificate format form undertaking declaration proof "
    "purchase order letter affidavit audited statement envelope pan gst acceptance"
)

_GATE_QUERY = "minimum qualifying score marks threshold technical evaluation pass gate"

_DATE_VALUE_RE = re.compile(
    r"\b(?:"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
    r"\d{4}-\d{1,2}-\d{1,2}|"
    r"\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+\d{2,4}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2},?\s+\d{2,4}"
    r")\b",
    re.IGNORECASE,
)

_DOCUMENT_CUES = (
    "shall submit",
    "must submit",
    "to be submitted",
    "required document",
    "supporting document",
    "certificate",
    "undertaking",
    "declaration",
    "affidavit",
    "purchase order",
    "work order",
    "completion certificate",
    "client acknowledgement",
    "go-live certificate",
    "statement of work",
    "master service agreement",
    "softex",
    "audited statement",
    "balance sheet",
    "reference letter",
    "bank guarantee",
    "bank draft",
    "demand draft",
    "presentation",
    "walkthrough",
    "test logs",
    "pre-recorded demo",
    "test summaries",
    "prior assignment",
    "case study",
    "anonymized data",
    "anonymised data",
    "envelope",
    "technical bid",
    "financial bid",
    "acceptance letter",
    "blacklisting",
    "debarment",
    "pan",
    "gst",
    "form",
)

_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_EXCEPTION_CUES = (
    "exempt",
    "relaxation",
    "unless",
    "provided that",
    "however",
    "in case of",
    "subject to",
    "stands deleted",
    "replaced by",
    "instead of",
)

_CONDITION_CUES = (
    "minimum",
    "at least",
    "within",
    "valid for",
    "only if",
    "provided that",
    "subject to",
    "not permitted",
)

_DOCUMENT_IDENTITY_TERMS = {
    "title",
    "organisation",
    "organization",
    "buyer",
    "issuer",
    "issuing",
    "publish",
    "publication",
    "release",
    "category",
    "type",
}

_RETRIEVAL_ONLY_SINGLE_WORDS = {
    "amount",
    "category",
    "date",
    "details",
    "document",
    "location",
    "number",
    "platform",
    "framework",
    "reference",
    "score",
    "subject",
    "title",
    "type",
    "value",
}


@lru_cache(maxsize=8192)
def _signal_words(value: str) -> tuple[str, ...]:
    return tuple(
        _stem(word)
        for word in _WORD_RE.findall(value.casefold().replace("_", " "))
        if len(word) > 1
    )


def _signal_phrase_covered(searchable: str, phrase: Any) -> bool:
    """Match reordered or inflected procurement phrases without semantic search."""

    phrase_words = _signal_words(str(phrase or ""))
    if len(phrase_words) < 2:
        return False
    searchable_words = _signal_words(searchable)

    def matches(word: str, candidate: str) -> bool:
        return bool(
            word == candidate
            or (
                abs(len(word) - len(candidate)) <= 2
                and min(len(word), len(candidate)) >= 5
                and (word.startswith(candidate) or candidate.startswith(word))
                and SequenceMatcher(None, word, candidate).ratio() >= 0.8
            )
            or (
                len(word) >= 7
                and len(candidate) >= 7
                and candidate[:7] == word[:7]
            )
        )

    cursor = 0
    first_match: int | None = None
    for phrase_index, word in enumerate(phrase_words):
        if phrase_index == 0:
            match_index = next(
                (
                    index
                    for index, candidate in enumerate(searchable_words)
                    if matches(word, candidate)
                ),
                None,
            )
            first_match = match_index
        else:
            match_index = next(
                (
                    index
                    for index in range(cursor, min(len(searchable_words), cursor + 9))
                    if matches(word, searchable_words[index])
                ),
                None,
            )
        if match_index is None:
            return False
        cursor = match_index + 1
    return first_match is not None and cursor - first_match <= max(12, len(phrase_words) + 6)


def _needs_document_start_lane(section: RfpSectionSpec, field: RfpFieldSpec) -> bool:
    key_terms = set(_WORD_RE.findall(f"{field.key} {field.label}".casefold()))
    section_text = f"{section.key} {section.label}".casefold()
    administrative_section = any(
        phrase in section_text
        for phrase in ("tender details", "tender_details", "administrative details")
    )
    return (
        administrative_section
        or field.key.endswith("_id")
        or bool(key_terms & _DOCUMENT_IDENTITY_TERMS)
    )


def _needs_gate_lane(field: RfpFieldSpec) -> bool:
    text = f"{field.key} {field.label} {field.description} {field.prompt}".casefold()
    return any(term in text for term in ("score", "threshold", "qualifying", "evaluation gate"))


def _needs_proof_lane(field: RfpFieldSpec) -> bool:
    text = f"{field.key} {field.label} {field.description} {field.prompt}".casefold()
    return any(
        term in text
        for term in (
            "proof",
            "evidence",
            "certificate",
            "affidavit",
            "undertaking",
            "registration",
            "authorization",
            "authorisation",
            "documentary",
            "required document",
        )
    )


def _binding_phrases(field: RfpFieldSpec) -> list[str]:
    """Return explicit high-precision phrases used to assign amendments."""

    signature_tokens = set(
        _tokens(f"{field.key} {field.label} {field.description}")
    )
    signature_roots = {
        token[:-1] if token.endswith("s") and not token.endswith("ss") else token
        for token in signature_tokens
    }
    phrases: list[str] = []
    explicit_phrases = {
        _phrase_text(phrase)
        for phrase in [
            field.label,
            *field.retrieval_guidance.anchor_phrases,
            *field.retrieval_guidance.binding_phrases,
        ]
    }
    substantive_signature = signature_tokens - _STOP_WORDS - _RETRIEVAL_ONLY_SINGLE_WORDS
    for phrase in [
        field.label,
        *field.retrieval_guidance.anchor_phrases,
        *field.retrieval_guidance.binding_phrases,
        *field.retrieval_guidance.aliases,
        *field.retrieval_guidance.keywords,
    ]:
        normalized = _phrase_text(phrase)
        phrase_tokens = set(_tokens(normalized))
        if not phrase_tokens:
            continue
        explicit = normalized in explicit_phrases
        substantive_phrase = phrase_tokens - _STOP_WORDS - _RETRIEVAL_ONLY_SINGLE_WORDS
        if not explicit and not (substantive_phrase & substantive_signature):
            continue
        only_token = next(iter(phrase_tokens)) if len(phrase_tokens) == 1 else ""
        if (
            only_token in _RETRIEVAL_ONLY_SINGLE_WORDS
            and len(normalized.split()) == 1
        ):
            continue
        only_root = (
            only_token[:-1]
            if only_token.endswith("s") and not only_token.endswith("ss")
            else only_token
        )
        if (
            explicit
            or len(phrase_tokens) >= 2
            or (only_token and len(only_token) <= 4)
            or phrase_tokens <= signature_tokens
            or (only_root and only_root in signature_roots)
        ):
            phrases.append(phrase)
    return list(dict.fromkeys(phrase for phrase in phrases if _phrase_text(phrase)))


def _binding_text_match(value: str, field: RfpFieldSpec) -> bool:
    searchable = _phrase_text(value)
    searchable_tokens = set(_tokens(searchable))
    if any(
        _contains_phrase(searchable, phrase)
        or _signal_phrase_covered(searchable, phrase)
        or (
            len(phrase_tokens := set(_tokens(phrase))) >= 3
            and len(phrase_tokens & searchable_tokens) / len(phrase_tokens) >= 0.8
        )
        for phrase in _binding_phrases(field)
    ):
        return True
    generic_labels = {
        _phrase_text(phrase)
        for phrase in (
            field.label,
            *field.retrieval_guidance.anchor_phrases,
            *field.retrieval_guidance.aliases,
        )
        if len(phrase_tokens := _tokens(str(phrase))) == 1
        and phrase_tokens[0] in _RETRIEVAL_ONLY_SINGLE_WORDS
    }
    return any(
        re.search(
            rf"(?:^|[|\n])\s*{re.escape(label)}\s*(?::|\||-|\u2013|"
            rf"\b(?:is\s+)?(?:revised|changed|amended|modified|updated)\b)",
            value,
            re.IGNORECASE,
        )
        for label in generic_labels
    )


def _field_subject_compatible(field: RfpFieldSpec, value: str) -> bool:
    """Reject an explicit actor mismatch encoded by the configurable field prompt."""

    intent = _phrase_text(f"{field.description} {field.prompt}")
    source = _phrase_text(value)
    if "service level" in intent:
        service_cues = (
            "service level", "sla", "service performance", "performance score",
            "performance standard", "target score", "cleanliness standard",
            "consistency", "service quality",
        )
        if not any(cue in source for cue in service_cues):
            return False
    actor_markers = {
        "bidder": ("bidder", "bidding entity", "lead member"),
        "oem": ("oem", "original equipment manufacturer", "manufacturer"),
        "subcontractor": ("subcontractor", "sub contractor"),
        "consortium": ("consortium", "joint venture"),
    }
    required_actor = next(
        (
            actor
            for actor in actor_markers
            if any(
                cue in intent
                for cue in (
                    f"from the {actor}",
                    f"from {actor}",
                    f"of {actor}",
                    f"{actor} itself",
                    f"{actor} owned",
                    f"{actor} only",
                    f"{actor} field",
                )
            )
        ),
        "",
    )
    if not required_actor:
        return True
    required_present = any(marker in source for marker in actor_markers[required_actor])
    other_present = any(
        marker in source
        for actor, markers in actor_markers.items()
        if actor != required_actor
        for marker in markers
    )
    if not required_present and other_present:
        return False
    if required_actor == "bidder" and any(
        cue in intent for cue in ("oem only", "product only", "oem-only", "product-only")
    ) and re.search(r"\b(?:oem|manufacturer|product|device)\b.{0,50}\bcertif", source):
        return False
    return True


def _field_presence_match(unit: dict[str, Any], field: RfpFieldSpec) -> bool:
    text = _unit_text(unit)
    anchors = [
        phrase
        for phrase in field.retrieval_guidance.anchor_phrases
        if _phrase_text(phrase)
    ]
    matched = (
        any(
            _contains_phrase(_phrase_text(text), phrase)
            or _signal_phrase_covered(_phrase_text(text), phrase)
            for phrase in anchors
        )
        if anchors
        else _binding_text_match(text, field)
    )
    return matched and _field_subject_compatible(field, text)


def _direct_field_match(unit: dict[str, Any], field: RfpFieldSpec) -> bool:
    searchable = _phrase_text(
        " ".join(
            (
                _unit_text(unit),
                " ".join(str(item) for item in unit.get("section_path") or []),
                " ".join(str(item) for item in unit.get("table_headers") or []),
            )
        )
    )
    phrases = [
        field.label,
        *field.retrieval_guidance.anchor_phrases,
        *field.retrieval_guidance.aliases,
        *field.retrieval_guidance.keywords,
    ]
    return any(
        _contains_phrase(searchable, normalized)
        for phrase in phrases
        if len(normalized := _phrase_text(phrase)) >= 3
    )


def _configured_anchor_match(unit: dict[str, Any], field: RfpFieldSpec) -> bool:
    searchable = _phrase_text(
        " ".join(
            (
                _unit_text(unit),
                " ".join(str(item) for item in unit.get("section_path") or []),
                " ".join(str(item) for item in unit.get("table_headers") or []),
            )
        )
    )
    return any(
        _contains_phrase(searchable, phrase)
        or _signal_phrase_covered(searchable, phrase)
        for phrase in field.retrieval_guidance.anchor_phrases
        if _phrase_text(phrase)
    )


def _date_event_kinds(value: str) -> set[str]:
    text = _phrase_text(value)
    patterns = {
        "prebid_query": ("pre bid query", "prebid query", "submission of pre bid queries"),
        "bid_submission": (
            "bid submission",
            "submission of bid",
            "proposal submission",
            "tender submission",
            "receipt of tender",
        ),
        "bid_opening": ("bid opening", "opening of bid", "opening of technical", "opening of financial"),
        "prebid_meeting": ("pre bid meeting", "prebid meeting"),
        "publication": ("publication date", "publish date", "date of issue", "release date"),
        "clarification": ("clarification start", "clarification end", "seek clarification"),
        "download": ("document download", "downloading of bidding documents"),
    }
    return {
        kind
        for kind, phrases in patterns.items()
        if any(phrase in text for phrase in phrases)
    }


def _date_event_compatible(field: RfpFieldSpec, text: str) -> bool:
    expected = _date_event_kinds(
        " ".join(
            [
                field.key,
                field.label,
                field.description,
                *field.retrieval_guidance.aliases,
                *field.retrieval_guidance.anchor_phrases,
            ]
        )
    )
    observed = _date_event_kinds(text)
    return not expected or not observed or bool(expected & observed)


def _direct_scalar_update_value(
    field: RfpFieldSpec,
    item: dict[str, Any],
) -> str:
    if field.value_type not in {"boolean", "integer", "number", "money", "date"}:
        return ""
    if item.get("formal_revision") or item.get("issuer_response"):
        return ""
    text = _lexical_text(_compact_text(item.get("text")))
    percentage_cue_tokens = set(_tokens(f"{field.key} {field.label}")) - {
        "evaluation",
        "mark",
        "percent",
        "percentage",
        "point",
        "ratio",
        "score",
        "weightage",
    }
    semantic_percentage_match = bool(
        _field_numeric_dimension(field) == "percentage"
        and percentage_cue_tokens & set(_tokens(text))
    )
    semantic_date_match = False
    if field.value_type == "date":
        if not _date_event_compatible(field, text):
            return ""
        field_tokens = set(
            _tokens(
                " ".join(
                    [
                        field.label,
                        field.description,
                        *field.retrieval_guidance.aliases,
                        *field.retrieval_guidance.anchor_phrases,
                    ]
                )
            )
        )
        semantic_date_match = len(set(_tokens(text)) & field_tokens) >= 3
    if not (
        (
            _binding_text_match(text, field)
            or semantic_date_match
            or semantic_percentage_match
        )
        and _field_subject_compatible(field, text)
    ):
        return ""
    if field.value_type == "date":
        matches = list(_DATE_VALUE_RE.finditer(text))
        return _compact_text(text[matches[0].start() :])[:200] if len(matches) == 1 else ""
    if field.value_type == "money":
        amounts = re.findall(
            r"(?<![A-Za-z])(?:INR|Rs\.?|₹|â‚¹)\s*[\d,.]+"
            r"(?:\s*(?:crores?|lakhs?|lacs?|million|billion))?",
            text,
            re.IGNORECASE,
        )
        return _compact_text(amounts[0]) if len(set(amounts)) == 1 else ""
    if field.value_type == "boolean":
        return text if _explicit_boolean_polarity(field, text) is not None else ""
    if _field_numeric_dimension(field) == "count":
        values = list(
            dict.fromkeys(
                str(
                    _NUMBER_WORDS.get(
                        match.group(0).casefold(),
                        int(match.group(0)) if match.group(0).isdigit() else -1,
                    )
                )
                for match in re.finditer(
                    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b",
                    text,
                    re.I,
                )
                if _numeric_token_compatible_for_field(
                    field, text, match.start(), match.end()
                )
            )
        )
        return text if len(values) == 1 and values[0] != "-1" else ""
    expected_dimension = _field_numeric_dimension(field)
    numeric_matches = []
    for match in re.finditer(r"(?<![a-z])\d+(?:\.\d+)?", text.casefold()):
        if not _numeric_token_compatible_for_field(
            field, text, match.start(), match.end()
        ):
            continue
        observed_dimension = _numeric_token_dimension(
            text, match.start(), match.end()
        )
        nearby = _phrase_text(
            text[max(0, match.start() - 50) : min(len(text), match.end() + 50)]
        )
        if (
            expected_dimension == "percentage"
            and observed_dimension == "generic"
            and not any(
                cue in nearby for cue in ("weightage", "percent", "percentage", "ratio")
            )
        ):
            continue
        numeric_matches.append(match)
    values = list(dict.fromkeys(match.group(0) for match in numeric_matches))
    if len(values) == 1:
        return values[0]
    if _field_numeric_dimension(field) == "percentage":
        cue_matches = [
            match
            for match in _WORD_RE.finditer(text)
            if _stem(match.group(0).casefold()) in percentage_cue_tokens
        ]
        bound_values: list[str] = []
        for number_match in numeric_matches:
            if (
                _numeric_token_dimension(
                    text, number_match.start(), number_match.end()
                )
                != "percentage"
            ):
                continue
            preceding_cues = [
                match
                for match in cue_matches
                if match.end() <= number_match.start()
                and number_match.start() - match.end() <= 80
            ]
            if not preceding_cues:
                continue
            nearest_cue = max(preceding_cues, key=lambda match: match.end())
            if any(
                other.start() >= nearest_cue.end()
                and other.start() < number_match.start()
                for other in numeric_matches
            ):
                continue
            bound_values.append(number_match.group(0))
        unique_bound_values = list(dict.fromkeys(bound_values))
        if len(unique_bound_values) == 1:
            return unique_bound_values[0]
    return ""


def _requires_exhaustive_coverage(field: RfpFieldSpec) -> bool:
    return field.validation.allow_multiple or field.value_type in {
        "string_array",
        "object",
        "object_array",
    }


def _value_matches(value: Any, value_type: str) -> bool:
    if value_type in {"string", "date", "money"}:
        return isinstance(value, str) and bool(value.strip())
    if value_type == "string_array":
        return bool(value) and isinstance(value, list) and all(
            isinstance(item, str) and bool(item.strip()) for item in value
        )
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "object":
        return isinstance(value, dict)
    if value_type == "object_array":
        return bool(value) and isinstance(value, list) and all(
            isinstance(item, dict) and bool(item) for item in value
        )
    return False


def _field_output_budget(
    field: RfpFieldSpec,
    effective_state: dict[str, Any],
    configured_budget: int,
) -> int:
    del field, effective_state
    return max(128, int(configured_budget))


def _explicit_boolean_polarity(field: RfpFieldSpec, text: str) -> bool | None:
    normalized = _phrase_text(text)
    field_text = _phrase_text(f"{field.key} {field.label} {field.description}")
    permission_field = "allowed" in field_text or "permitted" in field_text
    negative = bool(
        re.search(
            r"\b(?:not|never)\s+(?:be\s+)?(?:permitted|allowed|required|applicable)\b|"
            r"\b(?:shall|must)\s+not\b|\bprohibited\b",
            normalized,
        )
        or (permission_field and re.search(r"\bonly\s+(?:a\s+)?single\b", normalized))
        or (
            "reverse auction" in field_text
            and re.search(r"\bonly\s+fixed\s+price\s+financial\s+bids?\b", normalized)
        )
    )
    if negative:
        return False
    if permission_field and re.search(r"\b(?:allowed|permitted)\b", normalized):
        return True
    requirement_field = any(
        cue in field_text for cue in ("required", "requirement", "applicable", "mandatory")
    )
    if requirement_field and any(
        cue in normalized
        for cue in (
            "to be submitted",
            "shall be submitted",
            "must be submitted",
            "shall submit",
            "must submit",
            "is required",
            "is applicable",
            "shall apply",
        )
    ):
        return True
    return None


def _field_numeric_dimension(field: RfpFieldSpec) -> str:
    target = _phrase_text(f"{field.key} {field.label} {field.description}")
    if any(cue in target for cue in (" count", "number of", "how many")):
        return "count"
    if any(cue in target for cue in (" percent", " percentage", " pct", "weightage")):
        return "percentage"
    if any(
        cue in target
        for cue in (
            " inr",
            "turnover",
            "revenue",
            "net worth",
            "estimated value",
            "tender fee",
            "bid security",
            "emd",
        )
    ):
        return "money"
    if any(cue in target for cue in (" mark", "score", "point")):
        return "score"
    if any(cue in target for cue in (" year", " month", " day", "duration")):
        return "duration"
    return "generic"


def _numeric_token_dimension(text: str, start: int, end: int) -> str:
    prefix = text[max(0, start - 24) : start].casefold()
    suffix = text[end : end + 28].casefold()
    if re.search(r"(?:\binr|\brs\.?|₹|â‚¹|\brupees?)\s*$", prefix) or re.match(
        r"\s*(?:crores?|lakhs?|lacs?|million|billion|rupees?)\b", suffix
    ):
        return "money"
    if re.match(r"\s*%", suffix) or re.match(
        r"\s*percent(?:age)?\b", suffix
    ):
        return "percentage"
    if re.match(r"\s*(?:years?|months?|days?|hours?)\b", suffix):
        return "duration"
    if re.match(r"\s*(?:marks?|points?|scores?)\b", suffix):
        return "score"
    if re.match(
        r"\s*(?:projects?|assignments?|works?|contracts?|bidders?|documents?)\b",
        suffix,
    ):
        return "count"
    return "generic"


def _numeric_dimension_compatible(expected: str, observed: str) -> bool:
    if observed == "generic":
        return True
    return expected != "generic" and expected == observed


def _numeric_token_compatible_for_field(
    field: RfpFieldSpec, text: str, start: int, end: int
) -> bool:
    observed = _numeric_token_dimension(text, start, end)
    expected = _field_numeric_dimension(field)
    if expected != "count":
        return _numeric_dimension_compatible(expected, observed)
    if observed not in {"generic", "count"}:
        return False
    tail = text[end : end + 64].split("|", 1)[0]
    target_tokens = set(
        _tokens(f"{field.key} {field.label} {field.description}")
    )
    return bool(target_tokens & set(_tokens(tail)))


def _has_compatible_numeric_claim(field: RfpFieldSpec, text: str) -> bool:
    expected = _field_numeric_dimension(field)
    for match in re.finditer(
        r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)\b",
        text,
        re.I,
    ):
        if _numeric_token_compatible_for_field(field, text, match.start(), match.end()):
            return True
    return expected == "generic"
