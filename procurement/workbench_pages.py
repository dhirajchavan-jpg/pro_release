"""Serve the packaged bid workbench frontend pages and static assets."""

import os
import re
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from procurement.platform.auth import (
    Principal,
    create_access_token,
    create_refresh_token,
    decode_jwt,
    hash_local_password,
    jwt_access_ttl_seconds,
    jwt_principal_from_token,
    require_permission,
    require_role,
    verify_local_password,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND_PRO_DIST = Path(__file__).resolve().parent.parent / "frontend_pro" / "dist"

ALLOWED_STEPS = frozenset({"tender", "ledger", "compliance", "contract", "bids", "evaluate", "results", "report"})

router = APIRouter()
_LOCAL_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,35}$")
_FRONTEND_MODULE_NAME = re.compile(r"^[a-z][a-z0-9-]*\.js$")
_ROLE_NAME = r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$"


class LocalLogin(BaseModel):
    username: str = ""
    email: str = ""
    password: str


class LocalProfile(BaseModel):
    name: str = "Local procurement operator"
    phone: str = ""


class RegistrationRequest(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8)
    phone: str = ""


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="Team member", min_length=1, max_length=255)
    role: str = Field(default="reviewer", pattern=_ROLE_NAME)
    password: str = Field(min_length=8)
    phone: str = ""


class UpdateUserRequest(BaseModel):
    role: str | None = Field(default=None, pattern=_ROLE_NAME)
    is_active: bool | None = None
    display_name: str | None = None
    phone: str | None = None


class CreateInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default="reviewer", pattern=_ROLE_NAME)
    ttl_days: int = Field(default=7, ge=1, le=30)


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8)
    phone: str = ""


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=_ROLE_NAME)
    description: str = Field(default="", max_length=1000)
    permission_slugs: list[str] = Field(default_factory=list)


class UpdateRoleRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=_ROLE_NAME)
    description: str | None = Field(default=None, max_length=1000)
    permission_slugs: list[str] | None = None


class WorkbenchUsage(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    step: str = Field(pattern="^(tender|ledger|compliance|contract|bids|evaluate|results|report)$")
    previous_step: str | None = Field(
        default=None,
        pattern="^(tender|ledger|compliance|contract|bids|evaluate|results|report)$",
    )


_BEARER_SESSION_SECURITY = {"security": [{"BearerAuth": []}]}


def _token_auth_response(
    *,
    tenant_id: str,
    user_id: str,
    email: str,
    access_token: str,
    refresh_token: str,
    ttl_seconds: int,
    request: Request,
    status_code: int = 200,
) -> JSONResponse:
    """Return a JWT access token for browser and programmatic clients."""
    payload = _account_payload(tenant_id, user_id=user_id, email=email)
    payload.update({
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ttl_seconds,
    })
    response = JSONResponse(payload, status_code=status_code)
    response.set_cookie(
        "procurement_refresh",
        refresh_token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=int(os.getenv("JWT_REFRESH_TTL_SECONDS", "604800")),
        path="/workbench/account",
    )
    return response


def _create_refresh_token(session, user) -> str:
    from procurement.domain.models import RefreshToken

    token, token_id = create_refresh_token(
        user.id,
        user.tenant_id,
        user.subject,
        user.role,
        user.token_version,
    )
    claims = decode_jwt(token, "refresh")
    session.add(RefreshToken(
        id=token_id,
        user_id=user.id,
        expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
    ))
    return token


def _accessible_role(session, tenant_id: str, role_name: str):
    from sqlalchemy import or_, select

    from procurement.domain.models import Role

    return session.scalar(
        select(Role).where(
            Role.name == role_name.strip(),
            or_(Role.is_system == True, Role.tenant_id == tenant_id),
        )
    )


def _role_payload(role, permission_slugs: list[str]) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description or "",
        "is_system": role.is_system,
        "permissions": sorted(permission_slugs),
        "created_at": role.created_at.isoformat() if role.created_at else None,
    }


