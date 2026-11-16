"""Small, immutable primitives for entity- and proof-aware evidence.

This module deliberately has no persistence or provider integration.  It is a
pure boundary between extracted observations and a later evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering
import math
import re
from typing import Iterable, Mapping


class EntityRole(str, Enum):
    """The party or asset to which an observation belongs."""

    BIDDER = "bidder"
    DIRECTOR = "director"
    RELATED_PARTY = "related_party"
    CUSTOMER = "customer"
    ISSUER = "issuer"
    PLANT = "plant"
    TPIA = "tpia"


class IdentifierType(str, Enum):
    PAN = "pan"
    GSTIN = "gstin"


@total_ordering
class ProofStrength(str, Enum):
    """Ordered proof quality; a lower level never satisfies a higher one."""

    CLAIM = "claim"
    BIDDER_DECLARATION = "bidder_declaration"
    ISSUER_CERTIFICATE = "issuer_certificate"
    THIRD_PARTY_VERIFIED = "third_party_verified"
    PRIMARY_RECORD = "primary_record"

    def __str__(self) -> str:
        return self.value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ProofStrength):
            return NotImplemented
        return _PROOF_RANK[self] < _PROOF_RANK[other]


_PROOF_RANK = {
    ProofStrength.CLAIM: 0,
    ProofStrength.BIDDER_DECLARATION: 1,
    ProofStrength.ISSUER_CERTIFICATE: 2,
    ProofStrength.THIRD_PARTY_VERIFIED: 3,
    ProofStrength.PRIMARY_RECORD: 4,
}


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _coerce_role(value: EntityRole | str) -> EntityRole:
    if isinstance(value, EntityRole):
        return value
    try:
        return EntityRole(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown entity role: {value!r}") from exc


def _coerce_identifier_type(value: IdentifierType | str) -> IdentifierType:
    if isinstance(value, IdentifierType):
        return value
    try:
        return IdentifierType(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown identifier type: {value!r}") from exc


def _coerce_proof_strength(value: ProofStrength | str) -> ProofStrength:
    if isinstance(value, ProofStrength):
        return value
    try:
        return ProofStrength(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown proof strength: {value!r}") from exc


def _labels(values: Iterable[str] | str) -> frozenset[str]:
    if isinstance(values, str):
        values = (values,)
    result = {
        re.sub(r"\s+", "_", value.strip().lower())
        for value in values
        if isinstance(value, str) and value.strip()
    }
    return frozenset(result)


def normalize_pan(value: str) -> str:
    """Return a canonical PAN or raise for a malformed identifier."""

    compact = re.sub(r"[^A-Za-z0-9]", "", _required_text(value, "PAN")).upper()
    if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", compact):
        raise ValueError(f"invalid PAN: {value!r}")
    return compact


def normalize_gstin(value: str) -> str:
    """Return a canonical 15-character GSTIN or raise for a malformed one."""

    compact = re.sub(r"[^A-Za-z0-9]", "", _required_text(value, "GSTIN")).upper()
    if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]", compact):
        raise ValueError(f"invalid GSTIN: {value!r}")
    return compact


def normalize_identifier(identifier_type: IdentifierType | str, value: str) -> str:
    kind = _coerce_identifier_type(identifier_type)
    return normalize_pan(value) if kind is IdentifierType.PAN else normalize_gstin(value)


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """The smallest acceptable exact source locator for an observation."""

    document_id: str
    page_number: int
    page_id: str | None = None
    span: tuple[int, int] | None = None
    quote: str | None = None
    image_id: str | None = None
    image_uri: str | None = None
    image_bbox: tuple[float, float, float, float] | None = None
    block_id: str | None = None
    table_id: str | None = None
    cell_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_id", _required_text(self.document_id, "document_id"))
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int) or self.page_number < 1:
            raise ValueError("page_number must be a positive integer")
        if self.page_id is not None:
            object.__setattr__(self, "page_id", _required_text(self.page_id, "page_id"))
        if self.span is not None:
            if len(self.span) != 2 or any(isinstance(item, bool) or not isinstance(item, int) for item in self.span):
                raise ValueError("span must contain two integer offsets")
            start, end = self.span
            if start < 0 or end <= start:
                raise ValueError("span must be a non-empty, forward text range")
            object.__setattr__(self, "span", (start, end))
        if self.quote is not None:
            object.__setattr__(self, "quote", _required_text(self.quote, "quote"))
        for name in ("image_id", "image_uri", "block_id", "table_id", "cell_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))
        if self.image_bbox is not None:
            if len(self.image_bbox) != 4 or any(not isinstance(item, (int, float)) for item in self.image_bbox):
                raise ValueError("image_bbox must contain four numbers")
            if any(not math.isfinite(float(item)) for item in self.image_bbox):
                raise ValueError("image_bbox must contain finite numbers")
            object.__setattr__(self, "image_bbox", tuple(float(item) for item in self.image_bbox))
        if self.span is None and self.quote is None and self.image_id is None and self.image_uri is None:
            raise ValueError("provenance needs a text span, quote, or image locator")

    @property
    def text_span(self) -> tuple[int, int] | None:
        """Compatibility spelling for callers that call the range ``text_span``."""

        return self.span

    def as_dict(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "document_id": self.document_id,
                "page_number": self.page_number,
                "page_id": self.page_id,
                "span": self.span,
                "quote": self.quote,
                "image_id": self.image_id,
                "image_uri": self.image_uri,
                "image_bbox": self.image_bbox,
                "block_id": self.block_id,
                "table_id": self.table_id,
                "cell_id": self.cell_id,
            }.items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """A multi-label artifact without collapsing it to one document type."""

    artifact_id: str
    labels: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    provenance: tuple[SourceProvenance, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _required_text(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "labels", _labels(self.labels))
        object.__setattr__(self, "capabilities", _labels(self.capabilities))
        refs = tuple(self.provenance)
        if not refs or not all(isinstance(ref, SourceProvenance) for ref in refs):
            raise ValueError("an artifact needs at least one SourceProvenance")
        object.__setattr__(self, "provenance", refs)

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "labels": sorted(self.labels),
            "capabilities": sorted(self.capabilities),
            "provenance": [ref.as_dict() for ref in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    """One source-grounded fact about one explicitly identified entity."""

    observation_id: str
    fact_type: str
    value: str
    entity_id: str
    entity_role: EntityRole | str
    provenance: SourceProvenance
    proof_strength: ProofStrength | str = ProofStrength.CLAIM
    artifact_labels: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    identifier_type: IdentifierType | str | None = None
    normalized_value: str | None = None
    issuer_entity_id: str | None = None
    issuer_role: EntityRole | str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _required_text(self.observation_id, "observation_id"))
        object.__setattr__(self, "fact_type", _required_text(self.fact_type, "fact_type").lower())
        object.__setattr__(self, "value", _required_text(self.value, "value"))
        object.__setattr__(self, "entity_id", _required_text(self.entity_id, "entity_id"))
        object.__setattr__(self, "entity_role", _coerce_role(self.entity_role))
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        object.__setattr__(self, "proof_strength", _coerce_proof_strength(self.proof_strength))
        object.__setattr__(self, "artifact_labels", _labels(self.artifact_labels))
        object.__setattr__(self, "capabilities", _labels(self.capabilities))
        if self.identifier_type is not None:
            identifier_type = _coerce_identifier_type(self.identifier_type)
            canonical = normalize_identifier(identifier_type, self.value)
            if self.normalized_value is not None:
                supplied = normalize_identifier(identifier_type, self.normalized_value)
                if supplied != canonical:
                    raise ValueError("normalized_value does not match value")
            object.__setattr__(self, "identifier_type", identifier_type)
            object.__setattr__(self, "normalized_value", canonical)
        elif self.normalized_value is not None:
            object.__setattr__(self, "normalized_value", _required_text(self.normalized_value, "normalized_value"))
        if self.issuer_entity_id is not None:
            object.__setattr__(self, "issuer_entity_id", _required_text(self.issuer_entity_id, "issuer_entity_id"))
        if self.issuer_role is not None:
            object.__setattr__(self, "issuer_role", _coerce_role(self.issuer_role))
        if isinstance(self.metadata, Mapping):
            items = self.metadata.items()
        else:
            items = self.metadata
        frozen_metadata = tuple(sorted((_required_text(str(key), "metadata key"), str(value)) for key, value in items))
        object.__setattr__(self, "metadata", frozen_metadata)

    @property
    def raw_value(self) -> str:
        return self.value

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "fact_type": self.fact_type,
            "value": self.value,
            "normalized_value": self.normalized_value,
            "entity_id": self.entity_id,
            "entity_role": self.entity_role.value,
            "issuer_entity_id": self.issuer_entity_id,
            "issuer_role": self.issuer_role.value if self.issuer_role else None,
            "proof_strength": self.proof_strength.value,
            "artifact_labels": sorted(self.artifact_labels),
            "capabilities": sorted(self.capabilities),
            "identifier_type": self.identifier_type.value if self.identifier_type else None,
            "provenance": self.provenance.as_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProofContract:
    """Requirements an observation must already satisfy; no upgrading occurs."""

    minimum_strength: ProofStrength | str = ProofStrength.CLAIM
    accepted_artifact_labels: frozenset[str] = field(default_factory=frozenset)
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    entity_role: EntityRole | str | None = None
    entity_id: str | None = None
    identifier_type: IdentifierType | str | None = None
    issuer_role: EntityRole | str | None = None
    issuer_entity_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "minimum_strength", _coerce_proof_strength(self.minimum_strength))
        object.__setattr__(self, "accepted_artifact_labels", _labels(self.accepted_artifact_labels))
        object.__setattr__(self, "required_capabilities", _labels(self.required_capabilities))
        if self.entity_role is not None:
            object.__setattr__(self, "entity_role", _coerce_role(self.entity_role))
        if self.entity_id is not None:
            object.__setattr__(self, "entity_id", _required_text(self.entity_id, "entity_id"))
        if self.identifier_type is not None:
            object.__setattr__(self, "identifier_type", _coerce_identifier_type(self.identifier_type))
        if self.issuer_role is not None:
            object.__setattr__(self, "issuer_role", _coerce_role(self.issuer_role))
        if self.issuer_entity_id is not None:
            object.__setattr__(self, "issuer_entity_id", _required_text(self.issuer_entity_id, "issuer_entity_id"))

    def satisfied_by(self, observation: EvidenceObservation) -> bool:
        if not isinstance(observation, EvidenceObservation):
            raise TypeError("observation must be EvidenceObservation")
        if observation.proof_strength < self.minimum_strength:
            return False
        if self.accepted_artifact_labels and not (
            self.accepted_artifact_labels & observation.artifact_labels
        ):
            return False
        if not self.required_capabilities.issubset(observation.capabilities):
            return False
        if self.entity_role is not None and observation.entity_role is not self.entity_role:
            return False
        if self.entity_id is not None and observation.entity_id != self.entity_id:
            return False
        if self.identifier_type is not None and observation.identifier_type is not self.identifier_type:
            return False
        if self.issuer_role is not None and observation.issuer_role is not self.issuer_role:
            return False
        if self.issuer_entity_id is not None and observation.issuer_entity_id != self.issuer_entity_id:
            return False
        return True


def proof_contract_satisfied(observation: EvidenceObservation, contract: ProofContract) -> bool:
    """Return whether the supplied observation meets the contract as-is."""

    if not isinstance(contract, ProofContract):
        raise TypeError("contract must be ProofContract")
    return contract.satisfied_by(observation)


@dataclass(frozen=True, slots=True)
class ObservationConflict:
    """Conflicting values for one fact/entity/role scope."""

    fact_type: str
    entity_id: str
    entity_role: EntityRole
    identifier_type: IdentifierType | None
    observations: tuple[EvidenceObservation, ...]

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(sorted({observation.normalized_value or observation.value for observation in self.observations}))


def detect_conflicts(observations: Iterable[EvidenceObservation]) -> tuple[ObservationConflict, ...]:
    """Detect conflicts only within the same entity and role, never across roles."""

    groups: dict[tuple[str, EntityRole, str, IdentifierType | None], list[EvidenceObservation]] = {}
    for observation in observations:
        if not isinstance(observation, EvidenceObservation):
            raise TypeError("all observations must be EvidenceObservation")
        key = (
            observation.entity_id,
            observation.entity_role,
            observation.fact_type,
            observation.identifier_type,
        )
        groups.setdefault(key, []).append(observation)

    conflicts: list[ObservationConflict] = []
    for (entity_id, role, fact_type, identifier_type), group in groups.items():
        values = {observation.normalized_value or observation.value.strip().casefold() for observation in group}
        if len(values) > 1:
            conflicts.append(
                ObservationConflict(
                    fact_type=fact_type,
                    entity_id=entity_id,
                    entity_role=role,
                    identifier_type=identifier_type,
                    observations=tuple(sorted(group, key=lambda item: item.observation_id)),
                )
            )
    return tuple(sorted(conflicts, key=lambda item: (item.entity_id, item.entity_role.value, item.fact_type)))


class ReconciliationStatus(str, Enum):
    MATCHED = "matched"
    MISSING_GSTIN = "missing_gstin"
    MISSING_BIDDER_PAN = "missing_bidder_pan"
    CONFLICTING_GSTIN = "conflicting_gstin"
    CONFLICTING_BIDDER_PAN = "conflicting_bidder_pan"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class GstinPanReconciliation:
    bidder_entity_id: str
    status: ReconciliationStatus
    gstin: str | None = None
    bidder_pan: str | None = None
    gstin_observations: tuple[EvidenceObservation, ...] = ()
    pan_observations: tuple[EvidenceObservation, ...] = ()

    @property
    def matched(self) -> bool:
        return self.status is ReconciliationStatus.MATCHED


def reconcile_gstin_to_bidder_pan(
    observations: Iterable[EvidenceObservation], bidder_entity_id: str
) -> GstinPanReconciliation:
    """Reconcile only a bidder's GSTIN with that same bidder's PAN.

    A GSTIN embeds the bidder PAN in positions 3-12.  Director, customer and
    related-party PANs are intentionally excluded from this reconciliation.
    """

    bidder_id = _required_text(bidder_entity_id, "bidder_entity_id")
    source_items = tuple(observations)
    if not all(isinstance(item, EvidenceObservation) for item in source_items):
        raise TypeError("all observations must be EvidenceObservation")
    scoped = tuple(
        observation
        for observation in source_items
        if observation.entity_id == bidder_id and observation.entity_role is EntityRole.BIDDER
    )
    gstin_observations = tuple(item for item in scoped if item.identifier_type is IdentifierType.GSTIN)
    pan_observations = tuple(item for item in scoped if item.identifier_type is IdentifierType.PAN)
    gstins = {item.normalized_value for item in gstin_observations}
    pans = {item.normalized_value for item in pan_observations}

    base = {"bidder_entity_id": bidder_id, "gstin_observations": gstin_observations, "pan_observations": pan_observations}
    if not gstins:
        return GstinPanReconciliation(status=ReconciliationStatus.MISSING_GSTIN, **base)
    if len({gstin[2:12] for gstin in gstins}) > 1:
        return GstinPanReconciliation(status=ReconciliationStatus.CONFLICTING_GSTIN, **base)
    if not pans:
        return GstinPanReconciliation(
            status=ReconciliationStatus.MISSING_BIDDER_PAN,
            gstin=next(iter(gstins)) if len(gstins) == 1 else None,
            **base,
        )
    if len(pans) > 1:
        return GstinPanReconciliation(
            status=ReconciliationStatus.CONFLICTING_BIDDER_PAN,
            gstin=next(iter(gstins)) if len(gstins) == 1 else None,
            **base,
        )

    gstin = next(iter(gstins)) if len(gstins) == 1 else None
    pan = next(iter(pans))
    status = ReconciliationStatus.MATCHED if all(value[2:12] == pan for value in gstins) else ReconciliationStatus.MISMATCH
    return GstinPanReconciliation(status=status, gstin=gstin, bidder_pan=pan, **base)


def proof_contract_satisfied_by_any(
    observations: Iterable[EvidenceObservation], contract: ProofContract
) -> bool:
    """Check a contract against existing observations without changing them."""

    return any(proof_contract_satisfied(observation, contract) for observation in observations)


@dataclass(frozen=True, slots=True)
class EvidenceLedger:
    """An immutable tuple of observations with pure query operations."""

    observations: tuple[EvidenceObservation, ...] = ()

    def __post_init__(self) -> None:
        items = tuple(self.observations)
        if not all(isinstance(item, EvidenceObservation) for item in items):
            raise TypeError("ledger observations must be EvidenceObservation instances")
        ids = [item.observation_id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("observation_id values must be unique")
        object.__setattr__(self, "observations", items)

    def add(self, observation: EvidenceObservation) -> EvidenceLedger:
        if not isinstance(observation, EvidenceObservation):
            raise TypeError("observation must be EvidenceObservation")
        if any(item.observation_id == observation.observation_id for item in self.observations):
            raise ValueError(f"duplicate observation_id: {observation.observation_id}")
        return EvidenceLedger(self.observations + (observation,))

    def conflicts(self) -> tuple[ObservationConflict, ...]:
        return detect_conflicts(self.observations)

    def satisfying(self, contract: ProofContract) -> tuple[EvidenceObservation, ...]:
        return tuple(item for item in self.observations if proof_contract_satisfied(item, contract))

    def reconcile_gstin_to_bidder_pan(self, bidder_entity_id: str) -> GstinPanReconciliation:
        return reconcile_gstin_to_bidder_pan(self.observations, bidder_entity_id)


# Clear aliases for callers that use the shorter names.
EvidenceSource = SourceProvenance
Artifact = ArtifactDescriptor
detect_role_aware_conflicts = detect_conflicts
reconcile_gstin_to_pan = reconcile_gstin_to_bidder_pan
satisfies_proof_contract = proof_contract_satisfied


__all__ = [
    "Artifact",
    "ArtifactDescriptor",
    "EntityRole",
    "EvidenceLedger",
    "EvidenceObservation",
    "EvidenceSource",
    "GstinPanReconciliation",
    "IdentifierType",
    "ObservationConflict",
    "ProofContract",
    "ProofStrength",
    "ReconciliationStatus",
    "SourceProvenance",
    "detect_conflicts",
    "detect_role_aware_conflicts",
    "normalize_gstin",
    "normalize_identifier",
    "normalize_pan",
    "proof_contract_satisfied",
    "proof_contract_satisfied_by_any",
    "reconcile_gstin_to_bidder_pan",
    "reconcile_gstin_to_pan",
    "satisfies_proof_contract",
]
