"""Lexical, structural, semantic, and context ranking for field retrieval."""

from __future__ import annotations

from collections import Counter
import math
import re
from typing import Any

from .field_core import (
    _EXPLICIT_UPDATE_CUES,
    _configured_anchor_match,
    _contains_phrase,
    _field_subject_compatible,
    _field_target_match,
    _phrase_text,
    _table_cells,
    _tokens,
    _unit_document,
    _unit_page,
    _unit_page_id,
    _unit_reference_keys,
    _unit_text,
)

from .schema import (
    RfpFieldSpec,
    RfpSectionSpec,
    RfpUnderstandingSchema,
)


class FieldRetrievalScoring:
    """Methods for this retrieval responsibility."""

    def _query_weights(
        self,
        schema: RfpUnderstandingSchema,
        section: RfpSectionSpec,
        field: RfpFieldSpec,
        extra_query: str,
    ) -> Counter[str]:
        weighted: Counter[str] = Counter()
        for text, weight in (
            (field.key, 4),
            (field.label, 4),
            (" ".join(field.retrieval_guidance.aliases), 3),
            (" ".join(field.retrieval_guidance.keywords), 3),
            (field.description, 2),
            (field.prompt, 1),
            (extra_query, 4),
        ):
            for token in _tokens(text):
                weighted[token] += weight
        return weighted

    def _score(
        self,
        source_id: str,
        query: Counter[str],
        field: RfpFieldSpec,
    ) -> float:
        score = self._bm25_score(source_id, query)
        unit = self.units[source_id]
        searchable = _phrase_text(" ".join(
            (
                _unit_text(unit),
                " ".join(str(item) for item in unit.get("section_path") or []),
            )
        ))
        for phrase in [field.label, *field.retrieval_guidance.aliases]:
            normalized = _phrase_text(phrase)
            if len(normalized) >= 4 and _contains_phrase(searchable, normalized):
                score += 40.0 if len(normalized.split()) >= 2 else 8.0
        if self._likely_section_match(unit, field):
            score += 6.0
        anchor_match = self._anchor_match(unit, field)
        if anchor_match:
            score += 60.0
        signal_text = " ".join(
            [
                field.label,
                field.description,
                *field.retrieval_guidance.aliases,
                *field.retrieval_guidance.keywords,
                *field.retrieval_guidance.anchor_phrases,
            ]
        )
        signal_overlap = len(set(_tokens(signal_text)) & set(_tokens(searchable)))
        score += min(48.0, signal_overlap * 6.0)
        if (
            unit.get("source_role") == "post_base_update"
            and unit.get("formal_revision_scope")
            and not unit.get("embedded_revision_fragment")
            and (
                _field_target_match(unit, field, self.formal_revision_sections)
                or anchor_match
                or signal_overlap >= 3
            )
        ):
            score += 200.0
        return score

    @staticmethod
    def _likely_section_match(unit: dict[str, Any], field: RfpFieldSpec) -> bool:
        section_text = _phrase_text(
            " ".join(str(item) for item in unit.get("section_path") or [])
        )
        return any(
            normalized and _contains_phrase(section_text, normalized)
            for likely_section in field.retrieval_guidance.likely_sections
            if (normalized := _phrase_text(likely_section))
        )

    def _answer_shape_match(self, unit: dict[str, Any], field: RfpFieldSpec) -> bool:
        field_signature = set(
            _tokens(f"{field.key} {field.label} {field.description}")
        )
        if not field_signature & {"address", "city", "location", "place", "region", "site"}:
            return False
        if not self._likely_section_match(unit, field):
            return False
        return bool(
            re.search(
                r"\b(?:at|in|across|within)\s+(?:the\s+)?"
                r"[A-Z][A-Za-z0-9&.'/-]*"
                r"(?:[\s,]+(?:[A-Z][A-Za-z0-9&.'/-]*|of|the|and)){0,8}\b",
                _unit_text(unit),
            )
        )

    def _bm25_score(self, source_id: str, query: Counter[str]) -> float:
        counts = self.term_frequencies[source_id]
        length = self.lengths[source_id]
        score = 0.0
        total = max(1, len(self.units))
        for term, weight in query.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            document_frequency = self.document_frequency.get(term, 0)
            inverse_frequency = math.log(
                1.0 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + 1.2 * (
                0.25 + 0.75 * length / max(1.0, self.average_length)
            )
            score += weight * inverse_frequency * frequency * 2.2 / denominator
        return score

    def _concept_score(self, source_id: str, field: RfpFieldSpec) -> float:
        signal_terms = set(
            _tokens(
                " ".join(
                    [
                        field.label,
                        field.description,
                        *field.retrieval_guidance.aliases,
                        *field.retrieval_guidance.keywords,
                        *field.retrieval_guidance.anchor_phrases,
                    ]
                )
            )
        )
        overlap = signal_terms & set(self.term_frequencies[source_id])
        if len(overlap) < 2:
            return 0.0
        total = max(1, len(self.units))
        rarity = sum(
            math.log(1.0 + total / max(1, self.document_frequency.get(term, 1)))
            for term in overlap
        )
        # Tables are often long precisely because a complete revised criterion is present.
        # Use only a mild length penalty so compact bidder-query rows do not dominate them.
        length_penalty = 1.0 + math.log1p(self.lengths[source_id] / 300.0)
        return (rarity * 12.0 + len(overlap) * 8.0) / length_penalty

    @staticmethod
    def _anchor_match(unit: dict[str, Any], field: RfpFieldSpec) -> bool:
        if _configured_anchor_match(unit, field):
            return True
        searchable_tokens = set(
            _tokens(
                " ".join(
                    (
                        _unit_text(unit),
                        " ".join(str(item) for item in unit.get("section_path") or []),
                        " ".join(str(item) for item in unit.get("table_headers") or []),
                    )
                )
            )
        )
        return any(
            len(anchor_tokens := set(_tokens(str(phrase)))) >= 2
            and len(anchor_tokens & searchable_tokens) / len(anchor_tokens) >= 0.8
            for phrase in field.retrieval_guidance.anchor_phrases
            if _phrase_text(phrase)
        )

    def adjacent_page_ids(self, page_ids: list[str], radius: int) -> list[str]:
        result: list[str] = []
        for page_id in page_ids:
            ids = self.page_to_ids.get(page_id) or []
            if not ids:
                continue
            unit = self.units[ids[0]]
            document = _unit_document(unit)
            page = _unit_page(unit)
            for offset in range(-max(0, radius), max(0, radius) + 1):
                candidate = self.document_page_to_id.get((document, page + offset))
                if candidate and candidate not in result:
                    result.append(candidate)
        return result

    def _replacement_context_ids(self, source_id: str) -> list[str]:
        latest_document = self.superseded_by.get(source_id)
        if not latest_document:
            return []
        source = self.units[source_id]
        scope = str(source.get("section_scope") or "")
        candidates = self.scope_document_ids.get((scope, latest_document), [])
        source_cells = _table_cells(source)
        label_terms = set(_tokens(source_cells[1] if len(source_cells) > 1 else _unit_text(source)))
        source_clause = _phrase_text(str(source.get("clause_number") or ""))
        query = Counter({token: 1 for token in _tokens(_unit_text(source))})
        ranked: list[tuple[float, str]] = []
        for candidate_id in candidates:
            candidate = self.units[candidate_id]
            candidate_cells = _table_cells(candidate)
            candidate_label_terms = set(
                _tokens(candidate_cells[1] if len(candidate_cells) > 1 else _unit_text(candidate))
            )
            overlap = len(label_terms & candidate_label_terms)
            candidate_clause = _phrase_text(str(candidate.get("clause_number") or ""))
            score = self._bm25_score(candidate_id, query) + overlap * 12
            if source_clause and candidate_clause == source_clause:
                score += 60
            if score > 0:
                ranked.append((score, candidate_id))
        ranked.sort(reverse=True)
        return [candidate_id for _score, candidate_id in ranked[:2]]

    def _ranked(
        self,
        schema: RfpUnderstandingSchema,
        section: RfpSectionSpec,
        field: RfpFieldSpec,
        extra_query: str,
        *,
        semantic: bool = False,
    ) -> list[tuple[float, str]]:
        query = self._query_weights(schema, section, field, extra_query)
        ranked = sorted(
            (
                (score, source_id)
                for source_id in self._query_candidate_ids(query)
                if (score := self._score(source_id, query, field)) > 0
                and _field_subject_compatible(field, _unit_text(self.units[source_id]))
            ),
            reverse=True,
        )
        if not semantic or not self.semantic_ranker:
            return ranked
        query_text = " ".join(
            [
                field.key,
                field.label,
                field.description,
                field.prompt,
                *field.retrieval_guidance.aliases,
                *field.retrieval_guidance.keywords,
                *field.retrieval_guidance.anchor_phrases,
                extra_query,
            ]
        )
        # Semantic retrieval must be able to rescue units with no lexical token
        # overlap. BM25 remains a high-precision lane, not the admission gate.
        lexical_ids = [source_id for _score, source_id in ranked]
        lexical_id_set = set(lexical_ids)
        semantic_ids = [
            *lexical_ids,
            *(
                source_id
                for source_id in self.ids
                if source_id not in lexical_id_set
                and _field_subject_compatible(field, _unit_text(self.units[source_id]))
            ),
        ]
        semantic_candidates = [
            (source_id, _unit_text(self.units[source_id]))
            for source_id in semantic_ids
        ]
        by_id = {source_id: (0.0, source_id) for source_id, _text in semantic_candidates}
        by_id.update({item[1]: item for item in ranked})
        ordered_ids = self.semantic_ranker(
            query_text,
            semantic_candidates,
        )
        ordered = [
            by_id[source_id]
            for source_id in ordered_ids[: self.semantic_pool]
            if source_id in by_id
        ]
        seen = {source_id for _, source_id in ordered}
        return [*ordered, *(item for item in ranked if item[1] not in seen)]

    def _query_candidate_ids(self, query: Counter[str]) -> set[str]:
        return (
            set().union(*(self.term_to_ids.get(token, set()) for token in query))
            if query
            else set()
        )

    def _ranked_query(self, query_text: str) -> list[tuple[float, str]]:
        query = Counter({token: 4 for token in _tokens(query_text)})
        candidate_ids = self._query_candidate_ids(query)
        return sorted(
            (
                (score, source_id)
                for source_id in candidate_ids
                if (score := self._bm25_score(source_id, query)) > 0
            ),
            reverse=True,
        )

    def _with_context(self, score: float, source_id: str) -> list[tuple[float, str]]:
        result = [(score, source_id)]
        position = self.position[source_id]
        unit = self.units[source_id]
        for offset in (-2, -1, 1, 2):
            neighbor_position = position + offset
            if not 0 <= neighbor_position < len(self.ids):
                continue
            neighbor_id = self.ids[neighbor_position]
            neighbor = self.units[neighbor_id]
            same_page = _unit_page_id(neighbor) == _unit_page_id(unit)
            same_section = bool(
                unit.get("section_id")
                and unit.get("section_id") == neighbor.get("section_id")
            )
            same_table = bool(unit.get("table") and unit.get("table") == neighbor.get("table"))
            if same_page or same_section or same_table:
                result.append((max(0.0, score - 0.001), neighbor_id))
        parent_id = self.continuation_parent.get(source_id, source_id)
        continuation_group = [
            parent_id,
            *self.table_continuations.get(parent_id, [])[:12],
        ]
        for continuation_id in continuation_group:
            if continuation_id != source_id and not any(
                item[1] == continuation_id for item in result
            ):
                result.append((max(0.0, score - 0.0005), continuation_id))
        for replacement_id in self._replacement_context_ids(source_id):
            if replacement_id != source_id and not any(item[1] == replacement_id for item in result):
                result.append((score + 0.001, replacement_id))
        references_added = 0
        for reference in _unit_reference_keys(unit):
            candidates = sorted(
                self.reference_to_ids.get(reference, []),
                key=lambda candidate_id: (
                    self.units[candidate_id].get("source_role") == "post_base_update",
                    any(
                        cue in _unit_text(self.units[candidate_id]).casefold()
                        for cue in _EXPLICIT_UPDATE_CUES
                    ),
                    reference in _phrase_text(
                        " ".join(str(item) for item in self.units[candidate_id].get("section_path") or [])
                    ),
                ),
                reverse=True,
            )
            for candidate_id in candidates:
                if candidate_id == source_id or any(item[1] == candidate_id for item in result):
                    continue
                result.append((max(0.0, score - 0.002), candidate_id))
                candidate_position = self.position[candidate_id]
                candidate = self.units[candidate_id]
                for offset in (-1, 1):
                    neighbor_position = candidate_position + offset
                    if not 0 <= neighbor_position < len(self.ids):
                        continue
                    neighbor_id = self.ids[neighbor_position]
                    neighbor = self.units[neighbor_id]
                    if not (
                        candidate.get("table")
                        and candidate.get("table") == neighbor.get("table")
                    ):
                        continue
                    if not any(item[1] == neighbor_id for item in result):
                        result.append((max(0.0, score - 0.003), neighbor_id))
                references_added += 1
                if references_added >= 6:
                    return result
        return result


__all__ = ["FieldRetrievalScoring"]