def _role_payloads(session, tenant_id: str) -> list[dict]:
    from sqlalchemy import or_, select

    from procurement.domain.models import Permission, Role, RolePermission

    roles = session.scalars(
        select(Role)
        .where(or_(Role.is_system == True, Role.tenant_id == tenant_id))
        .order_by(Role.is_system.desc(), Role.name)
    ).all()
    permission_map: dict[str, list[str]] = {role.id: [] for role in roles}
    if permission_map:
        rows = session.execute(
            select(RolePermission.role_id, Permission.slug)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id.in_(permission_map))
        )
        for role_id, slug in rows:
            permission_map[role_id].append(slug)
    return [_role_payload(role, permission_map[role.id]) for role in roles]


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
    user_id: str | None = None,
    email: str | None = None,
    include_projects: bool = True,
) -> dict:
    from sqlalchemy import select

    from procurement.domain.models import Evaluation, Review, Tender, TenderVersion, User, WorkbenchBid, WorkbenchProject
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        account = None
        if user_id:
            account = session.get(User, user_id)
        if not account:
            account = session.scalar(select(User).where(User.tenant_id == tenant_id).order_by(User.created_at).limit(1))
        user = {
            "id": account.id if account else None,
            "name": (account.display_name if account else None) or "Procurement operator",
            "email": email or (account.email if account else None) or "operator@workspace",
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
def admin_dashboard(request: Request) -> RedirectResponse:
    require_role(request, "admin")
    return RedirectResponse("/app/admin", status_code=307)


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
def workbench_home() -> RedirectResponse:
    return RedirectResponse("/app", status_code=307)


@router.get("/workbench/login", include_in_schema=False)
def workbench_auth() -> RedirectResponse:
    return RedirectResponse("/app/login", status_code=307)


@router.get("/workbench/register", include_in_schema=False)
def workbench_register_page() -> RedirectResponse:
    return RedirectResponse("/app/register", status_code=307)


@router.get("/workbench/ui/{project_id}/{step}", include_in_schema=False)
def workbench_step(project_id: str, step: str) -> RedirectResponse:
    if step not in ALLOWED_STEPS:
        raise HTTPException(status_code=404, detail="unknown workbench step")
    react_step = {"ledger": "review", "contract": "checklist"}.get(step, step)
    return RedirectResponse(f"/app/projects/{project_id}/{react_step}", status_code=307)


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


@router.post(
    "/workbench/account/login",
    tags=["Authentication"],
    summary="Sign in and obtain a bearer token",
)
def workbench_login(body: LocalLogin, request: Request) -> Response:
    username = (body.username or body.email).strip()
    if not username or not body.password:
        raise HTTPException(status_code=401, detail="invalid username or password")

    from sqlalchemy import func, or_, select

    from procurement.domain.models import User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    clean_user = username.lower()
    with session_scope(_sessions()) as session:
        user = session.scalar(
            select(User).where(
                User.is_active == True,
                User.password_hash.isnot(None),
                or_(
                    func.lower(User.email) == clean_user,
                    func.lower(User.subject) == clean_user,
                    func.lower(User.subject) == f"local:{clean_user}",
                ),
            )
        )
        if not user or not user.password_hash or not verify_local_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="invalid username or password")
        user_id = user.id
        tenant_id = user.tenant_id
        user_email = user.email or username
        user_subject = user.subject
        user_role = user.role
        user_token_version = user.token_version
        refresh_token = _create_refresh_token(session, user)

    ttl_seconds = jwt_access_ttl_seconds()
    access_token = create_access_token(
        user_id, tenant_id, user_subject, user_role, user_token_version, ttl_seconds=ttl_seconds
    )
    return _token_auth_response(
        tenant_id=tenant_id,
        user_id=user_id,
        email=user_email,
        access_token=access_token,
        refresh_token=refresh_token,
        ttl_seconds=ttl_seconds,
        request=request,
    )


