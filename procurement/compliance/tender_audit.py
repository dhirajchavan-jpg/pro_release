"""Full-package policy compliance audit. Only verified problems are displayed."""

from __future__ import annotations

import math
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from procurement.compliance import rules
from procurement.compliance.approvals import approvals_for_finding
from procurement.compliance.corpus import (
    clause_embedding_index,
    lexical_rank,
    load_corpus,
    normalize_ws,
    offline_mode,
)
from procurement.compliance.profile import (
    build_mechanism_profile,
    select_governing_policies,
)
from procurement.platform import llm as llm_runtime
from procurement.platform.prompts import prompt_text

Progress = Callable[[str, dict[str, Any]], None]
_TOP_K = max(3, int(__import__("os").getenv("COMPLIANCE_RETRIEVAL_TOP_K", "8")))
_CHECK_BATCH = max(1, int(__import__("os").getenv("COMPLIANCE_ACTIVE_BATCH_SIZE", "6")))
_PROVISION_BATCH = max(1, int(os.getenv("COMPLIANCE_TENDER_BATCH_SIZE", "8")))
_PROVISION_CHARS = max(600, int(os.getenv("COMPLIANCE_TENDER_CHUNK_CHARS", "1800")))
_MAX_CANDIDATES = max(_TOP_K, int(os.getenv("COMPLIANCE_MAX_CONTEXT_CANDIDATES", "24")))
_FALLBACK_PROVISION_TOP_K = 3


