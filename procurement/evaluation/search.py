"""Deterministic, provider-neutral progressive search planning.

This module deliberately owns no retrieval or model clients.  Callers may
inject local rankers/verifiers, while the planner keeps the search order,
coverage accounting, escalation rules, and budgets deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import inspect
import json
import re
from functools import lru_cache
from typing import Any


__all__ = [
    "CriterionSpec",
    "ProviderCapabilities",
    "SearchBudget",
    "SearchCoverage",
    "SearchHit",
    "SearchPlan",
    "SearchStage",
    "SearchStatus",
    "SearchUnit",
    "StageExecution",
    "ProgressiveCriterionSearchPlanner",
    "build_search_cache_key",
]


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_VISUAL_WORDS = frozenset(
    {
        "certificate",
        "image",
        "photograph",
        "seal",
        "signature",
        "stamp",
        "table",
        "visual",
    }
)
_BROAD_WORDS = frozenset(
    {
        "capacity",
        "experience",
        "facility",
        "history",
        "implementation",
        "maintenance",
        "manufacturing",
        "performance",
        "project",
        "projects",
        "similar",
        "works",
    }
)


class SearchStatus(str, Enum):
    """Terminal state of a search, never a compliance decision."""

    SUFFICIENT = "sufficient"
    EXHAUSTED = "exhausted"
    SEARCH_INCOMPLETE = "search_incomplete"


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Mapping):
        value = value.keys()
    return tuple(sorted({str(item).strip() for item in value if str(item).strip()}, key=str.casefold))


@lru_cache(maxsize=8192)
def _text_tokens(value: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _TOKEN_RE.findall(value or ""))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint_units(units: Sequence["SearchUnit"]) -> str:
    payload = [
        {
            "unit_id": unit.unit_id,
            "document_id": unit.document_id,
            "page_number": unit.page_number,
            "chunk_index": unit.chunk_index,
            "text": unit.text,
            "labels": unit.labels,
            "has_image": unit.has_image,
        }
        for unit in sorted(units, key=lambda item: item.sort_key)
    ]
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def build_search_cache_key(
    *,
    source_hash: str,
    extraction_generation: str,
    provider: str,
    model: str,
    ontology_version: str,
    proof_schema_version: str,
    criterion_id: str = "",
) -> str:
    """Build a content/configuration-sensitive cache key.

    A changed source, extraction, provider/model, document ontology, or proof
    contract must never reuse an old retrieval or visual-verification result.
    """

    payload = {
        "schema": "progressive-criterion-search.v1",
        "source_hash": str(source_hash),
        "extraction_generation": str(extraction_generation),
        "provider": str(provider),
        "model": str(model),
        "ontology_version": str(ontology_version),
        "proof_schema_version": str(proof_schema_version),
        "criterion_id": str(criterion_id),
    }
    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return f"criterion-search:v1:{digest}"


@dataclass(frozen=True, slots=True)
class CriterionSpec:
    """The search-relevant part of an approved criterion contract."""

    criterion_id: str
    requirement: str
    objective: str = "standard"
    exact_terms: tuple[str, ...] = ()
    classification_hints: tuple[str, ...] = ()
    artifact_labels: tuple[str, ...] = ()
    broad: bool = False
    visual: bool = False
    multi_document: bool = False
    min_evidence: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "criterion_id", str(self.criterion_id).strip())
        object.__setattr__(self, "requirement", str(self.requirement).strip())
        object.__setattr__(self, "objective", str(self.objective).strip().casefold() or "standard")
        object.__setattr__(self, "exact_terms", _tuple(self.exact_terms))
        object.__setattr__(self, "classification_hints", _tuple(self.classification_hints))
        object.__setattr__(self, "artifact_labels", _tuple(self.artifact_labels))
        object.__setattr__(self, "min_evidence", max(1, int(self.min_evidence)))

    @property
    def query(self) -> str:
        parts = (self.requirement, *self.exact_terms, *self.artifact_labels)
        return " ".join(part for part in parts if part).strip()

    @property
    def needs_exact_first(self) -> bool:
        return bool(self.exact_terms) or self.objective in {"exact", "identifier", "statutory"}

    @property
    def exact_objective(self) -> bool:
        """Whether one grounded exact value can complete this search.

        Exact identifiers are presence questions.  Broad, multi-document, and
        visual criteria still need their existing wider-search guarantees.
        """

        return (
            self.needs_exact_first
            and bool(self.exact_terms)
            and not self.broad
            and not self.multi_document
            and not self.visual
        )

    @classmethod
    def from_mapping(cls, value: "CriterionSpec | Mapping[str, Any]") -> "CriterionSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("criterion must be CriterionSpec or a mapping")

        requirement = str(value.get("requirement", value.get("title", "")))
        objective = str(value.get("objective", value.get("search_objective", "standard"))).casefold()
        tokens = set(_text_tokens(requirement))
        visual = bool(value.get("visual", value.get("requires_visual", False)))
        visual = visual or objective in {"visual", "image", "multimodal"} or bool(tokens & _VISUAL_WORDS)
        broad = bool(value.get("broad", False))
        broad = broad or objective in {"broad", "multi_document", "experience", "capability"}
        broad = broad or bool(tokens & _BROAD_WORDS)
        multi_document = bool(value.get("multi_document", False)) or objective == "multi_document"
        multi_document = multi_document or broad
        return cls(
            criterion_id=str(value.get("criterion_id", value.get("id", ""))),
            requirement=requirement,
            objective=objective,
            exact_terms=_tuple(value.get("exact_terms", value.get("exact_values"))),
            classification_hints=_tuple(
                value.get("classification_hints", value.get("route_hints", value.get("classification")))
            ),
            artifact_labels=_tuple(value.get("artifact_labels", value.get("document_labels"))),
            broad=broad,
            visual=visual,
            multi_document=multi_document,
            min_evidence=int(value.get("min_evidence", 2 if multi_document else 1)),
        )


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Declared capabilities; no network/provider call is made here."""

    embeddings: bool = True
    reranker: bool = True
    image_embeddings: bool = True
    vlm: bool = True

    @classmethod
    def from_mapping(cls, value: "ProviderCapabilities | Mapping[str, Any] | None") -> "ProviderCapabilities":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        return cls(
            embeddings=bool(value.get("embeddings", value.get("embedding", False))),
            reranker=bool(value.get("reranker", value.get("rerank", False))),
            image_embeddings=bool(value.get("image_embeddings", value.get("image_embedding", False))),
            vlm=bool(value.get("vlm", value.get("vision_language_model", False))),
        )