@router.get("/workbench/account", tags=["Workspace account"], openapi_extra=_BEARER_SESSION_SECURITY)
def workbench_account(request: Request, include_projects: bool = True) -> dict:
    principal = request.state.principal
    return _account_payload(principal.tenant_id, user_id=principal.user_id, include_projects=include_projects)


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


@router.patch("/workbench/account", tags=["Workspace account"], openapi_extra=_BEARER_SESSION_SECURITY)
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
        account.display_name = body.name.strip() or "Procurement operator"
        account.phone = body.phone.strip()
    return _account_payload(principal.tenant_id, user_id=principal.user_id)


@router.post("/workbench/account/logout", tags=["Authentication"], openapi_extra=_BEARER_SESSION_SECURITY)
def workbench_logout(request: Request) -> Response:
    principal = request.state.principal
    from sqlalchemy import update

    from procurement.domain.models import RefreshToken, User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        session.execute(update(User).where(User.id == principal.user_id).values(token_version=User.token_version + 1))
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == principal.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("procurement_refresh", path="/workbench/account", samesite="strict")
    return response


@router.post(
    "/workbench/account/refresh",
    tags=["Authentication"],
    summary="Rotate refresh token and obtain a new access token",
)
def refresh_access_token(request: Request) -> Response:
    raw_token = request.cookies.get("procurement_refresh", "")
    if not raw_token:
        raise HTTPException(status_code=401, detail="refresh token is required")
    claims = decode_jwt(raw_token, "refresh")
    principal = jwt_principal_from_token(raw_token, expected_type="refresh")

    from sqlalchemy import select

    from procurement.domain.models import RefreshToken, User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        stored_token = session.scalar(
            select(RefreshToken)
            .where(RefreshToken.id == str(claims["jti"]))
            .with_for_update()
        )
        user = session.get(User, principal.user_id)
        if (
            not stored_token
            or stored_token.user_id != principal.user_id
            or stored_token.revoked_at is not None
            or not user
        ):
            raise HTTPException(status_code=401, detail="refresh token is revoked or unavailable")
        stored_token.revoked_at = datetime.now(timezone.utc)
        refresh_token = _create_refresh_token(session, user)
        user_email = user.email or user.subject
        access_token = create_access_token(
            user.id,
            user.tenant_id,
            user.subject,
            user.role,
            user.token_version,
            ttl_seconds=jwt_access_ttl_seconds(),
        )

    return _token_auth_response(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        email=user_email,
        access_token=access_token,
        refresh_token=refresh_token,
        ttl_seconds=jwt_access_ttl_seconds(),
        request=request,
    )


