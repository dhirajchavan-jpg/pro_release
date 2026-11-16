"""Evidence and amendment retrieval lanes."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .field_core import (
    _EXPLICIT_UPDATE_CUES,
    _GATE_QUERY,
    _PROOF_QUERY,
    _UPDATE_QUERY,
    _amendment_source_binds_field,
    _compact_text,
    _direct_field_match,
    _direct_scalar_update_value,
    _field_presence_match,
    _field_subject_compatible,
    _field_target_match,
    _formal_revision_cells,
    _implicit_parallel_revision_cells,
    _is_formal_revision_row,
    _issuer_response_text,
    _needs_document_start_lane,
    _needs_gate_lane,
    _needs_proof_lane,
    _paired_nested_revision_cells,
    _phrase_text,
    _pointer_only_amendment_response,
    _scope_contains,
    _table_cells,
    _table_header_index,
    _tokens,
    _unit_document,
    _unit_page,
    _unit_page_id,
    _unit_text,
)

from .schema import (
    RfpFieldSpec,
    RfpSectionSpec,
    RfpUnderstandingSchema,
)


class FieldRetrievalSearch:
    """Methods for this retrieval responsibility."""

    def retrieve(
        self,
        schema: RfpUnderstandingSchema,
        section: RfpSectionSpec,
        field: RfpFieldSpec,
        *,
        limit: int,
        max_chars: int,
        extra_query: str = "",
        requested_source_ids: list[str] | None = None,
        requested_page_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        lane_query = self._query_weights(schema, section, field, extra_query)
        candidate_ids = self._query_candidate_ids(lane_query)
        # Identity labels such as "Bid Number" can consist entirely of retrieval
        # stop words.  Let configured bindings enter the candidate set before
        # BM25 ranking so the anchor score can do its job.
        candidate_ids.update(
            source_id
            for source_id, unit in self.units.items()
            if not unit.get("embedded_revision_fragment")
            and (
                _field_target_match(unit, field, self.formal_revision_sections)
                or self._anchor_match(unit, field)
            )
        )
        direct = sorted(
            (
                (1_000.0 + self._score(source_id, lane_query, field), source_id)
                for source_id in candidate_ids
                if not self.units[source_id].get("embedded_revision_fragment")
                and _field_target_match(
                    self.units[source_id], field, self.formal_revision_sections
                )
            ),
            reverse=True,
        )
        anchors = sorted(
            (
                (800.0 + self._score(source_id, lane_query, field), source_id)
                for source_id in candidate_ids
                if field.retrieval_guidance.anchor_phrases
                and self._anchor_match(self.units[source_id], field)
            ),
            reverse=True,
        )
        concepts = sorted(
            (
                (500.0 + score, source_id)
                for source_id in candidate_ids
                if (score := self._concept_score(source_id, field)) > 0
            ),
            reverse=True,
        )
        scalar_value_ids = {
            source_id
            for source_id in candidate_ids
            if _direct_scalar_update_value(
                field,
                {
                    **self.units[source_id],
                    "direct_field_match": _field_target_match(
                        self.units[source_id],
                        field,
                        self.formal_revision_sections,
                    ),
                },
            )
        }
        scalar_values = sorted(
            (
                (1_100.0 + self._score(source_id, lane_query, field), source_id)
                for source_id in scalar_value_ids
            ),
            reverse=True,
        )
        likely_sections = sorted(
            (
                (950.0 + self._score(source_id, lane_query, field), source_id)
                for source_id in self.ids
                if self._likely_section_match(self.units[source_id], field)
            ),
            reverse=True,
        )
        answer_shape_ids = {
            source_id
            for source_id in self.ids
            if self._answer_shape_match(self.units[source_id], field)
        }
        answer_shapes = sorted(
            (
                (1_000.0 + self._score(source_id, lane_query, field), source_id)
                for source_id in answer_shape_ids
            ),
            reverse=True,
        )
        positive = self._ranked(
            schema,
            section,
            field,
            extra_query,
            semantic=True,
        )
        targeted = self._ranked_query(extra_query) if extra_query.strip() else []
        facet_matches: dict[str, list[str]] = {}
        facet_lane: list[tuple[float, str]] = []
        configured_facets = list(
            dict.fromkeys(
                [
                    *field.retrieval_guidance.anchor_phrases,
                    *field.retrieval_guidance.aliases,
                    *field.retrieval_guidance.keywords,
                ]
            )
        )[:16]
        for facet in configured_facets:
            facet_tokens = set(_tokens(facet))
            if not facet_tokens:
                continue
            matched = 0
            for score, source_id in self._ranked_query(facet):
                unit = self.units[source_id]
                unit_tokens = set(self.term_frequencies[source_id])
                if (
                    unit.get("embedded_revision_fragment")
                    or len(facet_tokens & unit_tokens) / len(facet_tokens) < 0.75
                    or not _field_subject_compatible(
                        field,
                        " ".join(
                            [
                                _unit_text(unit),
                                *[str(item) for item in unit.get("section_path") or []],
                            ]
                        ),
                    )
                ):
                    continue
                facet_matches.setdefault(source_id, []).append(facet)
                if matched == 0:
                    facet_lane.append((900.0 + score, source_id))
                matched += 1
                if matched >= 8:
                    break

        def authority_diverse(
            items: list[tuple[float, str]],
            *,
            limit: int,
            per_document: int = 2,
        ) -> list[tuple[float, str]]:
            selected: list[tuple[float, str]] = []
            counts: Counter[str] = Counter()
            for item in items:
                document = _unit_document(self.units[item[1]])
                if counts[document] >= per_document:
                    continue
                selected.append(item)
                counts[document] += 1
                if len(selected) >= limit:
                    break
            return selected
        proof = self._ranked(schema, section, field, _PROOF_QUERY) if _needs_proof_lane(field) else []
        document_inventory = (
            self._ranked_query(
                "envelope technical bid pan gst acceptance letter debarment form"
            )
            if "mandatory bid document" in _phrase_text(f"{field.key} {field.label}")
            else []
        )
        gates = self._ranked_query(_GATE_QUERY) if _needs_gate_lane(field) else []
        best_update_by_document: dict[str, tuple[float, str]] = {}
        for item in positive:
            document = _unit_document(self.units[item[1]])
            if self.units[item[1]].get("source_role") != "post_base_update":
                continue
            current = best_update_by_document.get(document)
            unit = self.units[item[1]]
            item_rank = (
                not unit.get("embedded_revision_fragment")
                and _field_target_match(
                    unit, field, self.formal_revision_sections
                ),
                self._anchor_match(unit, field),
                bool(unit.get("formal_revision_scope")),
                item[0],
            )
            current_unit = self.units[current[1]] if current else None
            current_rank = (
                not current_unit.get("embedded_revision_fragment")
                and _field_target_match(
                    current_unit, field, self.formal_revision_sections
                ),
                self._anchor_match(current_unit, field),
                bool(current_unit.get("formal_revision_scope")),
                current[0],
            ) if current else None
            if current is None or item_rank > current_rank:
                best_update_by_document[document] = item
        updates = sorted(
            best_update_by_document.values(),
            key=lambda item: (
                self.document_authority.get(_unit_document(self.units[item[1]]), 0),
                bool(self.units[item[1]].get("formal_revision_scope")),
                item[0],
            ),
            reverse=True,
        )

        seeds: list[tuple[float, str]] = []
        for index, source_id in enumerate(requested_source_ids or []):
            if source_id in self.units:
                seeds.append((900.0 - index * 0.01, source_id))
        for index, page_id in enumerate(requested_page_ids or []):
            for source_id in self.page_to_ids.get(page_id, []):
                seeds.append((850.0 - index * 0.01, source_id))
        seed_ids = {source_id for _score, source_id in seeds}

        direct_count = max(1, limit - 4)
        if _needs_document_start_lane(section, field):
            # Identity fields must see the first page of every logical source.
            # A page should not need to win semantic ranking before it can
            # establish the issuer, title, or tender number.
            start_pool = sorted(
                (
                    (
                        self._score(source_id, lane_query, field),
                        source_id,
                    )
                    for source_id in self.document_start_ids
                    if not self.units[source_id].get("embedded_revision_fragment")
                ),
                key=lambda item: (
                    _unit_page(self.units[item[1]]),
                    -int(self.units[item[1]].get("authority_rank") or 0),
                    -item[0],
                    self.position.get(item[1], 0),
                ),
            )
            starts = []
            seen_logical_documents: set[str] = set()
            for item in start_pool:
                unit = self.units[item[1]]
                logical_document = str(
                    unit.get("logical_document_id") or _unit_document(unit) or "document"
                )
                if logical_document in seen_logical_documents:
                    continue
                starts.append(item)
                seen_logical_documents.add(logical_document)
                if len(starts) >= 4:
                    break
            if len(starts) < 4:
                starts.extend(item for item in start_pool if item not in starts[:4])
                starts = starts[:4]
        else:
            starts = []
        lanes = [
            [(2_000.0 + score, source_id) for score, source_id in starts],
            [
                (1_200.0 + score, source_id)
                for score, source_id in authority_diverse(
                    targeted,
                    limit=16,
                    per_document=3,
                )
            ],
            authority_diverse(scalar_values, limit=8),
            authority_diverse(direct, limit=12),
            authority_diverse(anchors, limit=12),
            authority_diverse(answer_shapes, limit=8, per_document=4),
            authority_diverse(likely_sections, limit=8, per_document=4),
            facet_lane,
            authority_diverse(concepts, limit=16, per_document=3),
            positive[:direct_count],
            [(700.0 + score, source_id) for score, source_id in updates[:8]],
            proof[:2],
            authority_diverse(document_inventory, limit=12, per_document=4),
            gates[:2],
        ]
        lanes = [lane for lane in lanes if lane]
        selected = [*seeds]
        for index in range(max((len(lane) for lane in lanes), default=0)):
            for lane in lanes:
                if index < len(lane):
                    selected.append(lane[index])
        for item in positive:
            if len(selected) >= len(seeds) + max(1, limit):
                break
            if item not in selected:
                selected.append(item)
        roles = {
            str(self.units[source_id].get("source_role") or "")
            for _score, source_id in selected
        }
        for wanted_role in ("base_rfp", "post_base_update"):
            if wanted_role not in roles:
                candidate = next(
                    (
                        item
                        for item in positive
                        if self.units[item[1]].get("source_role") == wanted_role
                    ),
                    None,
                )
                if candidate:
                    selected.append(candidate)
        primary_ids = {source_id for _score, source_id in selected}

        expanded: list[tuple[float, str]] = []
        seen: set[str] = set()
        for score, source_id in selected:
            if source_id not in seen:
                seen.add(source_id)
                expanded.append((score, source_id))
        for score, source_id in selected:
            for candidate_score, candidate_id in self._with_context(score, source_id)[1:]:
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                expanded.append((candidate_score, candidate_id))
        expanded.sort(
            key=lambda item: (
                item[1] in primary_ids,
                self.units[item[1]].get("effective_state") != "superseded",
                item[0],
                int(self.units[item[1]].get("authority_rank") or 0),
            ),
            reverse=True,
        )

        evidence: list[dict[str, Any]] = []
        used_chars = 0
        for score, source_id in expanded:
            unit = self.units[source_id]
            if not _field_subject_compatible(field, _unit_text(unit)):
                continue
            text = _compact_text(_unit_text(unit))
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            text = text[: min(2500, remaining)]
            if not text:
                continue
            item = {
                "source_id": source_id,
                "document_id": _unit_document(unit),
                "page": _unit_page(unit),
                "page_id": _unit_page_id(unit),
                "unit_type": str(unit.get("unit_type") or "text"),
                "section_id": str(unit.get("section_id") or ""),
                "table": str(unit.get("table") or ""),
                "section_path": [str(item) for item in unit.get("section_path") or []],
                "table_headers": [str(item) for item in unit.get("table_headers") or []],
                "clause_number": str(unit.get("clause_number") or ""),
                "source_role": str(unit.get("source_role") or "unknown"),
                "source_file": str(unit.get("source_file") or ""),
                "revision_sequence": unit.get("revision_sequence"),
                "document_order": unit.get("document_order"),
                "authority_rank": unit.get("authority_rank"),
                "chronology_basis": unit.get("chronology_basis"),
                "effective_state": unit.get("effective_state"),
                "superseded_by_document_id": unit.get("superseded_by_document_id"),
                "superseded_by_scope": unit.get("superseded_by_scope"),
                "section_scope": unit.get("section_scope"),
                "replacement_scope": bool(unit.get("replacement_scope")),
                "formal_revision": _is_formal_revision_row(
                    unit, self.formal_revision_sections
                ),
                "formal_revision_scope": bool(unit.get("formal_revision_scope")),
                "embedded_revision_fragment": bool(
                    unit.get("embedded_revision_fragment")
                ),
                "anchor_match": self._anchor_match(unit, field),
                "likely_section_match": self._likely_section_match(unit, field),
                "answer_shape_match": source_id in answer_shape_ids,
                "scalar_value_match": source_id in scalar_value_ids,
                "direct_field_match": bool(
                    not unit.get("embedded_revision_fragment")
                    and _field_target_match(
                        unit, field, self.formal_revision_sections
                    )
                ),
                "facet_match": source_id in facet_matches,
                "facet_matches": facet_matches.get(source_id, []),
                "retrieval_score": round(score, 6),
                "text": text,
            }
            continuation_ids = self.table_continuations.get(source_id, [])
            if continuation_ids:
                item["table_continuation_source_ids"] = continuation_ids
                item["table_continuation_text"] = _compact_text(
                    " ".join(_unit_text(self.units[item_id]) for item_id in continuation_ids)
                )
            cells = _table_cells(unit)
            if cells:
                item["table_cells"] = cells
                revision = _formal_revision_cells(item)
                revision_source_ids: list[str] = []
                if not revision and continuation_ids:
                    nested_revision = _paired_nested_revision_cells(
                        unit,
                        [
                            {**self.units[continuation_id], "source_id": continuation_id}
                            for continuation_id in continuation_ids
                        ],
                    )
                    if nested_revision:
                        revision = nested_revision[:2]
                        revision_source_ids = nested_revision[2]
                        item["formal_revision"] = True
                        item["revision_layout"] = "paired_nested_rows"
                        target_text = " | ".join(
                            [
                                *cells[:3],
                                revision[0],
                                revision[1],
                            ]
                        )
                        item["direct_field_match"] = _direct_field_match(
                            {
                                "text": target_text,
                                "section_path": item.get("section_path") or [],
                                "table_headers": [],
                            },
                            field,
                        )
                if revision:
                    implicit_revision = _implicit_parallel_revision_cells(item)
                    item["formal_revision"] = True
                    item["existing_text"] = revision[0]
                    item["revised_text"] = revision[1]
                    item["bidder_request_or_prior_text"] = revision[0]
                    item["issuer_response_or_revised_text"] = revision[1]
                    if revision_source_ids:
                        item["revision_source_ids"] = revision_source_ids
                    else:
                        labelled_nested = (
                            len(cells) != len(item.get("table_headers") or [])
                            and _table_header_index(
                                item.get("table_headers") or [], "existing", "current clause"
                            )
                            is not None
                            and _table_header_index(
                                item.get("table_headers") or [],
                                "revised",
                                "modified clause",
                                "amended clause",
                            )
                            is not None
                        )
                        item["revision_layout"] = (
                            "parallel_nested_columns"
                            if labelled_nested
                            else "implicit_parallel_columns"
                            if implicit_revision
                            else "aligned_columns"
                            if len(cells) == len(item.get("table_headers") or [])
                            else "parallel_nested_columns"
                        )
                elif item["source_role"] == "post_base_update":
                    if item.get("formal_revision_scope"):
                        item["revision_layout"] = "flattened_or_continued"
                    headers = item.get("table_headers") or []
                    response_index = _table_header_index(
                        headers,
                        "issuer response",
                        "bank response",
                        "client response",
                        "response",
                        "amendment",
                    )
                    request_index = _table_header_index(headers, "bidder query", "bidder request", "query", "request")
                    if response_index is not None and response_index < len(cells):
                        response = _compact_text(
                            " ".join(
                                filter(
                                    None,
                                    [cells[response_index], item.get("table_continuation_text", "")],
                                )
                            )
                        )
                        item["issuer_response"] = response
                        item["issuer_response_or_revised_text"] = response
                    elif response_index is not None and len(cells) >= 2:
                        response = _compact_text(
                            " ".join(
                                filter(
                                    None,
                                    [cells[-1], item.get("table_continuation_text", "")],
                                )
                            )
                        )
                        item["issuer_response"] = response
                        item["issuer_response_or_revised_text"] = response
                    if request_index is not None and request_index < len(cells):
                        item["bidder_request"] = cells[request_index]
                        item["bidder_request_or_prior_text"] = cells[request_index]
                    elif (
                        request_index is not None
                        and response_index is not None
                        and response_index >= len(cells)
                        and len(cells) >= 3
                    ):
                        item["bidder_request"] = cells[-2]
                        item["bidder_request_or_prior_text"] = cells[-2]
            evidence.append(item)
            used_chars += len(text) + len(str(item.get("table_continuation_text") or ""))
        return evidence

    def retrieve_amendment_evidence(
        self,
        schema: RfpUnderstandingSchema,
        section: RfpSectionSpec,
        field: RfpFieldSpec,
        *,
        limit: int = 32,
        max_chars: int = 18000,
        extra_query: str = "",
    ) -> list[dict[str, Any]]:
        """Return a compact chronological field neighborhood for amendment compilation."""

        amendment_query = self._query_weights(
            schema,
            section,
            field,
            f"{_UPDATE_QUERY} {extra_query}".strip(),
        )
        targeted_query = Counter({token: 4 for token in _tokens(extra_query)})

        def local_relevance(source_id: str) -> float:
            unit = self.units[source_id]
            local_score = self._score(source_id, amendment_query, field)
            concept_score = self._concept_score(source_id, field)
            if (
                not unit.get("embedded_revision_fragment")
                and _field_target_match(unit, field, self.formal_revision_sections)
            ):
                local_score += 1_000.0
            elif field.retrieval_guidance.anchor_phrases and self._anchor_match(unit, field):
                local_score += 800.0
            local_score = max(
                local_score,
                500.0 + concept_score if concept_score else 0.0,
            )
            issuer_response = _phrase_text(_issuer_response_text(unit))
            if (
                concept_score
                and issuer_response
                and not _pointer_only_amendment_response(issuer_response)
                and any(cue in issuer_response for cue in _EXPLICIT_UPDATE_CUES)
            ):
                local_score = max(local_score, 1_600.0 + concept_score)
            if targeted_query:
                targeted_score = self._bm25_score(source_id, targeted_query)
                if targeted_score > 0:
                    local_score = max(local_score, 1_200.0 + targeted_score)
            return local_score

        # Amendment compilation must see relevant historical rows. Seed them before
        # the general retriever applies its current-state display preference.
        ranked_seeds = sorted(
            (
                (local_relevance(source_id), source_id)
                for source_id, unit in self.units.items()
                if unit.get("source_role") == "post_base_update"
            ),
            reverse=True,
        )
        replacement_marker_seed_ids: list[str] = []
        for scope, replacement_document in self.latest_replacement_document.items():
            replacement_rank = self.document_authority.get(replacement_document, 0)
            field_existed_in_scope = any(
                _scope_contains(scope, str(unit.get("section_scope") or ""))
                and self.document_authority.get(_unit_document(unit), 0) < replacement_rank
                and _field_presence_match(unit, field)
                for unit in self.units.values()
            )
            if not field_existed_in_scope:
                continue
            replacement_marker_seed_ids.extend(
                self.replacement_marker_ids.get((scope, replacement_document), [])
            )
        amendment_seed_ids = list(dict.fromkeys(replacement_marker_seed_ids))
        seen_seed_pages: set[tuple[str, str]] = {
            (
                _unit_document(self.units[source_id]),
                _unit_page_id(self.units[source_id]),
            )
            for source_id in amendment_seed_ids
        }
        for score, source_id in ranked_seeds:
            if score <= 0:
                break
            unit = self.units[source_id]
            page_key = (_unit_document(unit), _unit_page_id(unit))
            if page_key in seen_seed_pages:
                continue
            amendment_seed_ids.append(source_id)
            seen_seed_pages.add(page_key)
            if len(amendment_seed_ids) >= 40:
                break
        parent_by_continuation = {
            continuation_id: parent_id
            for parent_id, continuation_ids in self.table_continuations.items()
            for continuation_id in continuation_ids
        }
        structural_parent_seed_ids = list(
            dict.fromkeys(
                parent_by_continuation[source_id]
                for source_id in amendment_seed_ids
                if source_id in parent_by_continuation
            )
        )
        amendment_seed_ids = list(
            dict.fromkeys([*structural_parent_seed_ids, *amendment_seed_ids])
        )
        amendment_seed_ids.extend(
            source_id
            for _score, source_id in self._ranked(schema, section, field, "")
            if self.units[source_id].get("source_role") == "base_rfp"
            and source_id not in amendment_seed_ids
            and len(amendment_seed_ids) < 48
        )

        evidence = self.retrieve(
            schema,
            section,
            field,
            limit=max(48, limit),
            max_chars=max(140000, max_chars * 8),
            extra_query=extra_query.strip() or _UPDATE_QUERY,
            requested_source_ids=amendment_seed_ids,
        )
        updates = [
            item
            for item in evidence
            if item.get("source_role") == "post_base_update"
        ]
        if not updates:
            return []
        for item in updates:
            source_id = str(item.get("source_id") or "")
            item["amendment_relevance_score"] = round(local_relevance(source_id), 6)
        top_score = max(float(item.get("amendment_relevance_score") or 0) for item in updates)
        score_floor = max(12.0, top_score * 0.45)
        has_anchors = bool(field.retrieval_guidance.anchor_phrases)
        for item in updates:
            source_id = str(item.get("source_id") or "")
            item["amendment_candidate"] = bool(
                _amendment_source_binds_field(field, item, self)
                or (
                    float(item.get("amendment_relevance_score") or 0) >= score_floor
                    and self._concept_score(source_id, field) > 0
                )
            )
        primary = [
            item
            for item in updates
            if item.get("direct_field_match")
            or (has_anchors and item.get("anchor_match"))
            or item.get("formal_revision")
            or item.get("formal_revision_scope")
            or float(item.get("amendment_relevance_score") or 0) >= score_floor
        ]
        primary.sort(
            key=lambda item: (
                float(item.get("amendment_relevance_score") or 0),
                bool(item.get("direct_field_match")),
                bool(has_anchors and item.get("anchor_match")),
                bool(item.get("formal_revision")),
            ),
            reverse=True,
        )
        page_primary: list[dict[str, Any]] = []
        primary_per_page: Counter[tuple[str, str]] = Counter()
        for item in primary:
            page_key = (
                str(item.get("document_id") or ""),
                str(item.get("page_id") or item.get("page") or ""),
            )
            aligned = bool(
                item.get("amendment_candidate")
                or item.get("direct_field_match")
                or (has_anchors and item.get("anchor_match"))
            )
            if primary_per_page[page_key] >= (3 if aligned else 1):
                continue
            primary_per_page[page_key] += 1
            page_primary.append(item)
        page_primary_ids = {
            str(item.get("source_id") or "") for item in page_primary
        }
        scope_markers = [
            item
            for item in updates
            if str(item.get("source_id") or "") in replacement_marker_seed_ids
        ]
        marker_ids = {
            str(item.get("source_id") or "") for item in scope_markers
        }
        structural_parents = [
            item
            for item in updates
            if str(item.get("source_id") or "") in structural_parent_seed_ids
        ]
        structural_parent_ids = {
            str(item.get("source_id") or "") for item in structural_parents
        }
        primary = [
            *page_primary,
            *[
                item
                for item in scope_markers
                if str(item.get("source_id") or "") not in page_primary_ids
            ],
            *[
                item
                for item in structural_parents
                if str(item.get("source_id") or "")
                not in marker_ids | page_primary_ids
            ],
        ]
        top_primary = primary[:12]
        represented_documents = {
            str(item.get("document_id") or "") for item in top_primary
        }
        best_by_document: dict[str, dict[str, Any]] = {}
        for item in primary:
            document = str(item.get("document_id") or "")
            if document and document not in best_by_document:
                best_by_document[document] = item
        for item in sorted(
            best_by_document.values(),
            key=lambda candidate: int(candidate.get("authority_rank") or 0),
            reverse=True,
        ):
            document = str(item.get("document_id") or "")
            if document in represented_documents:
                continue
            top_primary.append(item)
            represented_documents.add(document)
            if len(top_primary) >= 16:
                break
        primary = top_primary[:16]
        primary.sort(
            key=lambda item: float(item.get("amendment_relevance_score") or 0),
            reverse=True,
        )
        if not primary:
            return []
        primary_ids = {str(item.get("source_id") or "") for item in primary}
        primary_pages = {str(item.get("page_id") or "") for item in primary}
        primary_tables = {
            str(self.units[source_id].get("table") or "")
            for source_id in primary_ids
            if source_id in self.units and self.units[source_id].get("table")
        }
        context = [
            item
            for item in updates
            if item.get("source_id") not in primary_ids
            and (
                str(item.get("page_id") or "") in primary_pages
                or str(self.units.get(str(item.get("source_id") or ""), {}).get("table") or "")
                in primary_tables
            )
        ][:8]
        adjacent_page_ids = set(
            self.adjacent_page_ids(
                [str(item.get("page_id") or "") for item in primary[:10]],
                1,
            )
        ) - primary_pages
        best_adjacent_by_page: dict[str, dict[str, Any]] = {}
        for item in sorted(
            updates,
            key=lambda candidate: float(
                candidate.get("amendment_relevance_score") or 0
            ),
            reverse=True,
        ):
            page_id = str(item.get("page_id") or "")
            if (
                page_id in adjacent_page_ids
                and page_id not in best_adjacent_by_page
                and str(item.get("source_id") or "") not in primary_ids
            ):
                best_adjacent_by_page[page_id] = item
        adjacent = list(best_adjacent_by_page.values())[:8]
        base = [
            item
            for item in evidence
            if item.get("source_role") == "base_rfp"
        ]
        base_query = self._query_weights(schema, section, field, "")
        base.sort(
            key=lambda item: self._score(
                str(item.get("source_id") or ""),
                base_query,
                field,
            ),
            reverse=True,
        )
        base = base[:6]
        # Keep the relevance ordering while enforcing the character budget. Sorting by
        # chronology first systematically discarded late, controlling corrigendum rows.
        adjacent_by_document_page = {
            (str(item.get("document_id") or ""), int(item.get("page") or 0)): item
            for item in adjacent
        }
        selected: list[dict[str, Any]] = []
        used_adjacent_ids: set[str] = set()
        for item in primary:
            selected.append(item)
            document = str(item.get("document_id") or "")
            page = int(item.get("page") or 0)
            for neighbor_page in (page - 1, page + 1):
                neighbor = adjacent_by_document_page.get((document, neighbor_page))
                neighbor_id = str((neighbor or {}).get("source_id") or "")
                if neighbor and neighbor_id not in used_adjacent_ids:
                    selected.append(neighbor)
                    used_adjacent_ids.add(neighbor_id)
        selected.extend([*context, *base])
        compact: list[dict[str, Any]] = []
        used_chars = 0
        seen: set[str] = set()
        for item in selected:
            source_id = str(item.get("source_id") or "")
            if not source_id or source_id in seen:
                continue
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            copy = dict(item)
            copy["text"] = str(copy.get("text") or "")[: min(1800, remaining)]
            if copy.get("table_continuation_text"):
                copy["table_continuation_text"] = str(copy["table_continuation_text"])[
                    : min(1200, max(0, remaining - len(copy["text"])))
                ]
            used_chars += len(copy["text"]) + len(
                str(copy.get("table_continuation_text") or "")
            )
            compact.append(copy)
            seen.add(source_id)
        compact.sort(
            key=lambda item: (
                int(item.get("authority_rank") or 0),
                int(item.get("page") or 0),
                self.position.get(str(item.get("source_id") or ""), 0),
            )
        )
        return compact


__all__ = ["FieldRetrievalSearch"]
