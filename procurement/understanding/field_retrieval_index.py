"""Source-unit normalization and retrieval indexes."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Callable

from .field_core import (
    _EXPLICIT_UPDATE_CUES,
    _compact_text,
    _implicit_parallel_revision_cells,
    _is_explicit_full_replacement,
    _is_replacement_scope,
    _issuer_response_text,
    _paired_nested_revision_cells,
    _phrase_text,
    _scope_contains,
    _section_scope_key,
    _table_cells,
    _table_header_index,
    _tokens,
    _unit_document,
    _unit_page,
    _unit_page_id,
    _unit_reference_keys,
    _unit_text,
)


class FieldRetrievalIndex:
    """Methods for this retrieval responsibility."""

    def __init__(
        self,
        source_units: dict[str, dict[str, Any]],
        semantic_ranker: Callable[[str, list[tuple[str, str]]], list[str]] | None = None,
        semantic_pool: int = 32,
    ):
        self.semantic_ranker = semantic_ranker
        self.semantic_pool = max(1, semantic_pool)
        self.units = {
            str(source_id): dict(unit)
            for source_id, unit in source_units.items()
            if isinstance(unit, dict) and _unit_text(unit)
        }
        self.ids = list(self.units)
        self.position = {source_id: index for index, source_id in enumerate(self.ids)}
        self.chronology_warnings = sorted(
            {
                str(unit.get("revision_order_warning"))
                for unit in self.units.values()
                if unit.get("revision_order_warning")
            }
        )
        document_first_position: dict[str, int] = {}
        document_declared_order: dict[str, int] = {}
        document_role: dict[str, str] = {}
        document_revision: dict[str, int | None] = {}
        for position, source_id in enumerate(self.ids, start=1):
            unit = self.units[source_id]
            document = _unit_document(unit)
            document_first_position.setdefault(document, position)
            try:
                declared_order = int(unit.get("document_order") or 0)
            except (TypeError, ValueError):
                declared_order = 0
            if declared_order:
                document_declared_order.setdefault(document, declared_order)
            document_role.setdefault(document, str(unit.get("source_role") or "unknown"))
            try:
                revision = int(unit.get("revision_sequence")) if unit.get("revision_sequence") else None
            except (TypeError, ValueError):
                revision = None
            if document not in document_revision or document_revision[document] is None:
                document_revision[document] = revision

        self.document_order = {
            document: document_declared_order.get(document, position)
            for document, position in document_first_position.items()
        }
        update_documents = [
            document
            for document in document_first_position
            if document_role.get(document) == "post_base_update"
        ]
        revisions = [document_revision.get(document) for document in update_documents]
        complete_revision_order = bool(update_documents) and all(
            isinstance(revision, int) and revision > 0 for revision in revisions
        ) and len(set(revisions)) == len(revisions)
        self.chronology_basis = (
            "unresolved_revision_order"
            if self.chronology_warnings
            else "complete_revision_sequence"
            if complete_revision_order
            else "packet_order"
        )
        base_documents = sorted(
            (document for document in document_first_position if document not in update_documents),
            key=lambda document: self.document_order[document],
        )
        ordered_updates = sorted(
            update_documents,
            key=(
                (lambda document: (document_revision[document] or 0, self.document_order[document]))
                if complete_revision_order
                else (lambda document: self.document_order[document])
            ),
        )
        self.document_authority = {
            document: rank
            for rank, document in enumerate([*base_documents, *ordered_updates], start=1)
        }
        for unit in self.units.values():
            document = _unit_document(unit)
            unit["document_order"] = self.document_order.get(document, 0)
            unit["authority_rank"] = self.document_authority.get(document, 0)
            unit["chronology_basis"] = self.chronology_basis
            unit["section_scope"] = _section_scope_key(unit)
            unit["replacement_scope"] = _is_replacement_scope(unit)

        self.scope_document_ids: dict[tuple[str, str], list[str]] = {}
        replacement_documents: dict[str, set[str]] = {}
        self.replacement_marker_ids: dict[tuple[str, str], list[str]] = {}
        full_replacement_scopes: set[tuple[str, str]] = set()
        for source_id, unit in self.units.items():
            scope = str(unit.get("section_scope") or "")
            document = _unit_document(unit)
            if scope:
                self.scope_document_ids.setdefault((scope, document), []).append(source_id)
            if scope and unit.get("replacement_scope"):
                replacement_documents.setdefault(scope, set()).add(document)
                self.replacement_marker_ids.setdefault((scope, document), []).append(source_id)
                if _is_explicit_full_replacement(unit):
                    full_replacement_scopes.add((scope, document))
        replacement_table_scopes = {
            (replacement_scope, document)
            for replacement_scope, documents in replacement_documents.items()
            for document in documents
            if any(
                _unit_document(candidate) == document
                and candidate.get("unit_type") in {"table_header", "table_row"}
                and _scope_contains(
                    replacement_scope,
                    str(candidate.get("section_scope") or ""),
                )
                for candidate in self.units.values()
            )
        }
        self.latest_replacement_document = {
            scope: max(
                documents,
                key=lambda document: self.document_authority.get(document, 0),
            )
            for scope, documents in replacement_documents.items()
        }
        self.superseded_by: dict[str, str] = {}
        self.superseded_by_scope: dict[str, str] = {}
        for source_id, unit in self.units.items():
            scope = str(unit.get("section_scope") or "")
            document = _unit_document(unit)
            candidates = [
                (replacement_scope, replacement_document)
                for replacement_scope, replacement_document in self.latest_replacement_document.items()
                if _scope_contains(replacement_scope, scope)
                and self.document_authority.get(replacement_document, 0)
                > self.document_authority.get(document, 0)
                and (
                    (replacement_scope, replacement_document)
                    in full_replacement_scopes
                    or (
                        unit.get("unit_type") in {"table_header", "table_row"}
                        and (replacement_scope, replacement_document)
                        in replacement_table_scopes
                    )
                    or (
                        unit.get("unit_type")
                        in {"heading", "header", "section_heading", "title"}
                        and scope == replacement_scope
                    )
                )
            ]
            replacement_scope, latest_document = max(
                candidates,
                key=lambda item: (
                    self.document_authority.get(item[1], 0),
                    len(item[0]),
                ),
                default=("", ""),
            )
            if (
                latest_document
                and latest_document != document
            ):
                self.superseded_by[source_id] = latest_document
                self.superseded_by_scope[source_id] = replacement_scope
                unit["effective_state"] = "superseded"
                unit["superseded_by_document_id"] = latest_document
                unit["superseded_by_scope"] = replacement_scope
            else:
                unit["effective_state"] = "effective"
                unit["superseded_by_document_id"] = ""
                unit["superseded_by_scope"] = ""
        self.table_continuations: dict[str, list[str]] = {}
        self.continuation_parent: dict[str, str] = {}
        table_parent: dict[str, str] = {}
        for source_id, unit in self.units.items():
            table = str(unit.get("table") or "")
            cells = _table_cells(unit)
            if not table or unit.get("unit_type") != "table_row":
                continue
            if len(cells) > 1:
                parent_id = table_parent.get(table)
                headers = [str(item) for item in unit.get("table_headers") or []]
                formal_headers = (
                    _table_header_index(headers, "existing", "current clause") is not None
                    and _table_header_index(
                        headers,
                        "revised",
                        "modified clause",
                        "amended clause",
                    )
                    is not None
                )
                if (
                    parent_id
                    and formal_headers
                    and _unit_page_id(unit) == _unit_page_id(self.units[parent_id])
                    and len(_table_cells(self.units[parent_id])) > len(headers)
                    and len(cells) <= len(headers)
                ):
                    self.table_continuations.setdefault(parent_id, []).append(source_id)
                    self.continuation_parent[source_id] = parent_id
                    continue
                table_parent[table] = source_id
                continue
            parent_id = table_parent.get(table)
            if (
                parent_id
                and len(unit.get("table_headers") or []) > 1
                and _unit_page_id(unit) == _unit_page_id(self.units[parent_id])
            ):
                self.table_continuations.setdefault(parent_id, []).append(source_id)
                self.continuation_parent[source_id] = parent_id
        last_row_by_headers: dict[tuple[str, str, tuple[str, ...]], str] = {}
        seen_data_pages: set[tuple[str, str, tuple[str, ...], int]] = set()
        for source_id, unit in self.units.items():
            if source_id in self.continuation_parent or unit.get("unit_type") != "table_row":
                continue
            headers = [str(item) for item in unit.get("table_headers") or []]
            cells = _table_cells(unit)
            if len(headers) < 3 or not cells:
                continue
            header_signature = tuple(_phrase_text(header) for header in headers)
            if set(header_signature).issubset({_phrase_text(cell) for cell in cells}):
                continue
            key = (
                _unit_document(unit),
                str(unit.get("section_id") or ""),
                header_signature,
            )
            page_key = (*key, _unit_page(unit))
            parent_id = last_row_by_headers.get(key)
            parent_cells = _table_cells(self.units[parent_id]) if parent_id else []
            requirement_index = _table_header_index(
                headers, "requirement", "criteria", "description", "particulars"
            )
            parent_requirement = (
                parent_cells[requirement_index]
                if parent_id
                and requirement_index is not None
                and requirement_index < len(parent_cells)
                else ""
            )
            if (
                parent_id
                and page_key not in seen_data_pages
                and _unit_page(unit) == _unit_page(self.units[parent_id]) + 1
                and len(parent_cells) >= len(headers)
                and len(cells) <= len(headers) - 2
                and len(_compact_text(cells[0])) >= 20
                and not re.fullmatch(r"\d+[a-z]?[.)]?", _compact_text(cells[0]), re.I)
                and not re.search(r"[.!?;:]\s*$", _compact_text(parent_requirement))
            ):
                self.table_continuations.setdefault(parent_id, []).append(source_id)
                self.continuation_parent[source_id] = parent_id
                seen_data_pages.add(page_key)
                continue
            seen_data_pages.add(page_key)
            if len(cells) >= len(headers):
                last_row_by_headers[key] = source_id
        last_formal_parent: dict[tuple[str, tuple[str, ...]], str] = {}
        for source_id, unit in self.units.items():
            if source_id in self.continuation_parent or unit.get("unit_type") != "table_row":
                continue
            headers = [str(item) for item in unit.get("table_headers") or []]
            existing_index = _table_header_index(headers, "existing", "current clause")
            revised_index = _table_header_index(
                headers, "revised", "modified clause", "amended clause"
            )
            cells = _table_cells(unit)
            if existing_index is None or revised_index is None or not cells:
                continue
            header_signature = tuple(_phrase_text(header) for header in headers)
            key = (_unit_document(unit), header_signature)
            normalized_cells = {_phrase_text(cell) for cell in cells}
            if {
                _phrase_text(headers[existing_index]),
                _phrase_text(headers[revised_index]),
            }.issubset(normalized_cells):
                continue
            parent_id = last_formal_parent.get(key)
            looks_like_target = bool(
                len(cells) >= 2
                and re.fullmatch(r"\d+[a-z]?[.)]?", _compact_text(cells[0]), re.I)
                and re.fullmatch(
                    r"\d+(?:\s*[-/,]\s*\d+)*[a-z]?",
                    _compact_text(cells[1]),
                    re.I,
                )
            )
            if (
                parent_id
                and len(cells) < len(headers)
                and (
                    len(cells) > 1
                    or len(_compact_text(cells[0])) >= 30
                )
                and not looks_like_target
                and _unit_page(unit) == _unit_page(self.units[parent_id]) + 1
            ):
                self.table_continuations.setdefault(parent_id, []).append(source_id)
                self.continuation_parent[source_id] = parent_id
                continue
            if len(cells) >= len(headers) or looks_like_target:
                last_formal_parent[key] = source_id
        for parent_id, parent in self.units.items():
            if _implicit_parallel_revision_cells(parent) is None:
                continue
            parent_tokens = set(_tokens(_unit_text(parent)))
            neighbors_by_page: dict[int, list[tuple[float, int, str]]] = {}
            for candidate_id, candidate in self.units.items():
                if (
                    candidate_id == parent_id
                    or candidate_id in self.continuation_parent
                    or _unit_document(candidate) != _unit_document(parent)
                    or candidate.get("section_id") != parent.get("section_id")
                    or abs(_unit_page(candidate) - _unit_page(parent)) != 1
                    or candidate.get("unit_type") not in {"table_header", "table_row"}
                ):
                    continue
                candidate_tokens = set(_tokens(_unit_text(candidate)))
                shared = parent_tokens & candidate_tokens
                if not candidate_tokens or len(shared) < 6:
                    continue
                containment = len(shared) / min(len(parent_tokens), len(candidate_tokens))
                if containment < 0.2:
                    continue
                neighbors_by_page.setdefault(_unit_page(candidate), []).append(
                    (containment, len(shared), candidate_id)
                )
            linked = [
                max(candidates)[2]
                for _page, candidates in sorted(neighbors_by_page.items())
            ]
            if linked:
                continuations = self.table_continuations.setdefault(parent_id, [])
                for candidate_id in linked:
                    if candidate_id not in continuations:
                        continuations.append(candidate_id)
                    self.continuation_parent[candidate_id] = parent_id
        for source_id, continuation_ids in self.table_continuations.items():
            nested_revision = _paired_nested_revision_cells(
                self.units[source_id],
                [
                    {**self.units[continuation_id], "source_id": continuation_id}
                    for continuation_id in continuation_ids
                ],
            )
            if not nested_revision:
                continue
            self.units[source_id]["existing_text"] = nested_revision[0]
            self.units[source_id]["revised_text"] = nested_revision[1]
            self.units[source_id]["revision_source_ids"] = nested_revision[2]
            self.units[source_id]["revision_layout"] = "paired_nested_rows"
            self.units[source_id]["formal_revision"] = True
        self.term_frequencies: dict[str, Counter[str]] = {}
        self.lengths: dict[str, int] = {}
        self.term_to_ids: dict[str, set[str]] = {}
        document_frequency: Counter[str] = Counter()
        for source_id, unit in self.units.items():
            terms = _tokens(
                " ".join(
                    (
                        _unit_text(unit),
                        " ".join(
                            _unit_text(self.units[continuation_id])
                            for continuation_id in self.table_continuations.get(source_id, [])
                        ),
                        " ".join(str(item) for item in unit.get("section_path") or []),
                        " ".join(str(item) for item in unit.get("table_headers") or []),
                    )
                )
            )
            counts = Counter(terms)
            self.term_frequencies[source_id] = counts
            self.lengths[source_id] = max(1, sum(counts.values()))
            document_frequency.update(counts)
            for term in counts:
                self.term_to_ids.setdefault(term, set()).add(source_id)
        self.document_frequency = document_frequency
        self.average_length = (
            sum(self.lengths.values()) / max(1, len(self.lengths))
        )
        self.page_ids = {
            _unit_page_id(unit)
            for unit in self.units.values()
            if _unit_page_id(unit)
        }
        self.page_to_ids: dict[str, list[str]] = {}
        self.document_page_to_id: dict[tuple[str, int], str] = {}
        self.reference_to_ids: dict[str, list[str]] = {}
        self.formal_revision_sections = {
            str(unit.get("section_id") or "")
            for unit in self.units.values()
            if unit.get("source_role") == "post_base_update"
            and "existing clause" in _unit_text(unit).casefold()
            and "revised clause" in _unit_text(unit).casefold()
            and unit.get("section_id")
        }
        self.formal_revision_tables = {
            str(unit.get("table") or "")
            for unit in self.units.values()
            if unit.get("source_role") == "post_base_update"
            and unit.get("table")
            and (
                (
                    any(
                        "existing" in _phrase_text(header)
                        or "current clause" in _phrase_text(header)
                        for header in unit.get("table_headers") or []
                    )
                    and any(
                        "revised" in _phrase_text(header)
                        or "modified clause" in _phrase_text(header)
                        or "amended clause" in _phrase_text(header)
                        for header in unit.get("table_headers") or []
                    )
                )
                or _implicit_parallel_revision_cells(unit) is not None
            )
        }
        formal_tables_by_page: dict[str, set[str]] = {}
        for unit in self.units.values():
            table = str(unit.get("table") or "")
            page_id = _unit_page_id(unit)
            if table in self.formal_revision_tables and page_id:
                formal_tables_by_page.setdefault(page_id, set()).add(table)
        for unit in self.units.values():
            table = str(unit.get("table") or "")
            page_formal_tables = formal_tables_by_page.get(_unit_page_id(unit), set())
            response_index = _table_header_index(
                unit.get("table_headers") or [],
                "issuer response",
                "bank response",
                "client response",
                "response",
                "amendment",
            )
            issuer_response = _issuer_response_text(unit)
            substantive_response_container = bool(
                response_index is not None
                and len(_tokens(issuer_response)) >= 4
                and (
                    any(cue in _phrase_text(issuer_response) for cue in _EXPLICIT_UPDATE_CUES)
                    or "no change" in _phrase_text(issuer_response)
                    or any(
                        cue in _phrase_text(issuer_response)
                        for cue in (" shall ", " must ", " only ", " required ")
                    )
                )
            )
            unit["formal_revision_scope"] = table in self.formal_revision_tables
            unit["embedded_revision_fragment"] = bool(
                unit.get("source_role") == "post_base_update"
                and table
                and page_formal_tables
                and table not in page_formal_tables
                and not substantive_response_container
            )
        logical_start_pages: dict[str, int] = {}
        for source_id, unit in self.units.items():
            page_id = _unit_page_id(unit)
            if page_id:
                self.page_to_ids.setdefault(page_id, []).append(source_id)
                self.document_page_to_id[(_unit_document(unit), _unit_page(unit))] = page_id
            for reference in _unit_reference_keys(unit):
                self.reference_to_ids.setdefault(reference, []).append(source_id)
            logical_document = str(
                unit.get("logical_document_id") or _unit_document(unit) or "document"
            )
            page = _unit_page(unit)
            if page and (
                logical_document not in logical_start_pages
                or page < logical_start_pages[logical_document]
            ):
                logical_start_pages[logical_document] = page
        self.document_start_ids = {
            source_id
            for source_id, unit in self.units.items()
            if _unit_page(unit) > 0
            and _unit_page(unit)
            <= logical_start_pages.get(
                str(unit.get("logical_document_id") or _unit_document(unit) or "document"),
                0,
            )
            + 1
        }


__all__ = ["FieldRetrievalIndex"]
