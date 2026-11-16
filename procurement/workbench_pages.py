"""Serve the packaged bid workbench frontend pages and static assets."""

import os
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from procurement.platform.auth import (
    Principal,
    activate_local_session,
    auth_mode,
    mint_local_session,
    require_role,
    revoke_local_session,
    verify_local_password,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND_PRO_DIST = Path(__file__).resolve().parent.parent / "frontend_pro" / "dist"

ALLOWED_STEPS = frozenset({"tender", "ledger", "compliance", "contract", "bids", "evaluate", "results", "report"})

router = APIRouter()
_LOCAL_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,35}$")
_FRONTEND_MODULE_NAME = re.compile(r"^[a-z][a-z0-9-]*\.js$")


class LocalLogin(BaseModel):
    username: str = ""
    email: str = ""
    password: str


class LocalProfile(BaseModel):
    name: str = "Local procurement operator"
    phone: str = ""


class WorkbenchUsage(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    step: str = Field(pattern="^(tender|ledger|compliance|contract|bids|evaluate|results|report)$")
    previous_step: str | None = Field(
        default=None,
        pattern="^(tender|ledger|compliance|contract|bids|evaluate|results|report)$",
    )


def _portfolio_decision_summary(evaluation: dict, reviews: dict[str, dict]) -> dict:
    from procurement.workbench.common import decision_summary

    findings = evaluation.get("internal_findings")
    if isinstance(findings, list) and findings:
        criteria = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            criterion_id = str(finding.get("criterion_id") or "")
            check = {
                "criterion_id": criterion_id,
                "status": finding.get("status") or "unclear",
                "requirement": finding.get("criterion_text") or criterion_id or "Requirement",
            }
            if criterion_id in reviews:
                check["human_review"] = reviews[criterion_id]
            criteria.append({"checks": [check]})
        final_decision = evaluation.get("internal_final_decision") or evaluation.get("final_decision")
        qualifies = evaluation.get("internal_qualifies")
    else:
        criteria = evaluation.get("public_criteria") or []
        final_decision = evaluation.get("public_final_decision") or evaluation.get("final_decision")
        qualifies = evaluation.get("public_qualifies")
    return decision_summary(criteria, final_decision, qualifies if isinstance(qualifies, bool) else None)


def _account_payload(
    tenant_id: str,
    *,
    email: str | None = None,
    include_projects: bool = True,
) -> dict:
    from sqlalchemy import select

    from procurement.domain.models import Evaluation, Review, Tender, TenderVersion, User, WorkbenchBid, WorkbenchProject
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        account = session.scalar(select(User).where(User.tenant_id == tenant_id).order_by(User.created_at).limit(1))
        user = {
            "name": (account.display_name if account else None) or "Local procurement operator",
            "email": email or (account.email if account else None) or "local@installation",
            "phone": (account.phone if account else None) or "",
            "role": (account.role if account else None) or "admin",
        }
        if not include_projects:
            return {"user": user, "projects": []}
        project_rows = session.execute(
            select(
                WorkbenchProject.id.label("project_id"),
                WorkbenchProject.project_key,
                WorkbenchProject.title,
                WorkbenchProject.stage,
                WorkbenchProject.tender_id,
                WorkbenchProject.tender_version_id,
                WorkbenchProject.updated_at,
            )
            .where(WorkbenchProject.tenant_id == tenant_id)
            .order_by(WorkbenchProject.created_at.desc())
        ).all()
        if not project_rows:
            return {"user": user, "projects": []}

        project_ids = [row.project_id for row in project_rows]
        tender_numbers = dict(
            session.execute(
                select(Tender.id, Tender.external_reference).where(
                    Tender.id.in_([row.tender_id for row in project_rows])
                )
            ).all()
        )
        version_settings = {
            row.id: {
                "policy_enabled": row.policy_enabled is True or str(row.policy_enabled).lower() == "true",
                "compliance_status": str(row.compliance_status or "not_started"),
            }
            for row in session.execute(
                select(
                    TenderVersion.id,
                    TenderVersion.understanding_json["policy_audit"]["enabled"].label("policy_enabled"),
                    TenderVersion.understanding_json["compliance"]["status"].label("compliance_status"),
                ).where(TenderVersion.id.in_([row.tender_version_id for row in project_rows]))
            )
        }
        bidder_rows = session.execute(
            select(
                WorkbenchBid.project_id,
                WorkbenchBid.bid_id,
                WorkbenchBid.vendor_key,
                WorkbenchBid.vendor_name,
                WorkbenchBid.evaluation_id,
                WorkbenchBid.classification_status,
            )
            .where(WorkbenchBid.project_id.in_(project_ids))
            .order_by(WorkbenchBid.created_at)
        ).all()
        bidders_by_project: dict[str, list] = {project_id: [] for project_id in project_ids}
        for bidder in bidder_rows:
            bidders_by_project[bidder.project_id].append(bidder)

        evaluation_ids = [row.evaluation_id for row in bidder_rows if row.evaluation_id]
        evaluations = {
            row.id: dict(row._mapping)
            for row in session.execute(
                select(
                    Evaluation.id,
                    Evaluation.status,
                    Evaluation.final_decision,
                    Evaluation.internal_result_json["report"]["findings"].label("internal_findings"),
                    Evaluation.internal_result_json["report"]["final_decision"].label("internal_final_decision"),
                    Evaluation.internal_result_json["report"]["qualifies"].label("internal_qualifies"),
                    Evaluation.result_json["criteria_results"].label("public_criteria"),
                    Evaluation.result_json["final_decision"].label("public_final_decision"),
                    Evaluation.result_json["qualifies"].label("public_qualifies"),
                ).where(Evaluation.id.in_(evaluation_ids or [""]))
            )
        }
        reviews_by_evaluation: dict[str, dict[str, dict]] = {}
        for review in session.execute(
            select(Review.evaluation_id, Review.payload_json)
            .where(Review.evaluation_id.in_(evaluation_ids or [""]))
            .order_by(Review.created_at)
        ):
            payload = review.payload_json or {}
            criterion_id = str(payload.get("criterion_id") or "")
            if criterion_id:
                reviews_by_evaluation.setdefault(review.evaluation_id, {})[criterion_id] = payload

        projects = []
        for project in project_rows:
            bidders = bidders_by_project[project.project_id]
            outcomes = {"pass": 0, "fail": 0, "review": 0}
            evaluated = 0
            for bidder in bidders:
                evaluation = evaluations.get(bidder.evaluation_id)
                if not evaluation or evaluation.get("status") != "succeeded":
                    continue
                summary = _portfolio_decision_summary(
                    evaluation,
                    reviews_by_evaluation.get(bidder.evaluation_id, {}),
                )
                outcome = str(summary.get("outcome") or "review")
                outcomes[outcome if outcome in outcomes else "review"] += 1
                evaluated += 1
            complete = bool(bidders) and evaluated == len(bidders)
            portfolio_status = (
                "Needs attention" if outcomes["review"]
                else "Complete" if complete
                else "In progress"
            )
            version = version_settings.get(project.tender_version_id, {})
            item = {
                "project_id": project.project_id,
                "project_key": project.project_key,
                "title": project.title,
                "stage": project.stage,
                "tender_id": project.tender_id,
                "tender_version_id": project.tender_version_id,
                "tender_number": tender_numbers.get(project.tender_id),
                "compliance": {
                    "enabled": bool(version.get("policy_enabled")),
                    "status": version.get("compliance_status") or "not_started",
                },
                "vendors": [bidder.vendor_key for bidder in bidders],
                "bidders": [
                    {
                        "vendor_id": bidder.vendor_key,
                        "bidder_name": bidder.vendor_name,
                        "bid_id": bidder.bid_id,
                        "classification_status": bidder.classification_status,
                        "evaluation_id": bidder.evaluation_id,
                        "evaluation_status": (
                            evaluations.get(bidder.evaluation_id, {}).get("status")
                            if bidder.evaluation_id
                            else None
                        ),
                    }
                    for bidder in bidders
                ],
                "portfolio_summary": {
                    "bidder_count": len(bidders),
                    "evaluated_count": evaluated,
                    "pending_count": len(bidders) - evaluated,
                    "qualified_count": outcomes["pass"],
                    "failed_count": outcomes["fail"],
                    "review_count": outcomes["review"],
                    "status": portfolio_status,
                },
                "updated_at": project.updated_at.isoformat() if project.updated_at else None,
            }
            projects.append(item)
    return {"user": user, "projects": projects}


def _ensure_local_tenant(tenant_id: str, username: str) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from procurement.domain.models import Tenant, User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        session.execute(
            insert(Tenant)
            .values(id=tenant_id, name=f"Local workspace: {username}")
            .on_conflict_do_nothing(index_elements=[Tenant.id])
        )
        session.execute(
            insert(User)
            .values(
                tenant_id=tenant_id,
                subject=f"local:{username}",
                email=username,
                display_name="Local procurement operator",
                role="admin",
            )
            .on_conflict_do_nothing(index_elements=[User.tenant_id, User.subject])
        )


def workbench_page() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "bid_workbench.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def react_frontend_page() -> FileResponse:
    index = FRONTEND_PRO_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="React frontend build is not installed")
    return FileResponse(
        index,
        media_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/app/assets/{asset_path:path}", include_in_schema=False)
def react_frontend_asset(asset_path: str) -> FileResponse:
    assets = (FRONTEND_PRO_DIST / "assets").resolve()
    path = (assets / asset_path).resolve()
    if assets not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="frontend asset not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/app", include_in_schema=False)