@router.post(
    "/workbench/account/register",
    tags=["Authentication"],
    summary="Create the first workspace administrator",
)
def workbench_register(body: RegistrationRequest, request: Request) -> Response:
    """Create the named workspace and its initial administrator."""
    from sqlalchemy import func, select

    from procurement.domain.models import Role, Tenant, User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    email = body.email.strip().lower()
    workspace_name = body.workspace_name.strip()
    if not workspace_name:
        raise HTTPException(status_code=422, detail="workspace name is required")
    with session_scope(_sessions()) as session:
        # This application uses PostgreSQL; serialize bootstrap registrations so
        # concurrent first requests cannot create multiple administrators.
        session.execute(select(func.pg_advisory_xact_lock(729_184_301)))
        if session.scalar(select(User.id).limit(1)):
            raise HTTPException(status_code=403, detail="registration is available only for the first workspace administrator")

        tenant = Tenant(name=workspace_name)
        session.add(tenant)
        session.flush()

        role = session.scalar(select(Role).where(Role.name == "admin").limit(1))
        if role is None:
            role = Role(name="admin", description="Workspace administrator", is_system=True)
            session.add(role)
            session.flush()

        user = User(
            tenant_id=tenant.id,
            subject=f"user:{email}",
            email=email,
            password_hash=hash_local_password(body.password),
            display_name=body.name.strip(),
            phone=body.phone.strip(),
            role="admin",
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        session.flush()
        user_id = user.id
        tenant_id = user.tenant_id
        refresh_token = _create_refresh_token(session, user)

    ttl_seconds = jwt_access_ttl_seconds()
    access_token = create_access_token(
        user_id, tenant_id, f"user:{email}", "admin", 0, ttl_seconds=ttl_seconds
    )
    return _token_auth_response(
        tenant_id=tenant_id,
        user_id=user_id,
        email=email,
        access_token=access_token,
        refresh_token=refresh_token,
        ttl_seconds=ttl_seconds,
        request=request,
        status_code=201,
    )


@router.get("/workbench/account/permissions", tags=["Workspace roles"], openapi_extra=_BEARER_SESSION_SECURITY)
def list_workspace_permissions(request: Request) -> dict:
    require_permission(request, "roles:manage")

    from sqlalchemy import select

    from procurement.domain.models import Permission
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        permissions = session.scalars(select(Permission).order_by(Permission.category, Permission.slug)).all()
        return {
            "permissions": [
                {"slug": permission.slug, "category": permission.category, "description": permission.description or ""}
                for permission in permissions
            ]
        }


@router.get("/workbench/account/roles", tags=["Workspace roles"], openapi_extra=_BEARER_SESSION_SECURITY)
def list_workspace_roles(request: Request) -> dict:
    require_permission(request, "roles:manage")
    principal = request.state.principal

    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        return {"roles": _role_payloads(session, principal.tenant_id)}


@router.post("/workbench/account/roles", tags=["Workspace roles"], openapi_extra=_BEARER_SESSION_SECURITY, status_code=201)
def create_workspace_role(body: CreateRoleRequest, request: Request) -> dict:
    require_permission(request, "roles:manage")
    principal = request.state.principal

    from sqlalchemy import or_, select

    from procurement.domain.models import Permission, Role, RolePermission
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    name = body.name.strip()
    requested_permissions = set(body.permission_slugs)
    with session_scope(_sessions()) as session:
        existing = session.scalar(
            select(Role.id).where(Role.name == name, or_(Role.is_system == True, Role.tenant_id == principal.tenant_id))
        )
        if existing:
            raise HTTPException(status_code=409, detail="a system or workspace role with this name already exists")
        permissions = session.scalars(select(Permission).where(Permission.slug.in_(requested_permissions or [""]))).all()
        permission_by_slug = {permission.slug: permission for permission in permissions}
        unknown = requested_permissions - permission_by_slug.keys()
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown permissions: {', '.join(sorted(unknown))}")

        role = Role(
            tenant_id=principal.tenant_id,
            name=name,
            description=body.description.strip() or None,
            is_system=False,
        )
        session.add(role)
        session.flush()
        for permission in permission_by_slug.values():
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        return {"role": _role_payload(role, list(permission_by_slug))}


@router.patch("/workbench/account/roles/{role_id}", tags=["Workspace roles"], openapi_extra=_BEARER_SESSION_SECURITY)
def update_workspace_role(role_id: str, body: UpdateRoleRequest, request: Request) -> dict:
    require_permission(request, "roles:manage")
    principal = request.state.principal

    from sqlalchemy import select

    from procurement.domain.models import Permission, Role, RolePermission
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        role = session.get(Role, role_id)
        if not role or role.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="custom role not found")
        if role.is_system:
            raise HTTPException(status_code=403, detail="system roles cannot be changed")

        if body.name is not None:
            name = body.name.strip()
            duplicate = _accessible_role(session, principal.tenant_id, name)
            if duplicate and duplicate.id != role.id:
                raise HTTPException(status_code=409, detail="a system or workspace role with this name already exists")
            role.name = name
        if body.description is not None:
            role.description = body.description.strip() or None
        if body.permission_slugs is not None:
            requested_permissions = set(body.permission_slugs)
            permissions = session.scalars(select(Permission).where(Permission.slug.in_(requested_permissions or [""]))).all()
            permission_by_slug = {permission.slug: permission for permission in permissions}
            unknown = requested_permissions - permission_by_slug.keys()
            if unknown:
                raise HTTPException(status_code=422, detail=f"unknown permissions: {', '.join(sorted(unknown))}")
            mappings = session.scalars(select(RolePermission).where(RolePermission.role_id == role.id)).all()
            for mapping in mappings:
                session.delete(mapping)
            for permission in permission_by_slug.values():
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))
            permission_slugs = list(permission_by_slug)
        else:
            permission_slugs = session.scalars(
                select(Permission.slug)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
            ).all()
        return {"role": _role_payload(role, permission_slugs)}


