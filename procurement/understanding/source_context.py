"""Lossless source-page packing shared by package-wide AI reviews."""
import hashlib
import json


def source_fingerprint(units: dict[str, dict]) -> str:
    original = {key: value for key, value in units.items()
                if value.get("unit_type") != "anchor_fact" and str(value.get("text") or "").strip()}
    return hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def resolve_page_citation(ref: dict, units: dict[str, dict]) -> dict:
    """Ground exact wording; audit uniquely recoverable same-page block-ID mistakes."""
    normalize = lambda value: " ".join(str(value or "").split()).casefold()
    if not isinstance(ref, dict):
        raise ValueError("Invalid source citation")
    key, quote = ref.get("unit_id"), normalize(ref.get("quote"))
    unit = units.get(key)
    if not unit or not quote or unit.get("unit_type") == "anchor_fact":
        raise ValueError("Source citation is ungrounded")
    result = {"unit_id": key, "quote": ref["quote"]}
    if quote in normalize(unit.get("text")):
        return result
    location = (unit.get("document"), unit.get("page"))
    if any(value is None for value in location):
        raise ValueError("Source citation is ungrounded")
    matches = [candidate for candidate, value in units.items()
               if (value.get("document"), value.get("page")) == location
               and value.get("unit_type") != "anchor_fact"
               and quote in normalize(value.get("text"))]
    if not matches:
        # Only explicit extraction order can establish adjacency; IDs are not order.
        ordered = {}
        for candidate, value in units.items():
            order = value.get("reading_order")
            if ((value.get("document"), value.get("page")) == location
                    and type(order) is int):
                ordered.setdefault(order, []).append((candidate, value))
        spans = []
        for start in sorted(ordered):
            parts, keys = [], []
            position = start
            while position in ordered and len(ordered[position]) == 1:
                candidate, value = ordered[position][0]
                if value.get("unit_type") != "paragraph":
                    break
                parts.append(normalize(value.get("text")))
                keys.append(candidate)
                if len(keys) > 1 and quote in " ".join(parts):
                    # Exclude windows with an unused leading block.
                    if quote not in " ".join(parts[1:]):
                        spans.append(keys[:])
                    break
                position += 1
        if len(spans) == 1:
            return {**result, "unit_id": spans[0][0], "unit_ids": spans[0],
                    "original_unit_id": key,
                    "citation_resolution": "exact_contiguous_paragraph_span"}
    if len(matches) != 1:
        raise ValueError("Source citation is ungrounded or ambiguous")
    return {**result, "unit_id": matches[0], "original_unit_id": key,
            "citation_resolution": "unique_exact_quote_on_same_page"}


def pack_source_pages(units: dict[str, dict]) -> list[dict]:
    """Share document metadata; keep every source id directly beside its text."""
    documents = {}
    for key, unit in units.items():
        location = tuple(unit.get(field) for field in ("document", "source_file", "source_role"))
        pages = documents.setdefault(location, {})
        pages.setdefault(unit.get("page"), {})[key] = unit["text"]
    return [{**dict(zip(("document", "source_file", "source_role"), location)),
             "pages": [{"page": page, "units": rows} for page, rows in pages.items()]}
            for location, pages in documents.items()]
