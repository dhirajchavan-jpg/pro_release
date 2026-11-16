"""Deterministic policy-treatment card assembly for bid findings."""

from __future__ import annotations

from typing import Any

from procurement.compliance.corpus import normalize_ws

from .next_ask_facts import (
    _as_dict,
    _deviation_kind,
    _evidence_state,
    _is_post_award_finding,
    _mii_is_eligibility,
    _tender_effect,
    already_have,
    classify_family,
    missing_papers,
)
from .next_ask_policy import (
    _append_policy_clause,
    _approvals,
    _bid_system,
    _clause,
    _clause_map,
    _emd_mse_clause,
    _is_two_part,
    _policy_id,
    _book_name,
    _subject_id,
    _subject_clause,
    PURCHASE,
)
from .next_ask_facts import _PASS


SCHEMA = "bid-policy-next-ask.v1"


def _door(
    *,
    door_id: str,
    open_: bool,
    label: str,
    policy: dict[str, str],
    reason: str = "",
    approvals: list[dict[str, Any]] | None = None,
    communicate_to_all: bool = False,
) -> dict[str, Any]:
    return {
        "id": door_id,
        "open": open_,
        "label": label,
        "clause_id": policy.get("clause_id") or "",
        "policy_id": policy.get("policy_id") or "",
        "policy_name": policy.get("policy_name") or "",
        "clause_title": policy.get("title") or "",
        "clause_quote": policy.get("quote") or "",
        "reason": reason,
        "approvals": approvals or [],
        "communicate_to_all_qualified_bidders": communicate_to_all,
    }


def _ask_once_or_single_part(
    tender: dict[str, Any],
    policy_id: str,
    paper: str,
) -> dict[str, Any]:
    clauses = _clause_map(policy_id)
    bid_system = _bid_system(tender)
    if not policy_id:
        return _door(
            door_id="verify_policy_scope",
            open_=False,
            label="Confirm whether Purchase Policy or Works Policy applies",
            policy=_clause("", ""),
            reason="No policy clause is applied until the procurement type is confirmed.",
        )
    if bid_system == "unknown":
        return _door(
            door_id="verify_bid_system",
            open_=False,
            label="Confirm the bid system before seeking clarification",
            policy=_clause(policy_id, ""),
            reason="The policy treatment differs for single-part and two/three-part bids.",
        )
    if _is_two_part(tender):
        policy = _clause(policy_id, clauses["ask_once"])
        return _door(
            door_id="ask_once",
            open_=True,
            label="Ask once for the missing paper",
            policy=policy,
            reason=(
                f"Two-part evaluation: ask the bidder once, by a cut-off date, to furnish {paper}. "
                "This is not a waiver of the requirement."
            ),
        )
    policy = _clause(policy_id, clauses["single_part"])
    return _door(
        door_id="single_part_closed",
        open_=False,
        label="No post-opening clarification in a single-part bid",
        policy=policy,
        reason="Single-part bids do not allow post-tender-opening clarifications.",
    )


def _no_substitute(
    policy_id: str,
    paper: str,
    *,
    goi: str = "",
    family: str = "",
) -> dict[str, Any]:
    policy = _subject_clause(policy_id, family)
    reason = f"BHEL cannot accept a substitute for {paper}."
    if goi:
        reason = f"{reason} {goi}"
    if not policy.get("clause_id"):
        reason = (
            f"{reason} {_book_name(policy_id)} does not contain a numbered clause "
            "for this paper. The requirement comes from the NIT or the Government rule."
        )
    return _door(
        door_id="no_path",
        open_=False,
        label="No waiver of this paper",
        policy=policy,
        reason=reason,
        approvals=[],
    )


def _record_rejection(policy_id: str) -> dict[str, Any]:
    clause_id = "26.0" if policy_id == PURCHASE else "32.0"
    policy = _clause(policy_id, clause_id)
    title = (
        "Section Head of Indenting/ Purchase group, not below the rank of DGM"
        if policy_id == PURCHASE
        else "Section Head of Indenting/ Contracting group, not below the rank of DGM"
    )
    return _door(
        door_id="record_rejection",
        open_=False,
        label="If the paper is still missing after the cut-off, record the rejection",
        policy=policy,
        reason=(
            "Wherever a committee has not been constituted, rejection of a bid on technical or commercial "
            "grounds is with the approval of the concerned Section Head, not below DGM. That is how the "
            "rejection is recorded. It is not a path to accept a substitute paper."
        ),
        approvals=[
            {
                "action": "Rejection of a bid on technical/commercial grounds when no committee is constituted",
                "authority_title": title,
                "clause_id": clause_id,
                "policy_id": policy_id,
                "quote": policy.get("quote"),
            }
        ],
    )