@router.delete("/workbench/account/roles/{role_id}", tags=["Workspace roles"], openapi_extra=_BEARER_SESSION_SECURITY)
def delete_workspace_role(role_id: str, request: Request) -> dict:
    require_permission(request, "roles:manage")
    principal = request.state.principal

    from sqlalchemy import select

    from procurement.domain.models import Role, User, WorkspaceInvite
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        role = session.get(Role, role_id)
        if not role or role.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="custom role not found")
        if role.is_system:
            raise HTTPException(status_code=403, detail="system roles cannot be deleted")
        if session.scalar(select(User.id).where(User.tenant_id == principal.tenant_id, User.role_id == role.id).limit(1)):
            raise HTTPException(status_code=409, detail="reassign users before deleting this role")
        if session.scalar(
            select(WorkspaceInvite.id).where(
                WorkspaceInvite.tenant_id == principal.tenant_id,
                WorkspaceInvite.role == role.name,
                WorkspaceInvite.accepted_at.is_(None),
            ).limit(1)
        ):
            raise HTTPException(status_code=409, detail="revoke pending invitations before deleting this role")
        session.delete(role)
    return {"status": "deleted"}


@router.get("/workbench/account/users", tags=["Workspace users"], openapi_extra=_BEARER_SESSION_SECURITY)
def list_workspace_users(request: Request) -> dict:
    require_permission(request, "users:manage")
    principal = request.state.principal

    from sqlalchemy import select

    from procurement.domain.models import User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        users = session.scalars(
            select(User).where(User.tenant_id == principal.tenant_id).order_by(User.created_at)
        ).all()
        return {
            "users": [
                {
                    "id": u.id,
                    "email": u.email,
                    "name": u.display_name,
                    "phone": u.phone,
                    "role": u.role,
                    "is_active": u.is_active,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                }
                for u in users
            ]
        }


@router.post("/workbench/account/users", tags=["Workspace users"], openapi_extra=_BEARER_SESSION_SECURITY)
def create_workspace_user(body: CreateUserRequest, request: Request) -> dict:
    require_permission(request, "users:manage")
    principal = request.state.principal

    from sqlalchemy import select

    from procurement.domain.models import User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    email = body.email.strip().lower()
    with session_scope(_sessions()) as session:
        existing = session.scalar(
            select(User.id).where(User.tenant_id == principal.tenant_id, User.email == email)
        )
        if existing:
            raise HTTPException(status_code=409, detail="user with this email already exists")

        role = _accessible_role(session, principal.tenant_id, body.role)
        if not role:
            raise HTTPException(status_code=422, detail="role is not available in this workspace")
        new_user = User(
            tenant_id=principal.tenant_id,
            subject=f"user:{email}",
            email=email,
            password_hash=hash_local_password(body.password),
            display_name=body.name.strip(),
            phone=body.phone.strip(),
            role=role.name,
            role_id=role.id,
            is_active=True,
        )
        session.add(new_user)
        session.flush()
        return {
            "status": "created",
            "user": {
                "id": new_user.id,
                "email": new_user.email,
                "name": new_user.display_name,
                "role": new_user.role,
            },
        }