@router.get("/app/{route:path}", include_in_schema=False)
def react_frontend(route: str = "") -> FileResponse:
    return react_frontend_page()


@router.get("/workbench/admin", include_in_schema=False)
def admin_dashboard(request: Request) -> FileResponse:
    require_role(request, "admin")
    return FileResponse(
        FRONTEND_DIR / "admin_dashboard.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/assets/admin-dashboard.css", include_in_schema=False)
def admin_dashboard_css(request: Request) -> FileResponse:
    require_role(request, "admin")
    return FileResponse(
        FRONTEND_DIR / "admin_dashboard.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/assets/admin-dashboard.js", include_in_schema=False)
def admin_dashboard_js(request: Request) -> FileResponse:
    require_role(request, "admin")
    return FileResponse(
        FRONTEND_DIR / "admin_dashboard.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/workbench", include_in_schema=False)
def workbench_home() -> FileResponse:
    return workbench_page()


@router.get("/workbench/login", include_in_schema=False)
def workbench_auth() -> FileResponse:
    return workbench_page()


@router.get("/workbench/register", include_in_schema=False)
def workbench_register_page() -> RedirectResponse:
    return RedirectResponse("/workbench/login", status_code=307)


@router.get("/workbench/ui/{project_id}/{step}", include_in_schema=False)
def workbench_step(project_id: str, step: str) -> FileResponse:
    if step not in ALLOWED_STEPS:
        raise HTTPException(status_code=404, detail="unknown workbench step")
    return workbench_page()