def _deviation_door(finding: dict[str, Any], policy_id: str) -> dict[str, Any]:
    clauses = _clause_map(policy_id)
    if not clauses:
        return _door(
            door_id="verify_policy_scope",
            open_=False,
            label="Confirm the governing policy before treating the deviation",
            policy=_clause("", ""),
            reason="No deviation clause is applied until the procurement type is confirmed.",
        )
    policy = _clause(policy_id, clauses["deviation"])
    kind = _deviation_kind(finding)
    return _door(
        door_id="deviation_loading",
        open_=True,
        label=(
            "Accept as a technical deviation with loading"
            if kind == "technical"
            else "Accept as a commercial deviation with loading"
        ),
        policy=policy,
        reason=(
            "Only if the bidder is declaring a deviation from the tendered term, not as a way to skip a missing statutory paper. "
            "Frame loading and tell every qualified bidder before price-bid opening."
        ),
        approvals=_approvals(policy_id, clauses["deviation"], kind=kind),
        communicate_to_all=True,
    )


def _finalize_card(
    card: dict[str, Any],
    finding: dict[str, Any],
    tender: dict[str, Any],
) -> dict[str, Any]:
    policy_id = _policy_id(tender)
    bid_system = _bid_system(tender)
    family = str(card.get("family") or "document")
    paper = str(card.get("missing_paper") or "")
    requested_evidence = paper or "the tender-required evidence"
    status = str(finding.get("status") or "").casefold()
    explicitly_missing = bool(
        status in {"missing", "missing_after_complete_search"}
        or any(str(item or "").strip() for item in finding.get("missing_documents") or [])
    )
    needs_internal_review = status in {
        "unclear", "review", "needs_review", "needs_manual_review", "not_evaluated"
    } and not explicitly_missing
    substantive_failure = status in {
        "fail", "failed", "fail_fact", "present_but_invalid", "non_compliant",
        "noncompliant", "does_not_meet", "does_not_meet_requirement",
    } and not explicitly_missing
    effect = _tender_effect(finding, family)
    clauses: list[dict[str, str]] = []

    subject_id = _subject_id(policy_id, family)
    _append_policy_clause(clauses, policy_id, subject_id, "governing")

    if family == "deviation":
        if not policy_id:
            action_code = "verify_policy_scope"
            action_label = "Confirm the governing policy"
            action_reason = "The procurement type is not resolved, so no policy clause can be applied safely."
        elif bid_system == "unknown":
            action_code = "verify_bid_system"
            action_label = "Confirm the bid system before treating the deviation"
            action_reason = "The policy treatment differs for single-part and two/three-part bids."
        elif bid_system == "single_part":
            action_code = "apply_tender_consequence"
            action_label = "Do not seek post-opening deviation treatment; apply the tender consequence"
            action_reason = "The governing policy permits single-part bidding only where deviations are not acceptable."
            _append_policy_clause(clauses, policy_id, _clause_map(policy_id)["single_part"], "bid_procedure")
        elif bid_system == "three_part":
            action_code = "review_clarification_authority"
            action_label = "Confirm the tender-specific clarification procedure"
            action_reason = "The governing policy's standard clarification clause is written for two-part bidding and must not be assumed for a three-part bid."
        else:
            action_code = "apply_deviation_procedure"
            kind = _deviation_kind(finding)
            action_label = f"Refer the {kind} deviation for acceptance and loading"
            action_reason = "Give the bidder an opportunity to withdraw it; if BHEL accepts it, frame loading and communicate the treatment to all qualified bidders before price opening."
            _append_policy_clause(clauses, policy_id, _clause_map(policy_id)["ask_once"], "clarification")
            _append_policy_clause(clauses, policy_id, _clause_map(policy_id)["deviation"], "deviation_loading")
    elif family == "deviation_statement":
        if not policy_id:
            action_code = "verify_policy_scope"
            action_label = "Confirm the governing policy"
        elif bid_system == "unknown":
            action_code = "verify_bid_system"
            action_label = "Confirm the bid system before treating the missing statement"
        elif bid_system == "three_part":
            action_code = "review_clarification_authority"
            action_label = "Confirm the tender-specific clarification procedure"
        elif bid_system == "two_part":
            action_code = "request_clarification_once"
            action_label = f"Request {requested_evidence} once by a recorded cut-off date"
        else:
            action_code = "apply_tender_consequence"
            action_label = "Apply the tender consequence for the missing statement"
        action_reason = "A missing deviation statement is a document gap, not an accepted deviation and not a basis for loading."
        if policy_id and action_code in {"request_clarification_once", "apply_tender_consequence"}:
            role = "clarification" if action_code == "request_clarification_once" else "bid_procedure"
            process_key = "ask_once" if action_code == "request_clarification_once" else "single_part"
            _append_policy_clause(clauses, policy_id, _clause_map(policy_id)[process_key], role)
    elif effect["code"] == "no_bidder_effect":
        action_code = "apply_tender_rule"
        action_label = "Apply the tender rule; do not request bidder evidence"
        action_reason = "No missing bidder document needs to be cured for this check."
    elif needs_internal_review:
        action_code = "review_evidence"
        action_label = "Review the cited evidence before contacting the bidder"
        action_reason = normalize_ws(card.get("reason") or "") or "The current evidence is inconclusive."
    elif not policy_id:
        action_code = "verify_policy_scope"
        action_label = "Confirm whether Purchase Policy or Works Policy applies"
        action_reason = "No policy clause is applied until the procurement type is confirmed."
    elif bid_system == "unknown":
        action_code = "verify_bid_system"
        action_label = "Confirm whether this is a single-part or two/three-part bid"
        action_reason = "Clarification is allowed only after the bid system is known."
    elif bid_system == "three_part":
        action_code = "review_clarification_authority"
        action_label = "Confirm the tender-specific clarification procedure"
        action_reason = "The governing policy's standard clarification clause is written for two-part bidding and must not be assumed for a three-part bid."
    elif bid_system == "single_part":
        action_code = "apply_tender_consequence"
        action_label = "Do not seek a post-opening document; apply the tender consequence"
        action_reason = "The governing policy does not permit post-opening clarification for a single-part bid."
        _append_policy_clause(clauses, policy_id, _clause_map(policy_id)["single_part"], "bid_procedure")
    elif substantive_failure:
        action_code = "clarify_substantive_finding_once"
        action_label = "Give one opportunity to clarify the finding without altering the bid"
        action_reason = normalize_ws(card.get("reason") or "") or "The submitted evidence does not meet the tender requirement; clarification cannot create new eligibility or alter the bid."
        _append_policy_clause(clauses, policy_id, _clause_map(policy_id)["ask_once"], "clarification")
    else:
        action_code = "request_clarification_once"
        action_label = f"Request {requested_evidence} once by a recorded cut-off date"
        action_reason = normalize_ws(card.get("reason") or "") or f"{requested_evidence} was not established from the submitted bid; any response may only prove facts that existed at the bid deadline."
        _append_policy_clause(clauses, policy_id, _clause_map(policy_id)["ask_once"], "clarification")

    next_action = {
        "code": action_code,
        "label": action_label,
        "reason": action_reason,
        "external": action_code in {"request_clarification_once", "clarify_substantive_finding_once"},
    }
    governing_clauses = [clause for clause in clauses if clause.get("role") in {"governing", "subject"}]
    clarification_clauses = [clause for clause in clauses if clause.get("role") == "clarification"]
    deviation_clauses = [clause for clause in clauses if clause.get("role") == "deviation_loading"]
    procedure_clauses = [
        clause for clause in clauses
        if clause.get("role") in {"clarification", "deviation_loading", "bid_procedure", "process"}
    ]
    primary_clause = governing_clauses[0] if governing_clauses else None
    approvals = card.get("approvals") if family == "deviation" and action_code == "apply_deviation_procedure" else []
    card.update(
        {
            "decision_effect": str(finding.get("decision_effect") or finding.get("failure_effect") or ""),
            "evidence_state": _evidence_state(finding),
            "tender_effect": effect,
            "next_action": next_action,
            "policy_clauses": clauses,
            "governing_policy_clauses": governing_clauses,
            "clarification_clauses": clarification_clauses,
            "deviation_clauses": deviation_clauses,
            "procedure_clauses": procedure_clauses,
            "approval_requirements": approvals,
            "communicate_to_all_qualified_bidders": bool(deviation_clauses),
            "policy_scope_status": "resolved" if policy_id else "unresolved",
            "bid_system": bid_system,
            "policy": {
                "policy_id": (primary_clause or {}).get("policy_id", ""),
                "clause_id": (primary_clause or {}).get("clause_id", ""),
                "title": (primary_clause or {}).get("title", ""),
                "quote": (primary_clause or {}).get("quote", ""),
            },
            "doors": [
                {
                    "id": action_code,
                    "open": action_code in {
                        "request_clarification_once",
                        "clarify_substantive_finding_once",
                        "apply_deviation_procedure",
                    },
                    "label": action_label,
                    "clause_id": (procedure_clauses[-1] if procedure_clauses else {}).get("clause_id", ""),
                    "policy_id": (procedure_clauses[-1] if procedure_clauses else {}).get("policy_id", ""),
                    "policy_name": (procedure_clauses[-1] if procedure_clauses else {}).get("policy_name", ""),
                    "clause_title": (procedure_clauses[-1] if procedure_clauses else {}).get("title", ""),
                    "clause_quote": (procedure_clauses[-1] if procedure_clauses else {}).get("quote", ""),
                    "reason": action_reason,
                    "approvals": approvals,
                    "communicate_to_all_qualified_bidders": bool(deviation_clauses),
                }
            ],
            "primary_door": action_code,
            "goi_note": "",
            "facts_for_ask": [action_label],
            "officer_problem": [],
            "officer_actions": [],
            "officer_copy_source": "pending",
        }
    )
    return card


