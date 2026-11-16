"""Evaluation workbench router assembly and compatibility exports."""
from __future__ import annotations

from fastapi import APIRouter

from procurement.workbench.evaluation_awards import (
    _award_bidder_payload,
    collect_award_inputs,
    refresh_award_recommendation,
)
from procurement.workbench.evaluation_reports import (
    _evaluation_workbook_bytes,
    _project_bhel_reports,
    _project_reports,
    _report_completion,
    _selected_reports,
    bhel_result_router,
    generate_project_report,
    project_bhel_results,
    project_report,
    project_report_excel,
    project_report_markdown,
    router as report_router,
)
from procurement.workbench.evaluation_results import (
    _BHEL_COMPARATIVE_SCHEMA_VERSION,
    _BHEL_SCHEMA_VERSION,
    _apply_bhel_reviews_to_report,
    _attach_bhel_extension,
    _bhel_comparative_result,
    _bhel_extension,
    _clarification_payload,
    _reject_bhel_clarification,
    _report_bhel_extension,
    close_clarification,
    create_clarification,
    issue_clarification,
    list_clarifications,
    project_results,
    record_clarification_response,
    review_criterion,
    review_log,
    review_router,
    router as result_router,
)
from procurement.workbench.evaluation_runs import (
    EvaluationSelection,
    evaluate_project,
    router as run_router,
)

router = APIRouter(
    prefix="/v1/workbench",
    routes=[
        *run_router.routes,
        *result_router.routes,
        *bhel_result_router.routes,
        *review_router.routes,
        *report_router.routes,
    ],
)

__all__ = [
    "EvaluationSelection",
    "_BHEL_COMPARATIVE_SCHEMA_VERSION",
    "_BHEL_SCHEMA_VERSION",
    "_apply_bhel_reviews_to_report",
    "_attach_bhel_extension",
    "_award_bidder_payload",
    "_bhel_comparative_result",
    "_bhel_extension",
    "_clarification_payload",
    "_evaluation_workbook_bytes",
    "_project_bhel_reports",
    "_project_reports",
    "_reject_bhel_clarification",
    "_report_bhel_extension",
    "_report_completion",
    "_selected_reports",
    "close_clarification",
    "collect_award_inputs",
    "create_clarification",
    "evaluate_project",
    "generate_project_report",
    "issue_clarification",
    "list_clarifications",
    "project_bhel_results",
    "project_report",
    "project_report_excel",
    "project_report_markdown",
    "project_results",
    "record_clarification_response",
    "refresh_award_recommendation",
    "review_criterion",
    "review_log",
    "router",
]
