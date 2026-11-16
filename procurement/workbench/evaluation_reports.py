"""Evaluation report collection and Markdown/Excel export routes."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from procurement.api import _actor_user_id, _sessions
from procurement.domain.models import (
    Evaluation,
    TenderVersion,
    WorkbenchBid,
    WorkbenchProject,
)
from procurement.evaluation.award import public_award_view
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.operations import audit
from procurement.workbench.common import (
    _apply_human_reviews,
    _latest_reviews_by_criterion,
    _normalize_public_report,
    _overlay_human_reviews_on_criteria_results,
    _project,
    _state,
    _ui_evaluation,
    decision_summary,
)
from procurement.workbench.evaluation_awards import refresh_award_recommendation
from procurement.workbench.evaluation_results import (
    _BHEL_SCHEMA_VERSION,
    _apply_bhel_reviews_to_report,
    _attach_bhel_extension,
    _bhel_comparative_result,
    _report_bhel_extension,
)

bhel_result_router = APIRouter(prefix="/v1/workbench", tags=["workbench"])
router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


@bhel_result_router.get("/projects/{project_id}/results/bhel")
def project_bhel_results(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        reports = _project_bhel_reports(session, project)
        if not any(_report_bhel_extension(report) for report in reports.values()):
            raise HTTPException(
                status_code=409,
                detail={"message": "BHEL evaluation results are not ready.", "code": "BHEL_RESULTS_NOT_READY"},
            )
        return _bhel_comparative_result(reports)


def _project_reports(session: Any, project: WorkbenchProject) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    version = session.get(TenderVersion, project.tender_version_id)
    understanding = dict(version.understanding_json or {}) if version else {}
    for row in session.scalars(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id)):
        evaluation = session.get(Evaluation, row.evaluation_id) if row.evaluation_id else None
        if evaluation and evaluation.status == "succeeded" and evaluation.result_json:
            report = _attach_bhel_extension(
                dict(evaluation.result_json),
                evaluation.internal_result_json,
            )
            report["vendor"] = {
                **(report.get("vendor") if isinstance(report.get("vendor"), dict) else {}),
                "vendor_id": row.vendor_key,
                "name": row.vendor_name,
                "vendor_name": row.vendor_name,
            }
            ui_result = _ui_evaluation(
                evaluation,
                project_id=project.id,
                vendor_key=row.vendor_key,
                intake=dict(row.intake_json or {}),
                understanding=understanding,
            )
            _apply_human_reviews(session, evaluation, ui_result)
            internal = (evaluation.internal_result_json or {}).get("report") if isinstance(evaluation.internal_result_json, dict) else {}
            if not isinstance(internal, dict):
                internal = {}
            reviews = _latest_reviews_by_criterion(session, evaluation)
            report["criteria_results"] = _overlay_human_reviews_on_criteria_results(
                report.get("criteria_results") or [],
                internal_criteria=list((internal.get("tender") or {}).get("criteria") or []),
                internal_findings=list(internal.get("findings") or []),
                reviews_by_id=reviews,
            )
            report = _normalize_public_report(report)
            report["decision_summary"] = decision_summary(
                ui_result.get("criteria") or report.get("criteria_results") or [],
                ui_result.get("final_decision") or report.get("final_decision"),
                report.get("qualifies"),
            )
            report = _apply_bhel_reviews_to_report(session, evaluation, report)
            reports[row.vendor_key] = report
    if not reports:
        raise HTTPException(status_code=409, detail="evaluation reports are not ready")
    return reports


def _project_bhel_reports(session: Any, project: WorkbenchProject) -> dict[str, dict[str, Any]]:
    """Return all bidders, including pending/failed ones, for comparison."""
    from procurement.bhel_evaluation import load_bhel_profile
    from procurement.compliance.profile import PURCHASE_POLICY, policy_id_for_profile
    from procurement.pipelines.support import _profile_data_from_understanding

    if load_bhel_profile() is None:
        return {}
    version = session.get(TenderVersion, project.tender_version_id)
    understanding = dict(version.understanding_json or {}) if version else {}
    if policy_id_for_profile(_profile_data_from_understanding(understanding)) != PURCHASE_POLICY:
        return {}
    try:
        reports = _project_reports(session, project)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        reports = {}
    bidders = list(
        session.scalars(
            select(WorkbenchBid)
            .where(WorkbenchBid.project_id == project.id)
            .order_by(WorkbenchBid.created_at)
        )
    )
    for bidder in bidders:
        if bidder.vendor_key in reports:
            continue
        evaluation = session.get(Evaluation, bidder.evaluation_id) if bidder.evaluation_id else None
        evaluation_status = evaluation.status if evaluation else (
            "classification_pending" if bidder.classification_status != "approved" else "not_started"
        )
        reports[bidder.vendor_key] = {
            "extensions": {
                "bhel": {
                    "schema_version": _BHEL_SCHEMA_VERSION,
                    "profile_id": "bhel_purchase_materials",
                    "bidder": {"bidder_id": bidder.vendor_key, "bidder_name": bidder.vendor_name},
                    "evaluation_status": evaluation_status,
                    "checks": [],
                    "internal_controls": [],
                    "bidder_summary": {
                        "qualification": "not_evaluated",
                        "counts": {},
                        "blocking_check_ids": [],
                        "evaluated_check_count": 0,
                        "active_check_count": 0,
                    },
                }
            }
        }
    return reports


def _report_completion(session: Any, project: WorkbenchProject, reports: dict[str, Any]) -> tuple[bool, list[str]]:
    bidders = list(session.scalars(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id)))
    missing = sorted(row.vendor_key for row in bidders if row.vendor_key not in reports)
    return not missing, missing


@router.post("/projects/{project_id}/report/generate")
def generate_project_report(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        reports = _project_reports(session, project)
        complete, missing_vendors = _report_completion(session, project, reports)
        if complete and project.stage != "report_generated":
            previous_stage = project.stage
            project.stage = "report_generated"
            project.updated_at = datetime.now(timezone.utc)
            audit(
                session,
                tenant_id=principal.tenant_id,
                actor_user_id=actor_id,
                event_type="workbench.report.generated",
                entity_type="workbench_project",
                entity_id=project.id,
                reason=None,
                before={"stage": previous_stage},
                after={"stage": project.stage, "vendors": sorted(reports)},
            )
        award = refresh_award_recommendation(session, project) if complete else public_award_view(project.award_json)
        return {
            "state": _state(session, project),
            "complete": complete,
            "missing_vendors": missing_vendors,
            "vendors": sorted(reports),
            "reports": reports,
            "award_recommendation": award,
        }


@router.get("/projects/{project_id}/report")
def project_report(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        reports = _project_reports(session, project)
        complete, missing_vendors = _report_completion(session, project, reports)
        award = refresh_award_recommendation(session, project) if complete else public_award_view(project.award_json)
        return {
            "state": _state(session, project),
            "complete": complete,
            "missing_vendors": missing_vendors,
            "reports": reports,
            "award_recommendation": award,
        }


def _selected_reports(request: Request, reports: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected = request.query_params.getlist("vendor")
    if not selected:
        return reports
    unknown = [vendor for vendor in selected if vendor not in reports]
    if unknown:
        raise HTTPException(status_code=404, detail=f"unknown report bidder: {', '.join(unknown)}")
    return {vendor: reports[vendor] for vendor in selected}


def _evaluation_workbook_bytes(project: WorkbenchProject, reports: dict[str, dict[str, Any]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    navy = "1F4E78"
    pale_blue = "D9EAF7"
    pale_green = "E2F0D9"
    pale_red = "FCE4D6"
    pale_amber = "FFF2CC"
    pale_grey = "E7E6E6"
    white = "FFFFFF"
    line = Side(style="thin", color="D9E2F3")

    def vendor_name(vendor: str, report: dict[str, Any]) -> str:
        bidder = report.get("vendor") or {}
        return str(bidder.get("vendor_name") or bidder.get("name") or vendor)

    def status_kind(item: dict[str, Any]) -> tuple[str, str]:
        human = item.get("human_review") or {}
        decision = str(human.get("decision") or "")
        status_value = str(item.get("status") or "").lower()
        if decision == "meets_requirement" or status_value in {"pass", "passed", "qualified", "compliant"}:
            return "PASS", pale_green
        if decision == "does_not_meet" or status_value in {
            "fail", "failed", "fail / missing", "fail_fact", "present_but_invalid", "missing", "missing_after_complete_search",
        }:
            return "FAIL / MISSING", pale_red
        if item.get("evaluation_state") == "not_evaluated_after_blocker" or status_value in {"not_evaluated", "not evaluated"}:
            return "NOT EVALUATED", pale_grey
        return "NEEDS REVIEW", pale_amber

    def joined(value: Any) -> str:
        if not value:
            return ""
        return "\n".join(str(item) for item in value) if isinstance(value, list) else str(value)

    def short_requirement(value: Any) -> str:
        requirement = str(value or "Requirement").strip()
        colon = requirement.find(":")
        if 0 < colon < 80:
            requirement = requirement[:colon]
        return requirement if len(requirement) <= 100 else f"{requirement[:97].rstrip()}..."

    def setup_sheet(sheet: Any, title: str, subtitle: str, columns: int) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=columns)
        sheet["A1"] = title
        sheet["A1"].fill = PatternFill("solid", fgColor=navy)
        sheet["A1"].font = Font(name="Aptos Display", size=16, bold=True, color=white)
        sheet["A1"].alignment = Alignment(vertical="center")
        sheet.row_dimensions[1].height = 28
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=columns)
        sheet["A2"] = subtitle
        sheet["A2"].font = Font(name="Aptos", size=10, color="44546A")
        sheet["A2"].alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[2].height = 30
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.oddFooter.center.text = "Page &P of &N"
        sheet.oddFooter.right.text = project.project_key

    def style_header(sheet: Any, row: int, columns: int) -> None:
        for cell in sheet[row][:columns]:
            cell.fill = PatternFill("solid", fgColor=navy)
            cell.font = Font(name="Aptos", size=10, bold=True, color=white)
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[row].height = 28

    def add_table(sheet: Any, header_row: int, last_row: int, columns: int, name: str) -> None:
        if last_row <= header_row:
            return
        ref = f"A{header_row}:{get_column_letter(columns)}{last_row}"
        table = Table(displayName=name, ref=ref)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)

    generated = datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %H:%M %Z")
    subtitle = f"{project.title or 'Untitled tender'} | {project.project_key} | Generated {generated}"
    workbook = Workbook()
    workbook.properties.creator = "Procurement Intelligence"
    workbook.properties.title = f"Bid Evaluation Report - {project.project_key}"

    summary_sheet = workbook.active
    summary_sheet.title = "Executive Summary"
    setup_sheet(summary_sheet, "Bid Evaluation Report", subtitle, 8)
    summary_sheet.merge_cells("A4:H4")
    summary_sheet["A4"] = "Bidder qualification summary"
    summary_sheet["A4"].fill = PatternFill("solid", fgColor=pale_blue)
    summary_sheet["A4"].font = Font(name="Aptos", size=11, bold=True, color=navy)
    summary_sheet["A4"].alignment = Alignment(vertical="center")
    summary_sheet.append(["Bidder", "Outcome", "Passed", "Failed / missing", "Needs review", "Not evaluated", "Total", "Next action"])
    style_header(summary_sheet, 5, 8)
    for vendor, report in reports.items():
        decision = report.get("decision_summary") or {}
        summary_sheet.append(
            [
                vendor_name(vendor, report),
                decision.get("label") or report.get("final_decision") or "Review required",
                int(decision.get("passed") or 0),
                int(decision.get("failed") or 0),
                int(decision.get("review") or 0),
                int(decision.get("not_evaluated") or 0),
                int(decision.get("total") or 0),
                report.get("next_action") or "Committee review and approval required.",
            ]
        )
        outcome_cell = summary_sheet.cell(summary_sheet.max_row, 2)
        outcome = str(decision.get("outcome") or "review")
        outcome_cell.fill = PatternFill("solid", fgColor=pale_green if outcome == "pass" else pale_red if outcome == "fail" else pale_amber)
        outcome_cell.font = Font(name="Aptos", bold=True, color="006100" if outcome == "pass" else "9C0006" if outcome == "fail" else "9C6500")
    add_table(summary_sheet, 5, summary_sheet.max_row, 8, "BidderSummary")
    summary_sheet.freeze_panes = "A6"
    summary_sheet.column_dimensions["A"].width = 42
    summary_sheet.column_dimensions["B"].width = 22
    for column in "CDEFG":
        summary_sheet.column_dimensions[column].width = 16
    summary_sheet.column_dimensions["H"].width = 55
    summary_sheet.auto_filter.ref = f"A5:H{summary_sheet.max_row}"
    summary_sheet.merge_cells(start_row=summary_sheet.max_row + 2, start_column=1, end_row=summary_sheet.max_row + 2, end_column=8)
    note = summary_sheet.cell(summary_sheet.max_row, 1)
    note.value = "Qualification, rejection, shortlisting, and commercial opening remain authorized committee decisions."
    note.fill = PatternFill("solid", fgColor=pale_amber)
    note.font = Font(name="Aptos", bold=True, color="7F6000")
    note.alignment = Alignment(wrap_text=True, vertical="center")

    requirements: list[tuple[str, str, str]] = []
    requirement_keys: set[str] = set()
    bidder_statuses: dict[str, dict[str, tuple[str, str]]] = {}
    for vendor, report in reports.items():
        bidder_statuses[vendor] = {}
        for item in report.get("criteria_results") or []:
            requirement = str(item.get("requirement") or item.get("category") or "Requirement")
            category = str(item.get("category") or "")
            key = requirement
            if key not in requirement_keys:
                requirements.append((key, short_requirement(requirement), category))
                requirement_keys.add(key)
            bidder_statuses[vendor][key] = status_kind(item)

    matrix_sheet = workbook.create_sheet("Bidder Comparison")
    setup_sheet(matrix_sheet, "Bidder Comparison", "One row per tender requirement; compare bidder outcomes across columns.", 2 + len(reports))
    matrix_sheet.append(["Requirement", "Category", *[vendor_name(vendor, report) for vendor, report in reports.items()]])
    style_header(matrix_sheet, 3, 2 + len(reports))
    for requirement_key, requirement_label, category in requirements:
        row = [requirement_label, category]
        for vendor in reports:
            row.append(bidder_statuses[vendor].get(requirement_key, ("NOT EVALUATED", pale_grey))[0])
        matrix_sheet.append(row)
        for column_index, vendor in enumerate(reports, start=3):
            _label, fill = bidder_statuses[vendor].get(requirement_key, ("NOT EVALUATED", pale_grey))
            matrix_sheet.cell(matrix_sheet.max_row, column_index).fill = PatternFill("solid", fgColor=fill)
            matrix_sheet.cell(matrix_sheet.max_row, column_index).font = Font(name="Aptos", bold=True)
            matrix_sheet.cell(matrix_sheet.max_row, column_index).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    add_table(matrix_sheet, 3, matrix_sheet.max_row, 2 + len(reports), "BidderComparison")
    matrix_sheet.freeze_panes = "C4"
    matrix_sheet.column_dimensions["A"].width = 65
    matrix_sheet.column_dimensions["B"].width = 24
    for index in range(3, 3 + len(reports)):
        matrix_sheet.column_dimensions[get_column_letter(index)].width = 24

    detail_headers = [
        "Bidder", "Result", "Requirement", "Category", "Finding / reason",
        "Required proof", "Missing evidence", "Evidence reviewed",
    ]
    detail_rows: list[list[Any]] = []
    exception_rows: list[list[Any]] = []
    for vendor, report in reports.items():
        name = vendor_name(vendor, report)
        for item in report.get("criteria_results") or []:
            status_label, _fill = status_kind(item)
            row = [
                name,
                status_label,
                item.get("requirement") or item.get("category") or "Requirement",
                item.get("category") or "",
                item.get("why") or item.get("result") or "",
                joined(item.get("required_documents")),
                joined(item.get("missing_documents")),
                joined(item.get("evidence_documents")),
            ]
            detail_rows.append(row)
            if status_label != "PASS":
                exception_row = list(row)
                exception_row[2] = short_requirement(exception_row[2])
                exception_rows.append(exception_row)

    for sheet_name, title, rows, table_name in [
        ("Exceptions", "Exceptions Requiring Attention", exception_rows, "EvaluationExceptions"),
        ("Evaluation Detail", "Complete Evaluation Detail", detail_rows, "EvaluationDetail"),
    ]:
        sheet = workbook.create_sheet(sheet_name)
        setup_sheet(sheet, title, "Full reasoning is retained here for audit and reviewer verification.", 8)
        sheet.append(detail_headers)
        style_header(sheet, 3, 8)
        for row in rows:
            sheet.append(row)
            _label, fill = status_kind({"status": row[1]})
            sheet.cell(sheet.max_row, 2).fill = PatternFill("solid", fgColor=fill)
            sheet.cell(sheet.max_row, 2).font = Font(name="Aptos", bold=True)
        if not rows:
            sheet.append(["No exceptions were identified." if sheet_name == "Exceptions" else "No evaluation detail is available.", "", "", "", "", "", "", ""])
        add_table(sheet, 3, sheet.max_row, 8, table_name)
        sheet.freeze_panes = "C4"
        widths = [38, 18, 58, 24, 72, 38, 38, 45]
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width

    bhel_reports = [
        (vendor, report, extension)
        for vendor, report in reports.items()
        if (extension := _report_bhel_extension(report))
    ]
    if bhel_reports:
        bhel_sheet = workbook.create_sheet("BHEL Evaluation")
        setup_sheet(
            bhel_sheet,
            "BHEL Evaluation",
            "BHEL baseline and tender-derived checks. Internal controls are shown separately and never qualify a bidder.",
            13,
        )
        bhel_sheet.append([
            "Bidder",
            "Qualification / decision",
            "Schema version",
            "Check type",
            "Check ID",
            "Requirement",
            "Result",
            "Applicability",
            "Blocks qualification",
            "Reason",
            "Tender source",
            "Bidder evidence",
            "Origin",
        ])
        style_header(bhel_sheet, 3, 13)
        for vendor, report, extension in bhel_reports:
            decision = report.get("decision_summary") or {}
            bhel_summary = extension.get("bidder_summary") or {}
            qualification = (
                bhel_summary.get("qualification")
                or extension.get("qualification")
                or extension.get("final_status")
                or report.get("final_decision")
                or decision.get("label")
                or "Review required"
            )
            rows = [
                ("Bidder check", item)
                for item in extension.get("checks") or []
                if isinstance(item, dict)
            ] + [
                ("Internal control (non-qualifying)", item)
                for item in extension.get("internal_controls") or []
                if isinstance(item, dict)
            ]
            if not rows:
                rows = [("Summary", {"reason": extension.get("notes") or "No BHEL check detail is available."})]
            for check_type, item in rows:
                bhel_sheet.append([
                    vendor_name(vendor, report),
                    qualification,
                    extension.get("schema_version") or _BHEL_SCHEMA_VERSION,
                    check_type,
                    item.get("check_id") or item.get("criterion_id") or "",
                    item.get("requirement") or item.get("label") or "",
                    item.get("result") or "not_evaluated",
                    item.get("applicability") or "",
                    "Yes" if check_type == "Bidder check" and item.get("blocks_qualification") else "No",
                    item.get("reason") or "",
                    json.dumps(item.get("source_references") or [], ensure_ascii=False, default=str),
                    json.dumps(item.get("bidder_evidence") or [], ensure_ascii=False, default=str),
                    item.get("origin") or "",
                ])
        add_table(bhel_sheet, 3, bhel_sheet.max_row, 13, "BhelEvaluation")
        bhel_sheet.freeze_panes = "A4"
        for column, width in {
            "A": 42,
            "B": 26,
            "C": 28,
            "D": 30,
            "E": 32,
            "F": 58,
            "G": 20,
            "H": 18,
            "I": 20,
            "J": 64,
            "K": 58,
            "L": 58,
            "M": 22,
        }.items():
            bhel_sheet.column_dimensions[column].width = width

    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.lstrip().startswith(("=", "+", "-", "@")):
                    cell.value = "'" + cell.value
        used = sheet.iter_rows(min_row=3, max_row=sheet.max_row, min_col=1, max_col=sheet.max_column)
        for row in used:
            for cell in row:
                cell.font = cell.font.copy(name="Aptos", size=10)
                cell.alignment = cell.alignment.copy(wrap_text=True, vertical="top")
                cell.border = Border(bottom=line)
        sheet.auto_filter.ref = None
        sheet.print_title_rows = "1:3"

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


@router.get("/projects/{project_id}/report/markdown")
def project_report_markdown(project_id: str, request: Request) -> Response:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        reports = _selected_reports(request, _project_reports(session, project))
        award = public_award_view(project.award_json)
    lines = [f"# Bid evaluation report — {project_id}", ""]
    if award:
        lines.extend(
            [
                "## AI suggestion",
                "",
                award.get("officer_headline") or "",
                "",
            ]
        )
        if award.get("preferred_vendor_name"):
            lines.append(f"- Preferred bid: {award.get('preferred_vendor_name')}")
            lines.append("")
        for title, key in (
            ("Why", "officer_why"),
            ("The other bids", "officer_others"),
            ("What to do now", "officer_actions"),
        ):
            items = award.get(key) or []
            if not items:
                continue
            lines.extend([f"### {title}", ""])
            for item in items:
                lines.append(f"- {item}")
            lines.append("")
    for vendor, report in reports.items():
        lines.extend(
            [
                f"## {vendor}",
                "",
                f"- Final decision: {report.get('final_decision') or 'Not recorded'}",
                f"- Recommendation: {report.get('recommendation') or 'Not recorded'}",
                f"- Summary: {report.get('summary') or ''}",
                f"- Rules: {report['decision_summary']['passed']} passed; {report['decision_summary']['failed']} failed/missing; {report['decision_summary']['review']} need review; {report['decision_summary']['not_evaluated']} not evaluated",
                "",
                "### Criteria",
                "",
            ]
        )
        for item in report.get("criteria_results") or []:
            lines.append(
                f"- **{item.get('requirement') or item.get('category') or 'Requirement'}** — {item.get('status') or 'unknown'}: {item.get('why') or ''}"
            )
        bhel = _report_bhel_extension(report)
        if bhel is not None:
            summary = bhel.get("bidder_summary") or {}
            counts = summary.get("counts") or {}
            lines.extend(
                [
                    "",
                    "### BHEL Evaluation",
                    "",
                    f"- Qualification: {summary.get('qualification') or 'Not evaluated'}",
                    f"- Checks: {counts.get('met', 0)} met; {counts.get('not_met', 0)} not met; {counts.get('insufficient_evidence', 0)} insufficient evidence; {counts.get('needs_review', 0)} need review; {counts.get('not_applicable', 0)} not applicable",
                    "",
                    "#### Bidder checks",
                    "",
                ]
            )
            for item in bhel.get("checks") or []:
                if isinstance(item, dict):
                    lines.append(
                        f"- **{item.get('label') or item.get('check_id') or 'Check'}** — {item.get('result') or 'not_evaluated'}: {item.get('reason') or ''}"
                    )
                    lines.append(
                        f"  - Tender source: {json.dumps(item.get('source_references') or [], ensure_ascii=False, default=str)}"
                    )
                    lines.append(
                        f"  - Bidder evidence: {json.dumps(item.get('bidder_evidence') or [], ensure_ascii=False, default=str)}"
                    )
            controls = [item for item in bhel.get("internal_controls") or [] if isinstance(item, dict)]
            if controls:
                lines.extend(["", "#### Internal controls (non-qualifying)", ""])
                for item in controls:
                    lines.append(
                        f"- **{item.get('label') or item.get('check_id') or 'Control'}** — {item.get('result') or 'not_evaluated'}: {item.get('reason') or ''}"
                    )
                    lines.append(
                        f"  - Tender source: {json.dumps(item.get('source_references') or [], ensure_ascii=False, default=str)}"
                    )
        lines.append("")
    return Response(
        "\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="bid-evaluation-{project_id}.md"'},
    )


@router.get("/projects/{project_id}/report/excel")
def project_report_excel(project_id: str, request: Request) -> Response:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        reports = _selected_reports(request, _project_reports(session, project))
    return Response(
        _evaluation_workbook_bytes(project, reports),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="bid-evaluation-{project_id}.xlsx"'},
    )