def run_compliance_audit(
    understanding: dict[str, Any],
    policy_id: str | None = None,
    *,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Review every governing policy clause, then independently verify findings."""

    def emit(stage: str, detail: dict[str, Any] | None = None) -> None:
        if progress:
            progress(stage, detail or {})

    policies_to_audit, detection = select_governing_policies(understanding, policy_id)
    profile = build_mechanism_profile(understanding)
    if not policies_to_audit:
        coverage = {
            "complete": False,
            "audit_mode": "policy_selection_required",
            "source_units": len(rules.source_units(understanding)),
            "source_units_with_text": 0,
            "source_unit_ids": sorted(rules.source_units(understanding)),
            "tender_lines": 0,
            "tender_lines_audited": 0,
            "tender_lines_failed": 0,
            "policy_clauses": 0,
            "tender_facing_policy_clauses": 0,
            "policy_clauses_audited": 0,
            "policy_clauses_not_triggered": 0,
            "policy_clauses_failed": 0,
            "active_checks": [],
            "mechanism_profile": profile,
            "retrieval_warnings": [],
            "failures": [{"stage": "policy_selection", "message": detection["reason"]}],
        }
        return {
            "schema_version": "tender-compliance.v2",
            "policy_id": "",
            "policy_name": "",
            "policies_audited": [],
            "detection": detection,
            "status": "policy_selection_required",
            "coverage": coverage,
            "line_coverage": [],
            "clause_coverage": [],
            "findings": [],
            "summary": _summary([], coverage),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    units = rules.source_units(understanding)
    failures: list[dict[str, Any]] = []
    if not any(normalize_ws(unit.get("text")) for unit in units.values()):
        failures.append(
            {
                "stage": "source_extraction",
                "message": "No extracted tender text was available for compliance review.",
            }
        )

    all_findings: list[dict[str, Any]] = []
    line_reviews: list[dict[str, Any]] = []
    names: list[str] = []
    expected_line_checks = 0
    facing_clause_keys: set[tuple[str, str]] = set()
    relevant_clause_keys: set[tuple[str, str]] = set()
    audited_clause_keys: set[tuple[str, str]] = set()
    failed_clause_keys: set[tuple[str, str]] = set()
    verification_audit: list[dict[str, Any]] = []
    package_decisions: dict[tuple[str, str], dict] = {}
    verification_failed_policies: set[str] = set()

    for current_policy in policies_to_audit:
        corpus = load_corpus(current_policy)
        names.append(str(corpus.get("name") or current_policy))
        from procurement.compliance.package_review import audit_package
        facing_keys = {(current_policy, str(clause["clause_id"])) for clause in corpus["clauses"]}
        facing_clause_keys.update(facing_keys)
        relevant_clause_keys.update(facing_keys)
        original_units = {key: unit for key, unit in units.items() if normalize_ws(unit.get("text"))}
        expected_line_checks += len(original_units)
        emit("policy_package_review_started", {"policy_id": current_policy, "clauses": len(facing_keys)})
        def retain_review(rows):
            for row in rows:
                key = (current_policy, row["clause_id"])
                package_decisions[key] = row
                audited_clause_keys.add(key)
        try:
            field_understanding = understanding.get("field_understanding") or understanding
            source_ledger = field_understanding.get("source_obligations") or {}
            findings, reviewed = audit_package(
                corpus,
                units,
                call=llm_runtime.llm_json,
                progress=emit,
                on_clause_reviewed=retain_review,
                tender_context={
                    "profile": understanding.get("tender_profile") or {},
                    "source_obligations": source_ledger.get("items") or [],
                },
            )
        except Exception as exc:
            remaining = facing_keys - audited_clause_keys
            failed_clause_keys.update(remaining)
            verification_failed_policies.add(current_policy)
            failures.append({"stage": "policy_package_review", "policy_id": current_policy,
                             "message": str(exc), "members": sorted(key[1] for key in remaining)})
            continue
        audited_clause_keys.update(facing_keys)
        package_decisions.update({(current_policy, row["clause_id"]): row for row in reviewed})
        line_reviews.extend({"line_id": key, "unit_id": key, "policy_id": current_policy,
                             "audit_status": "audited", "outcome": "Searched by the policy-clause source index",
                             "document_id": unit.get("document"), "page": unit.get("page")}
                            for key, unit in original_units.items())
        from procurement.compliance.finding_review import review_findings

        candidates = _merge_findings(corpus, findings)
        try:
            verified, decisions = review_findings(candidates, corpus, units, call=llm_runtime.llm_json)
            all_findings.extend(verified)
            verification_audit.extend({"policy_id": current_policy, **row} for row in decisions)
        except Exception as exc:
            verification_failed_policies.add(current_policy)
            failures.append({"stage": "finding_verification", "policy_id": current_policy,
                             "message": str(exc), "members": [item["finding_id"] for item in candidates]})

    unique_findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in all_findings:
        key = str(finding.get("finding_id") or finding.get("title") or "")
        if not key or key in seen:
            continue
        if finding.get("status") not in {"violation", "needs_review"}:
            continue
        seen.add(key)
        unique_findings.append(_enrich_finding(finding))
    from procurement.compliance.officer_language import apply_officer_language

    apply_officer_language(unique_findings)

    complete = not failures and set(package_decisions) == relevant_clause_keys and len(line_reviews) == expected_line_checks and all(
        review.get("audit_status") == "audited" for review in line_reviews
    )
    coverage = {
        "complete": complete,
        "audit_mode": "full_package_clause_review",
        "source_units": len(units),
        "source_units_with_text": sum(
            1 for unit in units.values() if normalize_ws(unit.get("text"))
        ),
        "source_unit_ids": sorted(units),
        "tender_lines": expected_line_checks,
        "tender_lines_audited": sum(
            1 for review in line_reviews if review.get("audit_status") == "audited"
        ),
        "tender_lines_failed": sum(
            1 for review in line_reviews if review.get("audit_status") != "audited"
        ) + max(0, expected_line_checks - len(line_reviews)),
        "policy_clauses": sum(
            int(load_corpus(policy).get("clause_count") or 0) for policy in policies_to_audit
        ),
        "tender_facing_policy_clauses": len(facing_clause_keys),
        "policy_clauses_audited": len(audited_clause_keys),
        "policy_clauses_not_triggered": sum(row["outcome"] == "not_applicable" for row in package_decisions.values()),
        "policy_clauses_unassessed": len(relevant_clause_keys - set(package_decisions)),
        "policy_clauses_failed": len(failed_clause_keys),
        "active_checks": [],
        "mechanism_profile": profile,
        "retrieval_warnings": [],
        "failures": failures,
        "finding_verification": verification_audit,
    }
    summary = _summary(unique_findings, coverage)
    status = "completed" if complete else "incomplete"
    display = names[0] if len(names) == 1 else " & ".join(names)
    findings_by_clause: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for finding in unique_findings:
        reference = finding.get("policy_reference")
        reference = reference if isinstance(reference, dict) else {}
        key = (
            str(reference.get("policy_id") or ""),
            str(finding.get("clause_id") or ""),
        )
        if all(key):
            findings_by_clause.setdefault(key, []).append(finding)
    clause_records: dict[tuple[str, str], dict[str, Any]] = {}
    policy_names: dict[str, str] = {}
    for policy_id in policies_to_audit:
        corpus = load_corpus(policy_id)
        policy_names[policy_id] = str(corpus.get("name") or policy_id)
        for clause in corpus.get("clauses") or []:
            clause_id = str(clause.get("clause_id") or "")
            key = (policy_id, clause_id)
            if key in relevant_clause_keys:
                clause_records[key] = clause
    clause_coverage = []
    verified_ids = {str(item["finding_id"]) for item in unique_findings}
    duplicate_targets = {(row["policy_id"], row["finding_id"]): row["duplicate_of"]
                         for row in verification_audit if row.get("decision") == "duplicate"}
    for (policy_id, clause_id), item in sorted(clause_records.items()):
        clause_coverage.append(
            {
                "policy_id": policy_id,
                "policy_name": policy_names.get(policy_id, policy_id),
                "clause_id": clause_id,
                "section": item.get("section"),
                "title": item.get("title") or item.get("section_title") or "",
                "clause_quote": normalize_ws(item.get("text")),
                "audit_status": (
                    "failed"
                    if (policy_id, clause_id) in failed_clause_keys
                    else "audited"
                    if (policy_id, clause_id) in audited_clause_keys
                    else "not_triggered"
                ),
                "outcome": "applicability_unassessed",
                "finding_ids": [
                    finding.get("finding_id")
                    for finding in findings_by_clause.get((policy_id, clause_id), [])
                ],
            }
        )
        reviewed = package_decisions.get((policy_id, clause_id))
        if reviewed:
            final_ids = clause_coverage[-1]["finding_ids"]
            linked_ids = list(dict.fromkeys(
                duplicate_targets.get((policy_id, candidate), candidate)
                for candidate in reviewed.get("finding_ids", [])
                if duplicate_targets.get((policy_id, candidate), candidate) in verified_ids))
            clause_coverage[-1].update(reviewed)
            clause_coverage[-1]["finding_ids"] = list(dict.fromkeys([*final_ids, *linked_ids]))
            clause_coverage[-1]["candidate_outcome"] = reviewed["outcome"]
            if reviewed["outcome"] == "findings" and not final_ids and linked_ids:
                clause_coverage[-1]["outcome"] = "covered_by_verified_finding"
            elif reviewed["outcome"] == "findings" and not final_ids:
                clause_coverage[-1]["outcome"] = (
                    "verification_incomplete" if policy_id in verification_failed_policies else "no_verified_issue")
    emit("compliance_audit_completed", {"status": status, "findings": len(unique_findings), **summary})
    return {
        "schema_version": "tender-compliance.v2",
        "policy_id": policies_to_audit[0],
        "policy_name": display,
        "policies_audited": policies_to_audit,
        "detection": detection,
        "status": status,
        "coverage": coverage,
        "line_coverage": line_reviews,
        "clause_coverage": clause_coverage,
        "findings": unique_findings,
        "summary": summary,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def _tender_provisions(units: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    provisions: list[dict[str, Any]] = []
    for unit_id, unit in sorted(
        units.items(), key=lambda item: (item[1].get("page") or 0, item[0])
    ):
        text = normalize_ws(unit.get("text"))
        for offset in range(0, len(text), _PROVISION_CHARS):
            chunk = text[offset:offset + _PROVISION_CHARS]
            provisions.append(
                {
                    "line_id": f"{unit_id}:C{offset // _PROVISION_CHARS + 1}",
                    "unit_id": unit_id,
                    "document_id": unit.get("document"),
                    "page": unit.get("page"),
                    "text": chunk,
                }
            )
    return provisions


def _coverage_vectors(
    policy_id: str,
    provisions: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[list[list[float]], list[list[float]]]:
    if offline_mode() or not provisions or not clauses:
        return [], []
    try:
        from ai_provider import embed_texts

        vectors: list[list[float]] = []
        batch_size = max(1, int(os.getenv("COMPLIANCE_EMBED_BATCH_SIZE", "32")))
        texts = [item["text"] for item in provisions]
        for start in range(0, len(texts), batch_size):
            vectors.extend(embed_texts(texts[start:start + batch_size]))
        clause_index = clause_embedding_index(policy_id)
        clause_vectors = [
            clause_index.get(str(clause.get("clause_id") or ""), [])
            for clause in clauses
        ]
        if len(vectors) != len(provisions) or any(not vector for vector in clause_vectors):
            raise RuntimeError("embedding response did not cover every tender provision and policy clause")
        return vectors, clause_vectors
    except Exception as exc:
        warnings.append(
            {
                "stage": "compliance_retrieval",
                "message": f"Semantic retrieval was unavailable; every provision was still audited with lexical retrieval: {exc}",
            }
        )
        return [], []


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else -1.0


def _rank_candidates(
    query: str,
    candidates: list[str],
    query_vector: list[float] | None,
    candidate_vectors: list[list[float]],
) -> list[int]:
    lexical = lexical_rank(query, candidates)[:_TOP_K]
    if not query_vector or len(candidate_vectors) != len(candidates):
        return lexical
    semantic = sorted(
        range(len(candidates)),
        key=lambda index: (-_cosine(query_vector, candidate_vectors[index]), index),
    )[:_TOP_K]
    return list(dict.fromkeys([*semantic, *lexical]))[: _TOP_K * 2]


def _round_robin_unique(rankings: list[list[int]]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for rank in range(max((len(items) for items in rankings), default=0)):
        for items in rankings:
            if rank >= len(items) or items[rank] in seen:
                continue
            seen.add(items[rank])
            result.append(items[rank])
            if len(result) >= _MAX_CANDIDATES:
                return result
    return result


def _audit_tender_provisions(
    corpus: dict[str, Any],
    units: dict[str, dict[str, Any]],
    provisions: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
    emit: Callable[[str, dict[str, Any]], None],
    retrieval_warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], set[str]]:
    if not provisions:
        return [], [], set(), set()
    if not clauses:
        return [], [{"stage": "tender_provisions", "message": "No tender-facing policy clauses were available."}], set(), set()
    provision_vectors, clause_vectors = _coverage_vectors(
        str(corpus.get("policy_id") or ""), provisions, clauses, retrieval_warnings
    )
    clause_texts = [
        f"{clause.get('title') or ''} {clause.get('text') or ''}" for clause in clauses
    ]
    positions = {item["line_id"]: index for index, item in enumerate(provisions)}
    batches = [
        provisions[start:start + _PROVISION_BATCH]
        for start in range(0, len(provisions), _PROVISION_BATCH)
    ]
    workers = max(1, int(os.getenv("COMPLIANCE_LLM_CONCURRENCY", "4")))
    ordered: dict[int, list[dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    forward_plans: list[tuple[int, list[dict[str, Any]], list[dict[str, Any]]]] = []
    audited_clause_ids: set[str] = set()
    unmatched_clause_ids: set[str] = set()
    successful_forward: set[int] = set()

    def plan(batch_index: int, batch: list[dict[str, Any]]) -> None:
        rankings = []
        for provision in batch:
            position = positions[provision["line_id"]]
            rankings.append(
                _rank_candidates(
                    provision["text"],
                    clause_texts,
                    provision_vectors[position]
                    if len(provision_vectors) == len(provisions)
                    else None,
                    clause_vectors,
                )
            )
        if any(not ranking for ranking in rankings):
            retrieval_warnings.append(
                {
                    "stage": "compliance_retrieval",
                    "message": "A tender batch had no reliable clause match; the complete tender-facing policy set was used for that batch.",
                    "batch": batch_index + 1,
                }
            )
            selected = list(clauses)
        else:
            selected = [clauses[index] for index in _round_robin_unique(rankings)]
        forward_plans.append((batch_index, batch, selected))

    for index, batch in enumerate(batches):
        plan(index, batch)

    def run(item: tuple[int, list[dict[str, Any]], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        _, batch, selected = item
        return _audit_provision_batch(corpus, units, batch, selected)

    with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as executor:
        futures = {executor.submit(run, item): item[0] for item in forward_plans}
        for future in as_completed(futures):
            index = futures[future]
            try:
                ordered[index] = future.result()
                successful_forward.add(index)
                emit("tender_provisions_batch_completed", {"batch": index + 1, "batches": len(batches)})
            except Exception as exc:
                ordered[index] = []
                failures.append(
                    {
                        "stage": "tender_provisions",
                        "section": "tender_provisions",
                        "batch": index + 1,
                        "message": str(exc) or type(exc).__name__,
                        "members": [item["line_id"] for item in batches[index]],
                    }
                )
    reviews_by_line = {
        review["line_id"]: review
        for index in range(len(batches))
        for review in ordered.get(index, [])
        if review.get("line_id")
    }
    for index, _, selected in forward_plans:
        if index not in successful_forward:
            continue
        audited_clause_ids.update(
            str(clause.get("clause_id") or "") for clause in selected
        )

    missing = [
        clause
        for clause in clauses
        if str(clause.get("clause_id") or "") not in audited_clause_ids
    ]
    if missing:
        provision_texts = [str(item.get("text") or "") for item in provisions]
        fallback_groups: dict[int, list[dict[str, Any]]] = {}
        clause_positions = {
            str(clause.get("clause_id") or ""): index
            for index, clause in enumerate(clauses)
        }
        for clause in missing:
            clause_index = clause_positions.get(str(clause.get("clause_id") or ""), -1)
            ranked = _rank_candidates(
                f"{clause.get('title') or ''} {clause.get('text') or ''}",
                provision_texts,
                clause_vectors[clause_index]
                if 0 <= clause_index < len(clause_vectors)
                else None,
                provision_vectors,
            )[:_FALLBACK_PROVISION_TOP_K]
            if not ranked:
                unmatched_clause_ids.add(str(clause.get("clause_id") or ""))
                continue
            fallback_groups.setdefault(ranked[0] // _PROVISION_BATCH, []).append(clause)
        fallback_plans: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        for group_index, group_clauses in sorted(fallback_groups.items()):
            group_provisions = provisions[
                group_index * _PROVISION_BATCH:(group_index + 1) * _PROVISION_BATCH
            ]
            for start in range(0, len(group_clauses), _MAX_CANDIDATES):
                fallback_plans.append(
                    (
                        group_provisions,
                        group_clauses[start:start + _MAX_CANDIDATES],
                    )
                )
        emit(
            "tender_provisions_fallback_started",
            {"batches": len(fallback_plans), "clauses": len(missing)},
        )
        if fallback_plans:
            with ThreadPoolExecutor(max_workers=min(workers, len(fallback_plans))) as executor:
                futures = {
                    executor.submit(_audit_provision_batch, corpus, units, batch, selected): (batch, selected)
                    for batch, selected in fallback_plans
                }
                for future in as_completed(futures):
                    batch, selected = futures[future]
                    try:
                        additions = future.result()
                        audited_clause_ids.update(
                            str(clause.get("clause_id") or "") for clause in selected
                        )
                        for review in additions:
                            line_id = review.get("line_id")
                            if not line_id:
                                continue
                            existing = reviews_by_line.get(line_id)
                            if existing is None:
                                reviews_by_line[line_id] = review
                                continue
                            existing["findings"] = [
                                *(existing.get("findings") or []),
                                *(review.get("findings") or []),
                            ]
                            existing["clause_ids"] = list(
                                dict.fromkeys(
                                    [
                                        *(existing.get("clause_ids") or []),
                                        *(review.get("clause_ids") or []),
                                    ]
                                )
                            )
                            if review.get("outcome") in {"violation", "needs_review"}:
                                existing["outcome"] = review["outcome"]
                    except Exception as exc:
                        failures.append(
                            {
                                "stage": "policy_clause_coverage",
                                "section": "policy_clauses",
                                "message": str(exc) or type(exc).__name__,
                                "members": [
                                    str(clause.get("clause_id") or "") for clause in selected
                                ],
                            }
                        )
    reviews = [
        reviews_by_line[item["line_id"]]
        for item in provisions
        if item["line_id"] in reviews_by_line
    ]
    return reviews, failures, audited_clause_ids, unmatched_clause_ids


def _audit_provision_batch(
    corpus: dict[str, Any],
    units: dict[str, dict[str, Any]],
    provisions: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    locations = {(row.get("document_id"), int(row.get("page") or 0) + offset)
                 for row in provisions for offset in (-1, 0, 1)}
    result = llm_runtime.llm_json(
        prompt_text(
            "tender_provision_compliance_audit",
            "Review every supplied tender provision against the governing policy. Return only JSON.",
        ),
        {
            "task": "tender_provision_compliance_audit",
            "section": "tender_provisions",
            "policy": {"policy_id": corpus.get("policy_id"), "name": corpus.get("name")},
            "instructions": (
                "Return exactly one result for every line_id. Read each provision in its full context. "
                "A tender provision is not a problem merely because it does not mention a policy clause. "
                "Flag only a direct contradiction, an unsafe ambiguity, or a policy-required statement that "
                "the provision invokes but omits. Use only supplied clause_ids. The tender_quote must be "
                "verbatim from that provision. Do not create a finding merely because the provision mentions "
                "a portal, standard, guideline, policy, annexure, or other external reference. Such references "
                "are relevant only when the provision itself creates a direct policy contradiction or makes "
                "evaluation against the supplied policy impossible. Do not apply a policy clause merely "
                "because a reference exists; include a clause_id only when the supplied policy text actually "
                "governs that provision."
            ),
            "tender_provisions": provisions,
            "surrounding_context": [{"unit_id": key, **unit} for key, unit in units.items()
                                    if (unit.get("document"), int(unit.get("page") or 0)) in locations
                                    and unit.get("unit_type") != "anchor_fact"],
            "candidate_policy_clauses": [_clause_payload(clause) for clause in clauses],
            "json_shape": {
                "provisions": [
                    {
                        "line_id": "exact line_id",
                        "status": "no_policy_issue | compliant | violation | needs_review",
                        "findings": [{"status": "violation | needs_review",
                                      "clause_id": "exact supplied clause id",
                                      "tender_quote": "verbatim quote from the tender provision",
                                      "rationale": "short objective explanation"}],
                    }
                ]
            },
        },
        max_output_tokens=max(1200, 360 * len(provisions)),
    )
    values = result.get("provisions")
    if not isinstance(values, list):
        raise RuntimeError("model omitted the tender provision results array")
    by_id = {
        str(value.get("line_id") or ""): value
        for value in values
        if isinstance(value, dict) and value.get("line_id")
    }
    if len(by_id) != len(values) or set(by_id) != {row["line_id"] for row in provisions}:
        raise RuntimeError("model returned duplicate, unknown or omitted tender provisions")
    clause_map = {str(clause.get("clause_id") or ""): clause for clause in clauses}
    reviews: list[dict[str, Any]] = []
    for provision in provisions:
        verdict = by_id.get(provision["line_id"])
        if verdict is None:
            raise RuntimeError(f"model omitted tender provision {provision['line_id']}")
        status = str(verdict.get("status") or "needs_review")
        if status not in {"no_policy_issue", "compliant", "violation", "needs_review"}:
            raise RuntimeError("model returned invalid provision outcome")
        values_for_line = verdict.get("findings")
        if not isinstance(values_for_line, list):
            raise RuntimeError("model omitted provision findings array")
        if bool(values_for_line) != (status in {"violation", "needs_review"}):
            raise RuntimeError("provision outcome contradicts its findings")
        findings, clause_ids = [], []
        for value in values_for_line:
            clause_id = str(value.get("clause_id") or "")
            if clause_id not in clause_map or value.get("status") not in {"violation", "needs_review"}:
                raise RuntimeError("model returned unknown policy clause or invalid finding status")
            findings.append(_provision_finding(corpus, units, provision, clause_map[clause_id], value))
            clause_ids.append(clause_id)
        reviews.append(
            {
                "policy_id": corpus.get("policy_id"),
                "line_id": provision["line_id"],
                "unit_id": provision["unit_id"],
                "document_id": provision.get("document_id"),
                "page": provision.get("page"),
                "audit_status": "audited",
                "outcome": status,
                "clause_ids": list(dict.fromkeys(clause_ids)),
                "findings": findings,
            }
        )
    return reviews


def _provision_finding(
    corpus: dict[str, Any],
    units: dict[str, dict[str, Any]],
    provision: dict[str, Any],
    clause: dict[str, Any] | None,
    verdict: dict[str, Any],
) -> dict[str, Any]:
    clause_id = str((clause or {}).get("clause_id") or "")
    quote = str(verdict.get("tender_quote") or "").strip()
    # A quote from another page must not validate a finding against this provision.
    grounded = (
        provision["unit_id"]
        if quote and normalize_ws(quote).casefold() in normalize_ws(provision["text"]).casefold()
        else None
    )
    status = str(verdict.get("status") or "needs_review")
    if not quote or grounded is None:
        raise RuntimeError(f"ungrounded tender quote for {provision['line_id']}")
    evidence = (
        [
            {
                "quote": quote,
                "page": provision.get("page"),
                "unit_id": provision["unit_id"],
                "document_id": provision.get("document_id"),
            }
        ]
        if grounded
        else []
    )
    return _finding(
        corpus,
        finding_id=f"{corpus.get('policy_id')}:{clause_id}:{provision['line_id']}:" + hashlib.sha256(
            (quote + str(verdict.get("rationale") or "")).encode()).hexdigest()[:12],
        clause=clause,
        status=status,
        title=(
            f"Tender provision conflicts with Clause {clause_id}"
            if status == "violation"
            else f"Tender provision needs confirmation against Clause {clause_id}"
        ),
        detail=str(verdict.get("rationale") or "Confirm this tender provision against the cited policy clause."),
        evidence=evidence,
        tender_provision=provision["text"],
        origin="tender_provision_audit",
    )


def _clause_payload(clause: dict[str, Any]) -> dict[str, Any]:
    return {
        "clause_id": clause.get("clause_id"),
        "title": clause.get("title") or clause.get("section_title") or "",
        "text": str(clause.get("text") or ""),
    }


def _audit_active_checks(
    corpus: dict[str, Any],
    units: dict[str, dict[str, Any]],
    checks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    if not checks:
        return [], 0, []
    unit_ids = sorted(units)
    unit_texts = [str(units[unit_id].get("text") or "") for unit_id in unit_ids]
    findings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    judged = 0
    for start in range(0, len(checks), _CHECK_BATCH):
        batch = checks[start:start + _CHECK_BATCH]
        try:
            batch_findings = _audit_check_batch(corpus, units, unit_ids, unit_texts, batch)
        except Exception as exc:
            failures.append(
                {
                    "stage": "active_checks",
                    "section": "active_checks",
                    "batch": start // _CHECK_BATCH + 1,
                    "message": str(exc) or type(exc).__name__,
                    "members": [item.get("clause_id") for item in batch],
                }
            )
            continue
        judged += len(batch)
        findings.extend(batch_findings)
    return findings, judged, failures


def _audit_check_batch(
    corpus: dict[str, Any],
    units: dict[str, dict[str, Any]],
    unit_ids: list[str],
    unit_texts: list[str],
    checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads = []
    for check in checks:
        query = " ".join(
            [
                str(check.get("title") or ""),
                str(check.get("required_statement") or (check.get("applicability") or {}).get("required_statement") or ""),
                str(check.get("text") or "")[:400],
            ]
        )
        ranked = lexical_rank(query, unit_texts)[:_TOP_K]
        excerpts = [
            {
                "unit_id": unit_ids[index],
                "page": units[unit_ids[index]].get("page"),
                "text": unit_texts[index],
            }
            for index in ranked
            if unit_texts[index].strip()
        ]
        applicability = check.get("applicability") or {}
        payloads.append(
            {
                "clause_id": check.get("clause_id"),
                "title": check.get("title") or check.get("section_title") or "",
                "clause_text": check.get("text") or "",
                "activation": check.get("activation") or "",
                "required_statement": applicability.get("required_statement") or "",
                "trigger": applicability.get("trigger") or "",
                "excerpts": excerpts,
            }
        )
    result = llm_runtime.llm_json(
        prompt_text(
            "active_check_audit",
            "You are a BHEL tender auditor. Return only JSON. Report only real problems.",
        ),
        {
            "task": "active_check_audit",
            "section": "active_checks",
            "policy": {"policy_id": corpus.get("policy_id"), "name": corpus.get("name")},
            "instructions": (
                "Each check is a candidate; establish applicability from the policy and tender evidence, "
                "including exceptions, before reporting a problem. "
                "Return a verdict only when there is a real problem: the tender contradicts "
                "the clause, or it uses the trigger but omits the required statement. "
                "If the tender is compliant or the excerpts are silent without a contradiction, omit that check. "
                "Never invent a clause. violation requires a verbatim tender_quote from an excerpt."
            ),
            "checks": payloads,
            "json_shape": {
                "verdicts": [
                    {
                        "clause_id": "exact clause id",
                        "verdict": "violation | needs_review",
                        "tender_quote": "verbatim quote from an excerpt or empty",
                        "title": "short officer-facing problem title",
                        "rationale": "one or two sentences: what the tender says, why that is a problem",
                    }
                ]
            },
        },
        max_output_tokens=max(900, 280 * len(checks)),
    )
    values = result.get("verdicts")
    if not isinstance(values, list):
        raise RuntimeError("model omitted the active-check verdicts array")
    check_map = {str(item.get("clause_id") or ""): item for item in checks}
    findings: list[dict[str, Any]] = []
    for verdict in values:
        if not isinstance(verdict, dict):
            raise RuntimeError("Invalid active-check verdict")
        clause_id = str(verdict.get("clause_id") or "")
        check = check_map.get(clause_id)
        if check is None:
            raise RuntimeError(f"model returned an unknown active policy clause: {clause_id}")
        outcome = str(verdict.get("verdict") or "needs_review")
        if outcome not in {"violation", "needs_review"}:
            raise RuntimeError(f"model returned an invalid active-check outcome: {outcome}")
        quote = str(verdict.get("tender_quote") or "").strip()
        grounded = rules._ground_quote(quote, units) if quote else None
        status = "violation" if outcome == "violation" and grounded is not None else "needs_review"
        title = str(verdict.get("title") or "").strip() or (
            f"Tender conflicts with Clause {clause_id}"
            if status == "violation"
            else f"Clause {clause_id} needs confirmation"
        )
        detail = str(verdict.get("rationale") or "").strip()
        if grounded is None and quote:
            detail = (
                "The cited wording could not be confirmed in the extracted tender text. "
                + (detail or "Confirm this point against the tender.")
            )
        evidence = []
        if grounded:
            evidence.append(
                {
                    "quote": quote,
                    "page": units[grounded].get("page"),
                    "unit_id": grounded,
                    "document_id": units[grounded].get("document"),
                }
            )
        findings.append(
            _finding(
                corpus,
                finding_id=f"{corpus.get('policy_id')}:{clause_id}:active:" + hashlib.sha256(
                    (quote + detail).encode()).hexdigest()[:12],
                clause=check,
                status=status,
                title=title,
                detail=detail or "Confirm this policy requirement against the tender wording.",
                evidence=evidence,
                tender_provision=quote,
                origin="active_check_audit",
            )
        )
    return findings


def _enrich_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Attach DoP titles. Officer wording is filled later by the live LLM."""
    finding["approvals"] = approvals_for_finding(finding)
    return finding


def _finding(
    corpus: dict[str, Any],
    *,
    finding_id: str,
    clause: dict[str, Any] | None,
    status: str,
    title: str,
    detail: str,
    evidence: list[dict[str, Any]],
    tender_provision: str,
    origin: str,
) -> dict[str, Any]:
    clause = clause or {}
    clause_id = str(clause.get("clause_id") or "")
    return {
        "finding_id": finding_id,
        "clause_id": clause_id,
        "clause_title": str(clause.get("title") or clause.get("section_title") or ""),
        "clause_quote": normalize_ws(clause.get("text")),
        "policy_reference": (
            {
                "policy_id": corpus.get("policy_id"),
                "policy_name": corpus.get("name"),
                "clause_id": clause_id,
            }
            if clause_id
            else {}
        ),
        "tender_provision": tender_provision,
        "severity": "violation" if status == "violation" else "warning",
        "status": status,
        "title": title,
        "detail": detail,
        "required_action": (
            "Correct the tender provision or record an approved exception with the named authority, then run the audit again."
            if status == "violation"
            else "Confirm the tender wording, then resolve or waive with the named approving authority."
        ),
        "consequence": (
            "The evaluation checklist stays locked until this is corrected or an authorized exception is recorded."
        ),
        "tender_evidence": evidence,
        "origin": origin,
        "resolution": {
            "status": "open",
            "note": "",
            "resolved_by": "",
            "resolved_at": "",
        },
    }


def _merge_findings(
    corpus: dict[str, Any], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for finding in findings:
        if not isinstance(finding, dict) or not finding.get("finding_id"):
            continue
        existing = merged.get(str(finding["finding_id"]))
        if existing is None:
            merged[str(finding["finding_id"])] = finding
    return list(merged.values())


def _summary(findings: list[dict[str, Any]], coverage: dict[str, Any]) -> dict[str, Any]:
    return {
        "violations": sum(1 for finding in findings if finding.get("status") == "violation"),
        "needs_review": sum(1 for finding in findings if finding.get("status") == "needs_review"),
        "open": sum(
            1
            for finding in findings
            if (finding.get("resolution") or {}).get("status") == "open"
            and finding.get("status") in {"violation", "needs_review"}
        ),
        "coverage_complete": bool(coverage.get("complete")),
        "tender_lines_audited": int(coverage.get("tender_lines_audited") or 0),
        "tender_lines": int(coverage.get("tender_lines") or 0),
        "policy_clauses_audited": int(coverage.get("policy_clauses_audited") or 0),
        "policy_clauses_not_triggered": int(coverage.get("policy_clauses_not_triggered") or 0),
        "tender_facing_policy_clauses": int(coverage.get("tender_facing_policy_clauses") or 0),
    }