@router.patch("/workbench/account/users/{user_id}", tags=["Workspace users"], openapi_extra=_BEARER_SESSION_SECURITY)
def update_workspace_user(user_id: str, body: UpdateUserRequest, request: Request) -> dict:
    require_permission(request, "users:manage")
    principal = request.state.principal

    from sqlalchemy import func, select

    from procurement.domain.models import User
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        target = session.get(User, user_id)
        if not target or target.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="user not found")

        if body.role:
            role = _accessible_role(session, principal.tenant_id, body.role)
            if not role:
                raise HTTPException(status_code=422, detail="role is not available in this workspace")
            if target.role == "admin" and role.name != "admin":
                active_admins = session.scalar(
                    select(func.count()).select_from(User).where(
                        User.tenant_id == principal.tenant_id,
                        User.role == "admin",
                        User.is_active == True,
                    )
                )
                if active_admins <= 1:
                    raise HTTPException(status_code=409, detail="assign another active administrator before changing this role")
            target.role = role.name
            target.role_id = role.id
        if body.is_active is not None:
            if target.role == "admin" and target.is_active and not body.is_active:
                active_admins = session.scalar(
                    select(func.count()).select_from(User).where(
                        User.tenant_id == principal.tenant_id,
                        User.role == "admin",
                        User.is_active == True,
                    )
                )
                if active_admins <= 1:
                    raise HTTPException(status_code=409, detail="assign another active administrator before deactivating this user")
            target.is_active = body.is_active
        if body.display_name is not None:
            target.display_name = body.display_name.strip()
        if body.phone is not None:
            target.phone = body.phone.strip()

        return {
            "status": "updated",
            "user": {
                "id": target.id,
                "email": target.email,
                "name": target.display_name,
                "role": target.role,
                "is_active": target.is_active,
            },
        }


def _frontend_base_url(request: Request) -> str:
    from procurement.api_app import frontend_cors_origins
    origins = frontend_cors_origins()
    if origins:
        return origins[0]
    return f"{request.url.scheme}://{request.url.netloc}"


@router.get("/workbench/account/invites", tags=["Workspace invitations"], openapi_extra=_BEARER_SESSION_SECURITY)
def list_workspace_invites(request: Request) -> dict:
    require_permission(request, "users:manage")
    principal = request.state.principal

    from sqlalchemy import select
    from procurement.domain.models import WorkspaceInvite
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        invites = session.scalars(
            select(WorkspaceInvite)
            .where(WorkspaceInvite.tenant_id == principal.tenant_id)
            .order_by(WorkspaceInvite.created_at.desc())
        ).all()
        return {
            "invites": [
                {
                    "id": inv.id,
                    "email": inv.email,
                    "role": inv.role,
                    "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
                    "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
                    "created_at": inv.created_at.isoformat() if inv.created_at else None,
                }
                for inv in invites
            ]
        }


@router.post("/workbench/account/invites", tags=["Workspace invitations"], openapi_extra=_BEARER_SESSION_SECURITY)
def create_workspace_invite(body: CreateInviteRequest, request: Request) -> dict:
    require_permission(request, "users:manage")
    principal = request.state.principal

    from sqlalchemy import select
    from procurement.domain.models import User, WorkspaceInvite
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    email = body.email.strip().lower()
    with session_scope(_sessions()) as session:
        existing_user = session.scalar(
            select(User.id).where(User.tenant_id == principal.tenant_id, User.email == email)
        )
        if existing_user:
            raise HTTPException(status_code=409, detail="user with this email already belongs to this workspace")
        role = _accessible_role(session, principal.tenant_id, body.role)
        if not role:
            raise HTTPException(status_code=422, detail="role is not available in this workspace")

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.ttl_days)

        invite = WorkspaceInvite(
            tenant_id=principal.tenant_id,
            email=email,
            role=role.name,
            token_hash=token_hash,
            invited_by_id=principal.user_id,
            expires_at=expires_at,
        )
        session.add(invite)
        session.flush()

        base_url = _frontend_base_url(request)
        invite_url = f"{base_url}/invite?token={raw_token}"

        return {
            "id": invite.id,
            "email": invite.email,
            "role": invite.role,
            "invite_url": invite_url,
            "token": raw_token,
            "expires_at": invite.expires_at.isoformat(),
        }