@dataclass(frozen=True, slots=True)
class SearchBudget:
    """Hard ceilings for a single criterion; every loop is bounded by these."""

    max_stages: int = 9
    max_candidates_per_stage: int = 64
    max_unseen_pages: int = 128
    max_image_pages: int = 32
    max_vlm_pages: int = 8
    max_total_candidates: int = 256

    def __post_init__(self) -> None:
        for name in (
            "max_stages",
            "max_candidates_per_stage",
            "max_unseen_pages",
            "max_image_pages",
            "max_vlm_pages",
            "max_total_candidates",
        ):
            object.__setattr__(self, name, max(1, int(getattr(self, name))))


@dataclass(frozen=True, slots=True)
class SearchUnit:
    """A searchable text chunk/page with optional visual metadata."""

    unit_id: str
    document_id: str
    page_number: int
    text: str = ""
    labels: tuple[str, ...] = ()
    chunk_index: int = 0
    has_image: bool = False
    image_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit_id", str(self.unit_id))
        object.__setattr__(self, "document_id", str(self.document_id))
        object.__setattr__(self, "page_number", max(1, int(self.page_number)))
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "labels", _tuple(self.labels))
        object.__setattr__(self, "chunk_index", max(0, int(self.chunk_index)))

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (self.document_id.casefold(), self.page_number, self.chunk_index, self.unit_id.casefold())

    @classmethod
    def from_mapping(cls, value: "SearchUnit | Mapping[str, Any]") -> "SearchUnit":
        if isinstance(value, cls):
            return value
        return cls(
            unit_id=str(value.get("unit_id", value.get("page_id", value.get("id", "")))),
            document_id=str(value.get("document_id", value.get("document", ""))),
            page_number=int(value.get("page_number", value.get("page", 1))),
            text=str(value.get("text", value.get("content", ""))),
            labels=_tuple(value.get("labels", value.get("classification", value.get("document_labels")))),
            chunk_index=int(value.get("chunk_index", value.get("chunk", 0))),
            has_image=bool(value.get("has_image", value.get("image_id"))),
            image_id=str(value.get("image_id", "")),
        )


