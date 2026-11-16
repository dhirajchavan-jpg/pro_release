"""Policy corpus loading, validation, and retrieval indexing."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from procurement.platform.files import write_json_atomic

_ROOT = Path(__file__).resolve().parents[2]
_POLICY_DIR = _ROOT / "config" / "policy"
_CACHE_DIR = _ROOT / "runtime" / "cache" / "policy_embeddings"

_POLICY_FILES = {
    "bhel_purchase_policy": "bhel_purchase_policy.v1.json",
    "bhel_works_policy": "bhel_works_policy.v1.json",
}

_corpora: dict[str, dict[str, Any]] = {}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def offline_mode() -> bool:
    return os.getenv("PROCUREMENT_OFFLINE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_ws(text: Any) -> str:
    return " ".join(str(text or "").split())


def policy_context(corpus: dict) -> dict:
    """Keep full clause wording while sharing repeated section headings."""
    sections = {}
    for clause in corpus.get("clauses", []):
        section = (clause.get("section"), clause.get("section_title"))
        sections.setdefault(section, []).append({key: clause.get(key) for key in ("clause_id", "title", "text")})
    return {"policy_id": corpus["policy_id"], "name": corpus.get("name", ""),
            "sections": [{"section": key[0], "title": key[1], "clauses": clauses}
                         for key, clauses in sections.items()]}


def token_set(text: Any) -> set[str]:
    return set(_TOKEN_RE.findall(str(text or "").lower()))


def load_corpus(policy_id: str) -> dict[str, Any]:
    """Load and validate a policy corpus; cached per process."""
    if policy_id in _corpora:
        return _corpora[policy_id]
    filename = _POLICY_FILES.get(policy_id)
    if not filename:
        raise ValueError(f"unknown policy_id: {policy_id}")
    corpus = json.loads((_POLICY_DIR / filename).read_text(encoding="utf-8"))
    _validate_corpus(policy_id, corpus)
    _corpora[policy_id] = corpus
    return corpus


def _validate_corpus(policy_id: str, corpus: dict[str, Any]) -> None:
    if not isinstance(corpus, dict) or corpus.get("schema_version") != "policy-corpus.v1":
        raise ValueError(f"{policy_id}: unsupported or missing schema_version")
    if corpus.get("policy_id") != policy_id:
        raise ValueError(f"{policy_id}: corpus policy_id mismatch: {corpus.get('policy_id')!r}")
    clauses = corpus.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        raise ValueError(f"{policy_id}: corpus has no clauses")
    if int(corpus.get("clause_count") or 0) != len(clauses):
        raise ValueError(f"{policy_id}: clause_count mismatch")
    source_name = str(corpus.get("source_file") or "")
    if not source_name or Path(source_name).name != source_name:
        raise ValueError(f"{policy_id}: invalid source_file")
    source_path = _POLICY_DIR / "source" / source_name
    if not source_path.is_file():
        raise ValueError(f"{policy_id}: policy source is missing")
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if str(corpus.get("source_sha256") or "").lower() != source_digest:
        raise ValueError(f"{policy_id}: policy source sha256 mismatch")
    seen: set[str] = set()
    for clause in clauses:
        clause_id = str(clause.get("clause_id") or "")
        if not clause_id or clause_id in seen:
            raise ValueError(f"{policy_id}: missing or duplicate clause_id {clause_id!r}")
        seen.add(clause_id)
        expected = hashlib.sha256(
            f"{clause.get('clause_id')}\n{clause.get('title')}\n{clause.get('text')}".encode("utf-8")
        ).hexdigest()
        if clause.get("sha256") != expected:
            raise ValueError(f"{policy_id}: clause {clause_id} failed sha256 validation")
    digest = hashlib.sha256(
        json.dumps(clauses, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    if corpus.get("corpus_sha256") != digest:
        raise ValueError(f"{policy_id}: corpus_sha256 mismatch")


_APPLICABILITY_PATH = _POLICY_DIR / "applicability.v1.json"
_applicability_cache: dict[str, dict[str, dict[str, Any]]] | None = None


def load_applicability(policy_id: str) -> dict[str, dict[str, Any]]:
    """Load validated per-clause applicability metadata for one policy book."""
    global _applicability_cache
    if policy_id not in _POLICY_FILES:
        raise ValueError(f"unknown policy_id: {policy_id}")
    if _applicability_cache is None:
        if not _APPLICABILITY_PATH.is_file():
            raise ValueError("policy applicability artifact is missing")
        payload = json.loads(_APPLICABILITY_PATH.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "applicability.v1":
            raise ValueError("unsupported policy applicability schema")
        _applicability_cache = {}
        for policy, clauses in (payload.get("policies") or {}).items():
            policy = str(policy)
            if policy not in _POLICY_FILES or not isinstance(clauses, dict):
                raise ValueError(f"invalid applicability policy: {policy}")
            known = {str(item.get("clause_id") or "") for item in load_corpus(policy).get("clauses") or []}
            unknown = {str(clause_id) for clause_id in clauses} - known
            if unknown:
                raise ValueError(f"{policy}: applicability references unknown clauses: {sorted(unknown)}")
            records: dict[str, dict[str, Any]] = {}
            for clause_id, record in clauses.items():
                if not isinstance(record, dict) or record.get("mode") not in {
                    "unconditional", "conditional", "internal_process"
                }:
                    raise ValueError(f"{policy}: invalid applicability for clause {clause_id}")
                if record.get("mode") == "conditional" and not str(record.get("trigger") or "").strip():
                    raise ValueError(f"{policy}: conditional clause {clause_id} has no trigger")
                records[str(clause_id)] = dict(record)
            _applicability_cache[policy] = records
    return _applicability_cache.get(policy_id, {})


def available_policies() -> list[dict[str, Any]]:
    return [
        {
            "policy_id": policy_id,
            "name": corpus["name"],
            "version": corpus["version"],
        }
        for policy_id in sorted(_POLICY_FILES)
        for corpus in (load_corpus(policy_id),)
    ]


def clause_embedding_index(policy_id: str) -> dict[str, list[float]]:
    """Content-addressed embedding index clause_id -> vector.

    Returns an empty dict when embeddings are unavailable (offline mode or
    provider failure); callers must fall back to lexical_rank.
    """
    corpus = load_corpus(policy_id)
    digest = str(corpus.get("corpus_sha256") or "")[:16]
    from ai_provider import settings

    settings_value = settings()
    embedding_identity = hashlib.sha256(
        "\0".join(
            (
                "policy-embedding-cache-v2",
                settings_value.provider,
                settings_value.embedding_base_url.rstrip("/"),
                settings_value.model_embedding,
                os.getenv("AI_EMBEDDING_DIMENSIONS", "provider-default"),
                os.getenv("AI_CACHE_REVISION", os.getenv("AI_DEPLOYMENT_REVISION", "1")),
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    cache_path = _CACHE_DIR / f"{policy_id}-{digest}-{embedding_identity}.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            vectors = data.get("vectors")
            if isinstance(vectors, dict) and vectors:
                return {str(key): [float(x) for x in value] for key, value in vectors.items()}
        except (OSError, ValueError, TypeError):
            pass
    if offline_mode():
        return {}
    try:
        from ai_provider import embed_texts

        clauses = corpus["clauses"]
        texts = [
            f"{clause['clause_id']} {clause.get('title') or ''}\n{(clause.get('text') or '')[:1500]}"
            for clause in clauses
        ]
        vectors = embed_texts(texts)
        if not isinstance(vectors, list) or len(vectors) != len(clauses):
            return {}
        index = {
            str(clause["clause_id"]): [float(x) for x in vector]
            for clause, vector in zip(clauses, vectors)
        }
    except Exception:
        return {}
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            cache_path,
            {
                "policy_id": policy_id,
                "corpus_sha256": corpus.get("corpus_sha256"),
                "embedding_identity": embedding_identity,
                "vectors": index,
            },
            indent=None,
        )
    except OSError:
        pass
    return index


_INTERNAL_MARKERS = (
    "committee", "delegation of powers", "competent authority", "approval of",
    "approving authority", "register", "financial concurrence", "indent",
    "unit head", "next higher authority", "corp. mm", "audit para",
    "work instruction", "departmental procedure", "sister unit", "bhel shall",
    "units shall", "unit shall", "repeat order", "expression of interest",
    "process control", "inventory levels", "registered supplier",
    "safe custody", "un-opened bids", "product material director",
    "subsidiary", "joint venture compan",
)
# Sections that are definitions or BHEL-internal directives by design; a tender
# document was never meant to restate them.
_INTERNAL_SECTIONS = {
    "bhel_purchase_policy": {"2.0", "3.0", "35.0"},
    "bhel_works_policy": {"2.0", "3.0", "36.0"},
}
_TENDER_FACING_MARKERS = (
    "nit", "tender document", "bid document", "bidder", "supplier", "vendor",
    "offer", "emd", "bid security", "earnest money", "specification",
    "delivery", "warranty", "guarantee", "liquidated", "ld ", "payment terms",
    "price bid", "techno-commercial", "pre-qualification", "submission",
    "opening of", "local content", "make in india", "mse", "startup",
    "corrigendum", "pre-bid", "eligibility", "experience", "turnover",
    "tender box", "reverse auction", "loading criteria", "deviation",
)


def is_tender_facing(clause: dict[str, Any], policy_id: str | None = None) -> bool:
    """Heuristic split between clauses a tender/NIT must address (True) and
    BHEL-internal process rules a tender document was never meant to contain
    (False). Fails toward visibility: unclear clauses stay tender-facing."""
    section = str(clause.get("section") or "")
    clause_id = str(clause.get("clause_id") or "")
    if (str(policy_id or ""), clause_id) in {
        ("bhel_purchase_policy", "8.7"),
        ("bhel_works_policy", "9.7"),
    }:
        return True
    internal_sections = _INTERNAL_SECTIONS.get(str(policy_id or ""), set())
    if not internal_sections:
        internal_sections = set().union(*_INTERNAL_SECTIONS.values())
    if section in internal_sections:
        return False
    text = f"{clause.get('title') or ''} {clause.get('text') or ''}".lower()
    internal = any(marker in text for marker in _INTERNAL_MARKERS)
    facing = any(marker in text for marker in _TENDER_FACING_MARKERS)
    if internal and any(
        marker in text
        for marker in ("committee", "competent authority", "next higher authority", "delegation of powers", "approval of")
    ):
        return False
    if facing:
        return not internal or any(
            marker in text
            for marker in (
                "bidder shall", "bidders shall", "bidder must", "supplier shall",
                "shall be stated in the nit", "shall be indicated in the nit",
                "shall be mentioned in the nit", "stated in the tender",
            )
        )
    return not internal


def lexical_rank(query: str, texts: list[str]) -> list[int]:
    """Deterministic TF-IDF-ish overlap ranking; returns indices best-first."""
    query_tokens = token_set(query)
    if not query_tokens or not texts:
        return []
    tokenized = [token_set(text) for text in texts]
    document_frequency = {token: 0 for token in query_tokens}
    for tokens in tokenized:
        for token in query_tokens & tokens:
            document_frequency[token] += 1
    total = len(texts)
    scored = []
    for index, tokens in enumerate(tokenized):
        score = sum(
            math.log((total + 1) / (document_frequency[token] + 0.5))
            for token in query_tokens & tokens
        )
        scored.append((score, index))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [index for score, index in scored if score > 0]