def assemble_card(
    finding: dict[str, Any],
    *,
    vendor: dict[str, Any] | None = None,
    tender: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    status = str(finding.get("status") or "").casefold()
    if status in _PASS or _is_post_award_finding(finding):
        return None
    tender = tender or {}
    vendor = vendor or {}
    policy_id = _policy_id(tender)
    family = classify_family(finding)
    actual_missing = bool(
        status in {"missing", "missing_after_complete_search"}
        or any(str(item or "").strip() for item in finding.get("missing_documents") or [])
    )
    papers = missing_papers(finding) if actual_missing else []
    paper = (
        papers[0]
        if len(papers) == 1
        else "the following documents: " + "; ".join(papers)
        if papers
        else "the tender-required evidence"
        if actual_missing
        else ""
    )
    have = already_have(finding, vendor)
    vendor_name = str(vendor.get("vendor_name") or vendor.get("name") or "").strip()
    eligibility = False
    waivable = False
    doors: list[dict[str, Any]] = []
    goi_note = ""
    facts_for_ask: list[str] = []

    if family == "pan":
        eligibility = True
        doors = [
            _ask_once_or_single_part(tender, policy_id, paper),
            _no_substitute(
                policy_id,
                paper,
                family="pan",
                goi="A PAN number written in the offer is not a PAN card. The NIT asks for the copy for vendor-code creation.",
            ),
        ]
        facts_for_ask.append("Ask for a scan of the PAN card. The PAN number already written in the bid is not the card.")
    elif family == "gst":
        eligibility = True
        doors = [
            _ask_once_or_single_part(tender, policy_id, paper),
            _no_substitute(
                policy_id,
                paper,
                family="gst",
                goi="A GSTIN typed in the offer is not the GST registration copy required for vendor-code creation.",
            ),
        ]
        facts_for_ask.append("Ask for a scan of the GST registration. The GSTIN already written in the bid is not the certificate.")
    elif family == "udyam_mse":
        goi_note = (
            "MSE benefits need a valid Udyam certificate in this bidder's name as a manufacturer/OEM. "
            "Traders and agents cannot take MSE. BHEL cannot grant MSE status without that paper."
        )
        doors = [
            {
                **_ask_once_or_single_part(tender, policy_id, "Udyam registration in the bidder's name showing manufacturer/OEM"),
                "reason": (
                    "If this bidder wants MSE preference or an MSE-linked exemption, ask once for Udyam in their own name "
                    "as manufacturer/OEM. A trader cannot substitute someone else's Udyam or an NSIC-only paper."
                ),
            },
            _door(
                door_id="preference_closed",
                open_=False,
                label="MSE preference door closed until Udyam of a manufacturer is on file",
                policy=_subject_clause(policy_id, "udyam_mse"),
                reason=goi_note + " Missing Udyam does not, by itself, make the bid unresponsive unless the tender is MSE-reserved.",
            ),
            _door(
                door_id="bid_continues",
                open_=True,
                label="Bid can continue without MSE preference",
                policy=_subject_clause(policy_id, "udyam_mse"),
                reason="Evaluate the rest of the bid on merit. Do not treat missing Udyam as a PAN/PQR fail.",
            ),
        ]
        facts_for_ask.append("Do not reject only for missing Udyam. Close MSE preference. Ask for Udyam only if they want MSE.")
    elif family in {"startup_not_offered", "mse_relaxation_not_offered"}:
        doors = [
            _door(
                door_id="process_rule",
                open_=False,
                label="Tender does not give this relaxation",
                policy=_subject_clause(policy_id, family),
                reason=(
                    "The tender says this relaxation is not offered. There is no bidder paper to collect for it, "
                    "and no officer approval that would create the relaxation after opening."
                ),
            )
        ]
        paper = ""
        facts_for_ask.append("Do not ask the bidder for a startup/MSE-relaxation certificate. The NIT does not offer that relaxation.")
    elif family == "mii_allocation":
        doors = [
            _door(
                door_id="process_rule",
                open_=False,
                label="BHEL allocation rule, not a bidder paper",
                policy=_subject_clause(policy_id, "mii_allocation"),
                reason=(
                    "Maximum allocation quantity is how BHEL splits quantity after a Class-I local supplier is proven. "
                    "Do not ask the bidder for a 'maximum allocation' certificate."
                ),
            )
        ]
        paper = ""
        facts_for_ask.append("No paper to ask. Apply MII quantity split only after Class-I/II is proven.")
    elif family == "mii":
        eligibility = _mii_is_eligibility(finding)
        goi_note = (
            "Make in India class and local-content percentage must be shown by the prescribed OEM/local-content certificate. "
            "BHEL cannot treat a bidder as Class I/II without that paper."
        )
        doors = [
            _ask_once_or_single_part(tender, policy_id, paper),
            _no_substitute(policy_id, paper, family="mii", goi=goi_note),
        ]
        if not eligibility:
            doors.insert(
                1,
                _door(
                    door_id="preference_closed",
                    open_=False,
                    label="MII purchase preference closed until the certificate is on a verified page",
                    policy=_subject_clause(policy_id, "mii"),
                    reason=goi_note,
                ),
            )
        facts_for_ask.append(
            "Ask once for the OEM local-content certificate naming the percentage and place of value addition. "
            "Do not accept an unverified percentage as Class I/II."
        )
    elif family == "land_border":
        eligibility = True
        goi_note = (
            "Rule 144(xi) / GeM land-border condition: a bidder from a land-border country needs competent-authority "
            "(DPIIT) registration. BHEL cannot waive this."
        )
        doors = [
            _ask_once_or_single_part(tender, policy_id, paper),
            _no_substitute(policy_id, paper, family="land_border", goi=goi_note),
        ]
        facts_for_ask.append("Ask once for the signed land-border undertaking or DPIIT registration. No BHEL officer can waive it.")
    elif family == "nda":
        doors = [_ask_once_or_single_part(tender, policy_id, paper)]
        facts_for_ask.append("Ask once for the filled, signed NDA if drawings/specifications still need to be issued.")
    elif family == "oem_tc":
        doors = [
            _ask_once_or_single_part(tender, policy_id, paper),
            _deviation_door(finding, policy_id),
        ]
        facts_for_ask.append(
            "Ask once for correlated test certificates of the original manufacturer. "
            "If the committee wants to accept the bidder's own certificate instead, that is a technical deviation: "
            "Engineering/Indenting head, loading, and communicate to all qualified bidders before price opening."
        )
    elif family == "pqr":
        eligibility = True
        doors = [
            _ask_once_or_single_part(tender, policy_id, paper),
            _no_substitute(
                policy_id,
                paper,
                family="pqr",
                goi="Pre-qualification cannot be waived by treating missing work orders as a commercial deviation under the loading clause.",
            ),
        ]
        facts_for_ask.append("Ask once for the PQR papers (orders/completion certificates). If the bidder does not have the qualification, there is no ordinary acceptance path.")
    elif family == "emd":
        doors = [
            _ask_once_or_single_part(tender, policy_id, paper),
            _door(
                door_id="mse_exemption",
                open_=True,
                label="EMD exemption only with manufacturer Udyam if the tender allows MSE exemption",
                policy=_emd_mse_clause(policy_id),
                reason="A trader cannot use MSE to skip EMD. Purchase Policy 5A excepts MSEs from EMD at NIT stage; it is not a bid-stage waiver for a trader.",
            ),
        ]
        facts_for_ask.append("Ask once for the EMD instrument, or for manufacturer Udyam if the tender allows MSE EMD exemption.")
    elif family == "deviation":
        doors = [_deviation_door(finding, policy_id)]
        facts_for_ask.append("Named officer must accept the deviation and frame loading. Tell all qualified bidders before price-bid opening.")
    elif family == "deviation_statement":
        doors = [_ask_once_or_single_part(tender, policy_id, paper)]
        facts_for_ask.append("Ask for the tender-required deviation or no-deviation statement; do not treat its absence as a deviation with loading.")
    elif family == "participation":
        eligibility = _mii_is_eligibility(finding)
        doors = [
            _ask_once_or_single_part(
                tender,
                policy_id,
                "Class I/II local-supplier proof and, if MSE is claimed, Udyam as manufacturer/OEM",
            ),
            _door(
                door_id="preference_closed",
                open_=False,
                label="MSE preference stays closed without manufacturer Udyam",
                policy=_subject_clause(policy_id, "participation"),
                reason="Legal identity alone does not prove Class I/II or MSE OEM status.",
            ),
        ]
        facts_for_ask.append("Legal identity is not enough. Ask for MII class proof. MSE remains closed without manufacturer Udyam.")
    else:
        doors = [_ask_once_or_single_part(tender, policy_id, paper)]
        facts_for_ask.append(f"Ask once for {paper} by a cut-off date. Do not treat this as a loading deviation unless the bidder is declaring a different term.")

    for door in doors:
        grounded = _clause(policy_id, str(door.get("clause_id") or ""))
        door["clause_id"] = grounded["clause_id"]
        door["clause_title"] = grounded["title"]
        door["clause_quote"] = grounded["quote"]
        door["policy_id"] = grounded["policy_id"]
        door["policy_name"] = grounded["policy_name"]
        if not grounded["clause_id"]:
            door["clause_id"] = ""
            door["clause_title"] = ""
            door["clause_quote"] = ""

    primary = next((door for door in doors if door.get("open")), doors[0] if doors else None)
    subject = _subject_clause(policy_id, family)
    shown = subject if subject.get("clause_id") else {
        "policy_id": (primary or {}).get("policy_id") or policy_id,
        "policy_name": (primary or {}).get("policy_name") or _book_name(policy_id),
        "clause_id": (primary or {}).get("clause_id") or "",
        "title": (primary or {}).get("clause_title") or "",
        "quote": (primary or {}).get("clause_quote") or "",
    }
    card = {
        "criterion_id": finding.get("criterion_id"),
        "family": family,
        "status": finding.get("status"),
        "mandatory": bool(finding.get("mandatory")),
        "eligibility_gate": eligibility,
        "waivable_by_bhel": waivable,
        "vendor_name": vendor_name,
        "title": str(finding.get("criterion_text") or "")[:240],
        "reason": str(finding.get("reason") or "")[:500],
        "missing_paper": paper,
        "missing_papers": papers,
        "already_have": have,
        "doors": doors,
        "primary_door": (primary or {}).get("id"),
        "policy": {
            "policy_id": shown.get("policy_id") or policy_id,
            "clause_id": shown.get("clause_id") or "",
            "title": shown.get("title") or shown.get("clause_title") or "",
            "quote": shown.get("quote") or shown.get("clause_quote") or "",
        },
        "approvals": [
            approval
            for door in doors
            for approval in (door.get("approvals") or [])
        ],
        "goi_note": goi_note,
        "facts_for_ask": facts_for_ask,
        "officer_problem": [],
        "officer_actions": [],
        "officer_copy_source": "pending",
    }
    return _finalize_card(card, finding, tender)


def assemble_next_ask(
    findings: list[Any],
    *,
    vendor: Any = None,
    tender: Any = None,
) -> dict[str, Any]:
    vendor_d = _as_dict(vendor)
    tender_d = _as_dict(tender)
    cards: list[dict[str, Any]] = []
    for finding in findings or []:
        card = assemble_card(_as_dict(finding), vendor=vendor_d, tender=tender_d)
        if card:
            cards.append(card)
    return {
        "schema_version": SCHEMA,
        "policy_id": _policy_id(tender_d),
        "two_part": _is_two_part(tender_d),
        "bid_system": _bid_system(tender_d),
        "cards": cards,
    }


def cards_by_criterion(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for card in payload.get("cards") or []:
        if isinstance(card, dict) and card.get("criterion_id"):
            out[str(card["criterion_id"])] = card
    return out


__all__ = [
    "SCHEMA",
    "_door",
    "_ask_once_or_single_part",
    "_no_substitute",
    "_record_rejection",
    "_deviation_door",
    "_finalize_card",
    "assemble_card",
    "assemble_next_ask",
    "cards_by_criterion",
]