@dataclass(frozen=True, slots=True)
class SearchStage:
    stage_id: str
    mode: str
    radius: int
    candidate_limit: int
    scope: str
    enabled: bool = True
    provider: str = ""
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class SearchPlan:
    criterion: CriterionSpec
    stages: tuple[SearchStage, ...]
    radius_sequence: tuple[int, ...]
    cache_key: str
    route_hint_only: bool = True

    @property
    def instructions(self) -> tuple[str, ...]:
        return tuple(stage.instruction for stage in self.stages if stage.instruction)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion.criterion_id,
            "objective": self.criterion.objective,
            "cache_key": self.cache_key,
            "route_hint_only": self.route_hint_only,
            "radius_sequence": list(self.radius_sequence),
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "mode": stage.mode,
                    "radius": stage.radius,
                    "candidate_limit": stage.candidate_limit,
                    "scope": stage.scope,
                    "enabled": stage.enabled,
                    "provider": stage.provider,
                    "instruction": stage.instruction,
                }
                for stage in self.stages
            ],
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    unit_id: str
    document_id: str
    page_number: int
    score: float
    stages: tuple[str, ...] = ()
    visual_verified: bool = False


@dataclass(frozen=True, slots=True)
class StageExecution:
    stage_id: str
    mode: str
    enabled: bool
    inspected_count: int
    inspected_unit_ids: tuple[str, ...] = ()
    returned_unit_ids: tuple[str, ...] = ()
    degraded_reason: str = ""


@dataclass(frozen=True, slots=True)
class SearchCoverage:
    units_total: int
    units_inspected: int
    pages_total: int
    pages_inspected: int
    documents_total: int
    documents_inspected: int
    route_hint_units: int
    unseen_pages_recovered: int
    top_k_exhausted: bool
    full_corpus_searched: bool
    inspected_unit_ids: tuple[str, ...] = ()
    remaining_unit_ids: tuple[str, ...] = ()
    degraded_providers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "units_total": self.units_total,
            "units_inspected": self.units_inspected,
            "pages_total": self.pages_total,
            "pages_inspected": self.pages_inspected,
            "documents_total": self.documents_total,
            "documents_inspected": self.documents_inspected,
            "route_hint_units": self.route_hint_units,
            "unseen_pages_recovered": self.unseen_pages_recovered,
            "top_k_exhausted": self.top_k_exhausted,
            "full_corpus_searched": self.full_corpus_searched,
            "inspected_unit_ids": list(self.inspected_unit_ids),
            "remaining_unit_ids": list(self.remaining_unit_ids),
            "degraded_providers": list(self.degraded_providers),
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    status: SearchStatus
    hits: tuple[SearchHit, ...]
    coverage: SearchCoverage
    stages: tuple[StageExecution, ...]
    degradations: tuple[str, ...] = ()
    escalation_instructions: tuple[str, ...] = ()

    @property
    def matched_unit_ids(self) -> tuple[str, ...]:
        return tuple(hit.unit_id for hit in self.hits)


Ranker = Callable[..., Sequence[Any]]
VisualVerifier = Callable[..., Any]