@router.delete("/workbench/account/invites/{invite_id}", tags=["Workspace invitations"], openapi_extra=_BEARER_SESSION_SECURITY)
def delete_workspace_invite(invite_id: str, request: Request) -> dict:
    require_permission(request, "users:manage")
    principal = request.state.principal

    from procurement.domain.models import WorkspaceInvite
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    with session_scope(_sessions()) as session:
        invite = session.get(WorkspaceInvite, invite_id)
        if not invite or invite.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="invite not found")
        session.delete(invite)
    return {"status": "deleted"}


@router.get("/workbench/account/invites/verify", tags=["Workspace invitations"], summary="Verify an invitation")
def verify_workspace_invite(token: str = Query(...)) -> dict:
    clean_token = token.strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="token is required")

    from sqlalchemy import select
    from procurement.domain.models import Tenant, WorkspaceInvite
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    token_hash = hashlib.sha256(clean_token.encode()).hexdigest()
    with session_scope(_sessions()) as session:
        invite = session.scalar(select(WorkspaceInvite).where(WorkspaceInvite.token_hash == token_hash))
        if not invite or invite.accepted_at is not None:
            raise HTTPException(status_code=404, detail="invitation is invalid or has already been accepted")

        expires_at = invite.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="invitation link has expired")

        tenant = session.get(Tenant, invite.tenant_id)
        tenant_name = tenant.name if tenant else "Procurement Workspace"

        return {
            "valid": True,
            "email": invite.email,
            "role": invite.role,
            "tenant_name": tenant_name,
            "expires_at": expires_at.isoformat(),
        }


@router.post("/workbench/account/invites/accept", tags=["Workspace invitations"], summary="Accept an invitation and obtain a bearer token")
def accept_workspace_invite(body: AcceptInviteRequest, request: Request) -> Response:
    from sqlalchemy import select
    from procurement.domain.models import User, WorkspaceInvite
    from procurement.platform.database import session_scope
    from procurement.workbench_api import _sessions

    token_hash = hashlib.sha256(body.token.strip().encode()).hexdigest()
    with session_scope(_sessions()) as session:
        invite = session.scalar(select(WorkspaceInvite).where(WorkspaceInvite.token_hash == token_hash))
        if not invite or invite.accepted_at is not None:
            raise HTTPException(status_code=404, detail="invitation is invalid or has already been accepted")

        expires_at = invite.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="invitation link has expired")

        clean_email = invite.email.lower()
        existing = session.scalar(
            select(User.id).where(User.tenant_id == invite.tenant_id, User.email == clean_email)
        )
        if existing:
            raise HTTPException(status_code=409, detail="an account with this email already exists")

        role = _accessible_role(session, invite.tenant_id, invite.role)
        if not role:
            raise HTTPException(status_code=410, detail="the invitation's role is no longer available")
        new_user = User(
            tenant_id=invite.tenant_id,
            subject=f"user:{clean_email}",
            email=clean_email,
            password_hash=hash_local_password(body.password),
            display_name=body.name.strip(),
            phone=body.phone.strip(),
            role=role.name,
            role_id=role.id,
            is_active=True,
        )
        session.add(new_user)
        invite.accepted_at = datetime.now(timezone.utc)
        session.flush()

        user_id = new_user.id
        tenant_id = new_user.tenant_id
        user_email = new_user.email
        user_subject = new_user.subject
        user_role = new_user.role
        user_token_version = new_user.token_version
        refresh_token = _create_refresh_token(session, new_user)

    ttl_seconds = jwt_access_ttl_seconds()
    access_token = create_access_token(
        user_id, tenant_id, user_subject, user_role, user_token_version, ttl_seconds=ttl_seconds
    )
    return _token_auth_response(
        tenant_id=tenant_id,
        user_id=user_id,
        email=user_email,
        access_token=access_token,
        refresh_token=refresh_token,
        ttl_seconds=ttl_seconds,
        request=request,
    )