@router.get("/assets/bid-workbench.js", include_in_schema=False)
def workbench_js() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "bid_workbench.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=0, must-revalidate"},
    )


@router.get("/assets/current/modules/{module_name}", include_in_schema=False)
@router.get("/assets/modules/{module_name}", include_in_schema=False)
def workbench_module(module_name: str) -> FileResponse:
    if not _FRONTEND_MODULE_NAME.fullmatch(module_name):
        raise HTTPException(status_code=404, detail="unknown frontend module")
    path = FRONTEND_DIR / "modules" / module_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="unknown frontend module")
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=0, must-revalidate"},
    )


@router.get("/assets/workbench-theme.css", include_in_schema=False)
def workbench_css() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "workbench_theme.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=0, must-revalidate"},
    )


@router.post("/workbench/account/login", include_in_schema=False)
def workbench_login(body: LocalLogin, request: Request) -> Response:
    if auth_mode() != "api_token":
        raise HTTPException(status_code=409, detail="use the configured identity provider")
    api_token = os.getenv("PROCUREMENT_API_TOKEN") or os.getenv("API_AUTH_TOKEN") or ""
    configured_username = os.getenv("PROCUREMENT_LOCAL_USERNAME", "").strip()
    password_hash = os.getenv("PROCUREMENT_LOCAL_PASSWORD_HASH", "").strip()
    tenant_id = os.getenv("PROCUREMENT_LOCAL_TENANT_ID", "").strip()
    username = (body.username or body.email).strip()
    if (
        not api_token
        or not configured_username
        or not password_hash
        or not _LOCAL_TENANT_ID.fullmatch(tenant_id)
        or not secrets.compare_digest(username, configured_username)
        or not verify_local_password(body.password, password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid username or password")
    _ensure_local_tenant(tenant_id, username)
    ttl_seconds = max(300, int(os.getenv("AUTH_SESSION_TTL_SECONDS", "28800")))
    principal = Principal(
        subject=f"local:{username}",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
    )
    session_cookie = mint_local_session(
        api_token,
        subject=principal.subject,
        tenant_id=tenant_id,
        roles=principal.roles,
        ttl_seconds=ttl_seconds,
    )
    try:
        activate_local_session(session_cookie, principal, ttl_seconds=ttl_seconds)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="session store is unavailable") from exc
    response = JSONResponse(_account_payload(tenant_id, email=username))
    response.set_cookie(
        "procurement_session",
        session_cookie,
        httponly=True,
        secure=request.url.scheme == "https" or os.getenv("AUTH_COOKIE_SECURE", "0") == "1",
        samesite="strict",
        max_age=ttl_seconds,
        path="/",
    )
    return response


@router.get("/workbench/account", include_in_schema=False)
def workbench_account(request: Request, include_projects: bool = True) -> dict:
    principal = request.state.principal
    return _account_payload(principal.tenant_id, include_projects=include_projects)


@router.post("/v1/workbench/usage", include_in_schema=False, status_code=204)
def record_workbench_usage(body: WorkbenchUsage, request: Request) -> Response:
    from sqlalchemy import select

    from procurement.api import _actor_user_id
    from procurement.domain.models import WorkbenchProject
    from procurement.platform.auth import require_role
    from procurement.platform.database import session_scope
    from procurement.platform.operations import audit
    from procurement.workbench_api import _sessions

    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor", "approver")
    with session_scope(_sessions()) as session:
        project_id = session.scalar(
            select(WorkbenchProject.id).where(
                WorkbenchProject.id == body.project_id,
                WorkbenchProject.tenant_id == principal.tenant_id,
            )
        )
        if not project_id:
            raise HTTPException(status_code=404, detail="project not found")
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=_actor_user_id(session, principal),
            event_type="workbench.step.viewed",
            entity_type="workbench_project",
            entity_id=project_id,
            reason=None,
            before=None,
            after={"step": body.step, "previous_step": body.previous_step},
        )
    return Response(status_code=204)


@router.patch("/workbench/account", include_in_schema=False)
def workbench_account_update(body: LocalProfile, request: Request) -> dict:
    principal = request.state.principal
    from sqlalchemy import select

    from procurement.domain.models import User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        account = session.scalar(
            select(User).where(
                User.tenant_id == principal.tenant_id,
                User.subject == principal.subject,
            )
        )
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        account.display_name = body.name.strip() or "Local procurement operator"
        account.phone = body.phone.strip()
    return _account_payload(principal.tenant_id)


@router.post("/workbench/account/logout", include_in_schema=False)
def workbench_logout(request: Request) -> Response:
    api_token = os.getenv("PROCUREMENT_API_TOKEN") or os.getenv("API_AUTH_TOKEN") or ""
    cookie = request.cookies.get("procurement_session", "")
    if cookie and api_token:
        try:
            revoke_local_session(cookie, api_token)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="session store is unavailable") from exc
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("procurement_session", path="/", samesite="strict")
    return response


@router.post("/workbench/account/register", include_in_schema=False)
def workbench_register() -> None:
    raise HTTPException(status_code=405, detail="local account registration is disabled")