class ProgressiveCriterionSearchPlanner:
    """Build and optionally execute a bounded progressive search plan."""

    def __init__(
        self,
        budget: SearchBudget | None = None,
        *,
        capabilities: ProviderCapabilities | Mapping[str, Any] | None = None,
    ) -> None:
        self.budget = budget or SearchBudget()
        self.capabilities = ProviderCapabilities.from_mapping(capabilities)

    def _radii(self, criterion: CriterionSpec) -> tuple[int, ...]:
        if criterion.broad or criterion.multi_document:
            values = (16, 48, 96, 192)
        elif criterion.visual:
            values = (8, 24, 64, 128)
        elif criterion.needs_exact_first:
            values = (4, 16, 64, 128)
        else:
            values = (8, 24, 64, 128)
        return tuple(dict.fromkeys(min(value, self.budget.max_total_candidates) for value in values))

    def plan(
        self,
        criterion: CriterionSpec | Mapping[str, Any],
        *,
        source_hash: str = "",
        extraction_generation: str = "",
        provider: str = "",
        model: str = "",
        ontology_version: str = "",
        proof_schema_version: str = "",
        capabilities: ProviderCapabilities | Mapping[str, Any] | None = None,
    ) -> SearchPlan:
        spec = CriterionSpec.from_mapping(criterion)
        caps = ProviderCapabilities.from_mapping(capabilities) if capabilities is not None else self.capabilities
        radii = self._radii(spec)
        stages: list[SearchStage] = []

        def add(
            stage_id: str,
            mode: str,
            radius: int,
            *,
            scope: str = "all_pages",
            enabled: bool = True,
            required_provider: str = "",
            instruction: str = "",
        ) -> None:
            stages.append(
                SearchStage(
                    stage_id=stage_id,
                    mode=mode,
                    radius=max(1, radius),
                    candidate_limit=min(max(1, radius), self.budget.max_candidates_per_stage),
                    scope=scope,
                    enabled=enabled,
                    provider=required_provider,
                    instruction=instruction,
                )
            )

        if spec.needs_exact_first:
            add(
                "01_exact_scan",
                "exact",
                self.budget.max_total_candidates,
                scope="all_pages",
                instruction=(
                    "Search exact identifiers/values until every required term is grounded; "
                    "scan the complete corpus only when absence must be established."
                ),
            )
        add(
            "02_lexical_hint",
            "lexical",
            radii[0],
            scope="route_hint_plus_fallback",
            instruction="Use classification and multi-label routes as ranking hints only; never exclude other pages.",
        )
        add(
            "03_semantic_union",
            "semantic_union",
            radii[1] if len(radii) > 1 else radii[0],
            enabled=caps.embeddings,
            required_provider="embeddings",
            instruction="Union semantic candidates with exact/lexical candidates; semantic retrieval is not proof.",
        )
        add(
            "04_rerank_union",
            "rerank_union",
            radii[1] if len(radii) > 1 else radii[0],
            enabled=caps.reranker,
            required_provider="reranker",
            instruction="Rerank the union without discarding lexical or exact candidates when the provider degrades.",
        )
        if len(radii) > 2:
            add(
                "05_expand_radius",
                "lexical_expand",
                radii[2],
                scope="all_pages",
                instruction="Expand beyond the initial route/top-k radius before concluding that evidence is absent.",
            )
        if spec.visual:
            add(
                "06_image_embedding",
                "image_embedding",
                radii[2] if len(radii) > 2 else radii[-1],
                enabled=caps.image_embeddings,
                required_provider="image_embeddings",
                instruction="Use image embeddings to retrieve visually similar pages; similarity alone is not proof.",
            )
            add(
                "07_vlm_escalation",
                "vlm",
                self.budget.max_vlm_pages,
                enabled=caps.vlm,
                required_provider="vlm",
                instruction="Escalate bounded candidate page images to the VLM for signatures, seals, tables, and certificate fields.",
            )
        if len(radii) > 3:
            add(
                "08_expand_radius_final",
                "lexical_expand",
                radii[3],
                scope="all_pages",
                instruction="Use the final bounded radius for broad or multi-document requirements.",
            )
        add(
            "09_unseen_page_recovery",
            "unseen_recovery",
            self.budget.max_unseen_pages,
            scope="unseen_pages",
            instruction="Inspect unseen pages in stable document/page order; top-k exhaustion is not a missing result.",
        )

        stages = stages[: self.budget.max_stages]
        cache = build_search_cache_key(
            source_hash=source_hash,
            extraction_generation=extraction_generation,
            provider=provider,
            model=model,
            ontology_version=ontology_version,
            proof_schema_version=proof_schema_version,
            criterion_id=spec.criterion_id,
        )
        return SearchPlan(spec, tuple(stages), radii, cache)

    def search(
        self,
        criterion: CriterionSpec | Mapping[str, Any],
        units: Iterable[SearchUnit | Mapping[str, Any]],
        *,
        source_hash: str = "",
        extraction_generation: str = "",
        provider: str = "",
        model: str = "",
        ontology_version: str = "",
        proof_schema_version: str = "",
        semantic_ranker: Ranker | None = None,
        reranker: Ranker | None = None,
        image_ranker: Ranker | None = None,
        vlm_verifier: VisualVerifier | None = None,
    ) -> SearchResult:
        """Execute only supplied pure callbacks; never calls a provider itself."""

        by_id: dict[str, SearchUnit] = {}
        for raw in units:
            unit = SearchUnit.from_mapping(raw)
            by_id.setdefault(unit.unit_id, unit)
        ordered = tuple(sorted(by_id.values(), key=lambda unit: unit.sort_key))
        plan = self.plan(
            criterion,
            source_hash=source_hash or _fingerprint_units(ordered),
            extraction_generation=extraction_generation,
            provider=provider,
            model=model,
            ontology_version=ontology_version,
            proof_schema_version=proof_schema_version,
        )
        spec = plan.criterion
        lookup = {unit.unit_id: unit for unit in ordered}
        hinted = {
            unit.unit_id
            for unit in ordered
            if spec.classification_hints
            and {label.casefold() for label in unit.labels}
            & {label.casefold() for label in spec.classification_hints}
        }

        inspected: set[str] = set()
        hits: dict[str, SearchHit] = {}
        stages: list[StageExecution] = []
        degradations: list[str] = []
        top_k_exhausted = False
        visual_verified: set[str] = set()
        exact_terms_verified: set[str] = set()
        exact_proof_ids: set[str] = set()
        all_ranked_lexically = self._lexical_rank(spec, ordered, hinted)

        def mark_inspected(ids: Iterable[str]) -> None:
            inspected.update(unit_id for unit_id in ids if unit_id in lookup)

        def add_hits(ids: Iterable[str], stage: SearchStage, *, score_offset: float = 0.0) -> None:
            for position, unit_id in enumerate(ids):
                unit = lookup.get(str(unit_id))
                if unit is None:
                    continue
                score = float(max(0, stage.candidate_limit - position)) + score_offset
                previous = hits.get(unit.unit_id)
                if previous is None:
                    hits[unit.unit_id] = SearchHit(
                        unit_id=unit.unit_id,
                        document_id=unit.document_id,
                        page_number=unit.page_number,
                        score=score,
                        stages=(stage.stage_id,),
                    )
                else:
                    hits[unit.unit_id] = replace(
                        previous,
                        score=max(previous.score, score),
                        stages=tuple(sorted(set(previous.stages) | {stage.stage_id})),
                        visual_verified=previous.visual_verified or unit.unit_id in visual_verified,
                    )

        def record(stage: SearchStage, ids: Iterable[str], *, inspected_ids: Iterable[str] = (), degraded: str = "") -> None:
            returned = tuple(str(unit_id) for unit_id in ids if str(unit_id) in lookup)
            inspected_values = tuple(str(unit_id) for unit_id in inspected_ids if str(unit_id) in lookup)
            mark_inspected(inspected_values)
            if degraded:
                degradations.append(degraded)
            stages.append(
                StageExecution(
                    stage_id=stage.stage_id,
                    mode=stage.mode,
                    enabled=stage.enabled,
                    inspected_count=len(set(inspected_values)),
                    inspected_unit_ids=tuple(dict.fromkeys(inspected_values)),
                    returned_unit_ids=returned,
                    degraded_reason=degraded,
                )
            )

        def sufficient() -> bool:
            if spec.exact_objective:
                required_terms = set(spec.exact_terms)
                if required_terms <= exact_terms_verified and exact_proof_ids:
                    return True
            selected = [hit for hit in hits.values() if not spec.visual or hit.visual_verified]
            if len(selected) < spec.min_evidence:
                return False
            if spec.multi_document and len({hit.document_id for hit in selected}) < 2:
                return False
            return True

        for stage in plan.stages:
            if stage.mode == "exact":
                if not spec.exact_terms:
                    # An identifier objective without a supplied value cannot
                    # perform an exact scan; lexical/semantic recovery remains
                    # responsible for finding candidate evidence.
                    record(stage, ())
                    exact_ids = ()
                else:
                    exact_ids_list: list[str] = []
                    inspected_ids: list[str] = []
                    required_terms = {term.casefold() for term in spec.exact_terms}
                    for unit in ordered:
                        inspected_ids.append(unit.unit_id)
                        matched_terms = {
                            term
                            for term in required_terms
                            if term in unit.text.casefold()
                        }
                        if matched_terms:
                            exact_ids_list.append(unit.unit_id)
                            exact_terms_verified.update(matched_terms)
                            if spec.exact_objective and required_terms <= exact_terms_verified:
                                break
                    # Broad/multi-document exact terms retain the prior full
                    # corpus scan so absence cannot be inferred from top-k.
                    if not spec.exact_objective and len(inspected_ids) < len(ordered):
                        inspected_ids.extend(
                            unit.unit_id for unit in ordered if unit.unit_id not in inspected_ids
                        )
                    exact_ids = tuple(dict.fromkeys(exact_ids_list))
                    record(stage, exact_ids, inspected_ids=inspected_ids)
                exact_proof_ids.update(exact_ids)
                add_hits(exact_ids, stage, score_offset=1000.0)
            elif stage.mode in {"lexical", "lexical_expand"}:
                ranked = all_ranked_lexically
                selected = tuple(unit.unit_id for _score, unit in ranked[: stage.candidate_limit])
                evidence_ids = tuple(
                    unit.unit_id for score, unit in ranked[: stage.candidate_limit] if score > 0
                )
                if len(ranked) > stage.candidate_limit:
                    top_k_exhausted = True
                record(stage, selected, inspected_ids=selected)
                add_hits(evidence_ids, stage)
            elif stage.mode == "semantic_union":
                if not stage.enabled:
                    record(stage, (), degraded="embeddings_unavailable")
                elif semantic_ranker is None:
                    record(stage, (), degraded="embeddings_not_executed")
                else:
                    try:
                        semantic_ids = _ranker_ids(
                            _invoke_ranker(semantic_ranker, spec.query, ordered, stage.candidate_limit), lookup
                        )
                        selected = tuple(semantic_ids[: stage.candidate_limit])
                        record(stage, selected, inspected_ids=selected)
                        add_hits(selected, stage, score_offset=0.5)
                    except Exception as exc:  # provider-neutral degradation boundary
                        record(stage, (), degraded=f"embeddings_error:{type(exc).__name__}")
            elif stage.mode == "rerank_union":
                candidate_units = tuple(
                    lookup[unit_id]
                    for unit_id in sorted(hits, key=lambda value: lookup[value].sort_key)
                    if unit_id in lookup
                )
                if not stage.enabled:
                    record(stage, (), degraded="reranker_unavailable")
                elif reranker is None:
                    record(stage, (), degraded="reranker_not_executed")
                elif not candidate_units:
                    record(stage, (), degraded="reranker_no_candidates")
                else:
                    try:
                        reranked_ids = _ranker_ids(
                            _invoke_ranker(reranker, spec.query, candidate_units, stage.candidate_limit), lookup
                        )
                        selected = tuple(reranked_ids[: stage.candidate_limit])
                        record(stage, selected, inspected_ids=selected)
                        # Keep the semantic/lexical union; reranking cannot discard it.
                        add_hits(selected, stage, score_offset=1.0)
                    except Exception as exc:
                        record(stage, (), degraded=f"reranker_error:{type(exc).__name__}")
            elif stage.mode == "image_embedding":
                image_units = tuple(unit for unit in ordered if unit.has_image)
                if not stage.enabled:
                    record(stage, (), degraded="image_embeddings_unavailable")
                elif image_ranker is None:
                    record(stage, (), degraded="image_embeddings_not_executed")
                elif not image_units:
                    record(stage, (), degraded="no_image_units")
                else:
                    try:
                        image_ids = _ranker_ids(
                            _invoke_ranker(image_ranker, spec.query, image_units, self.budget.max_image_pages),
                            lookup,
                        )
                        selected = tuple(image_ids[: self.budget.max_image_pages])
                        record(stage, selected, inspected_ids=selected)
                        add_hits(selected, stage, score_offset=0.25)
                    except Exception as exc:
                        record(stage, (), degraded=f"image_embeddings_error:{type(exc).__name__}")
            elif stage.mode == "vlm":
                visual_candidates = [
                    lookup[unit_id]
                    for unit_id in hits
                    if unit_id in lookup and lookup[unit_id].has_image
                ]
                if not visual_candidates:
                    visual_candidates = [unit for unit in ordered if unit.has_image][: self.budget.max_vlm_pages]
                visual_candidates = visual_candidates[: self.budget.max_vlm_pages]
                if not stage.enabled:
                    record(stage, (), inspected_ids=(unit.unit_id for unit in visual_candidates), degraded="vlm_unavailable")
                elif vlm_verifier is None:
                    record(stage, (), inspected_ids=(unit.unit_id for unit in visual_candidates), degraded="vlm_not_executed")
                else:
                    confirmed: list[str] = []
                    try:
                        for unit in visual_candidates:
                            verdict = _invoke_visual_verifier(vlm_verifier, spec, unit)
                            is_confirmed = bool(verdict.get("confirmed")) if isinstance(verdict, Mapping) else bool(verdict)
                            if is_confirmed:
                                confirmed.append(unit.unit_id)
                                visual_verified.add(unit.unit_id)
                        record(stage, confirmed, inspected_ids=(unit.unit_id for unit in visual_candidates))
                        add_hits(confirmed, stage, score_offset=2.0)
                    except Exception as exc:
                        record(stage, confirmed, inspected_ids=(unit.unit_id for unit in visual_candidates), degraded=f"vlm_error:{type(exc).__name__}")
            elif stage.mode == "unseen_recovery":
                remaining = [unit for unit in ordered if unit.unit_id not in inspected]
                recovery = remaining[: self.budget.max_unseen_pages]
                recovery_ids: list[str] = []
                for unit in recovery:
                    score = self._lexical_score(spec, unit, hinted)
                    exact = any(term.casefold() in unit.text.casefold() for term in spec.exact_terms)
                    if score > 0 or exact or (spec.visual and unit.has_image):
                        recovery_ids.append(unit.unit_id)
                record(stage, recovery_ids, inspected_ids=(unit.unit_id for unit in recovery))
                add_hits(recovery_ids, stage, score_offset=0.75)

            if sufficient():
                break

        remaining = tuple(unit.unit_id for unit in ordered if unit.unit_id not in inspected)
        full_corpus = not remaining
        visual_degraded = spec.visual and not visual_verified and any(
            degradation.startswith(("image_embeddings_", "vlm_")) or degradation.startswith("vlm_error")
            for degradation in degradations
        )
        if sufficient():
            status = SearchStatus.SUFFICIENT
        elif visual_degraded:
            # A complete text scan cannot prove a visual-only requirement when
            # the image/VLM lane is unavailable.  It is not evidence of absence.
            status = SearchStatus.SEARCH_INCOMPLETE
        elif full_corpus:
            status = SearchStatus.EXHAUSTED
        else:
            status = SearchStatus.SEARCH_INCOMPLETE

        selected_hits = tuple(
            sorted(
                (
                    replace(hit, visual_verified=hit.visual_verified or hit.unit_id in visual_verified)
                    for hit in hits.values()
                ),
                key=lambda hit: (-hit.score, hit.document_id.casefold(), hit.page_number, hit.unit_id.casefold()),
            )[: self.budget.max_total_candidates]
        )
        inspected_pages = {(lookup[unit_id].document_id, lookup[unit_id].page_number) for unit_id in inspected}
        all_pages = {(unit.document_id, unit.page_number) for unit in ordered}
        inspected_docs = {lookup[unit_id].document_id for unit_id in inspected}
        all_docs = {unit.document_id for unit in ordered}
        recovered_pages = {
            (lookup[unit_id].document_id, lookup[unit_id].page_number)
            for execution in stages
            if execution.mode == "unseen_recovery"
            for unit_id in execution.inspected_unit_ids
            if unit_id in lookup
        }
        coverage = SearchCoverage(
            units_total=len(ordered),
            units_inspected=len(inspected),
            pages_total=len(all_pages),
            pages_inspected=len(inspected_pages),
            documents_total=len(all_docs),
            documents_inspected=len(inspected_docs),
            route_hint_units=len(hinted),
            unseen_pages_recovered=len(recovered_pages),
            top_k_exhausted=top_k_exhausted,
            full_corpus_searched=full_corpus,
            inspected_unit_ids=tuple(sorted(inspected, key=lambda unit_id: lookup[unit_id].sort_key)),
            remaining_unit_ids=remaining,
            degraded_providers=tuple(sorted(set(degradations))),
        )
        escalations = plan.instructions
        return SearchResult(status, selected_hits, coverage, tuple(stages), tuple(sorted(set(degradations))), escalations)

    run = search

    @staticmethod
    def _lexical_score(criterion: CriterionSpec, unit: SearchUnit, hinted: set[str]) -> float:
        query_tokens = set(_text_tokens(criterion.query))
        unit_tokens = set(_text_tokens(unit.text))
        overlap = len(query_tokens & unit_tokens)
        exact_count = sum(1 for term in criterion.exact_terms if term.casefold() in unit.text.casefold())
        label_match = bool({label.casefold() for label in unit.labels} & {label.casefold() for label in criterion.classification_hints})
        return float(overlap + exact_count * 100 + (1 if label_match and unit.unit_id in hinted else 0))

    @classmethod
    def _lexical_rank(
        cls,
        criterion: CriterionSpec,
        units: Sequence[SearchUnit],
        hinted: set[str],
    ) -> tuple[tuple[float, SearchUnit], ...]:
        ranked = [(cls._lexical_score(criterion, unit, hinted), unit) for unit in units]
        return tuple(sorted(ranked, key=lambda item: (-item[0], item[1].sort_key)))


def _invoke_ranker(ranker: Ranker, query: str, units: Sequence[SearchUnit], limit: int) -> Sequence[Any]:
    """Support small two-argument test/adaptor callbacks without owning clients."""

    try:
        parameters = inspect.signature(ranker).parameters.values()
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        variadic = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters)
        return ranker(query, units, limit) if variadic or len(positional) >= 3 else ranker(query, units)
    except (TypeError, ValueError):
        return ranker(query, units, limit)


def _invoke_visual_verifier(verifier: VisualVerifier, criterion: CriterionSpec, unit: SearchUnit) -> Any:
    try:
        parameters = inspect.signature(verifier).parameters.values()
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        return verifier(criterion, unit) if len(positional) >= 2 else verifier(unit)
    except (TypeError, ValueError):
        return verifier(criterion, unit)


def _ranker_ids(values: Sequence[Any], lookup: Mapping[str, SearchUnit]) -> tuple[str, ...]:
    ids: list[str] = []
    for value in values or ():
        if isinstance(value, SearchUnit):
            unit_id = value.unit_id
        elif isinstance(value, Mapping):
            unit_id = value.get("unit_id", value.get("id", ""))
        elif isinstance(value, (tuple, list)) and value:
            unit_id = value[0]
        else:
            unit_id = value
        unit_id = str(unit_id)
        if unit_id in lookup and unit_id not in ids:
            ids.append(unit_id)
    return tuple(ids)
