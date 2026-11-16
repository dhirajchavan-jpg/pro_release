"""Tenant-scoped provider usage, timing, and cost reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import os
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, select

from procurement.api import _actor_user_id, _sessions
from procurement.domain.models import Operation, ProviderCall, WorkbenchProject
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.operations import audit


router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


class StepView(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    step: Literal["tender", "ledger", "compliance", "contract", "bids", "evaluate", "results", "report"]
    previous_step: Literal["tender", "ledger", "compliance", "contract", "bids", "evaluate", "results", "report"] | None = None


def _money(value: Any) -> str:
    return str((Decimal(value or 0)).quantize(Decimal("0.00000001")))


def _usage_projection(row: Any) -> dict[str, Any]:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return {
        "call_count": int(mapping["call_count"] or 0),
        "failed_calls": int(mapping["failed_calls"] or 0),
        "budget_blocked_calls": int(mapping["budget_blocked_calls"] or 0),
        "cache_hits": int(mapping["cache_hits"] or 0),
        "retry_count": int(mapping["retry_count"] or 0),
        "input_tokens": int(mapping["input_tokens"] or 0),
        "output_tokens": int(mapping["output_tokens"] or 0),
        "total_tokens": int(mapping["total_tokens"] or 0),
        "input_units": int(mapping["input_units"] or 0),
        "provider_time_ms": int(mapping["provider_time_ms"] or 0),
        "estimated_cost_usd": (
            _money(mapping["estimated_cost_usd"])
            if mapping["estimated_cost_usd"] is not None
            else None
        ),
        "estimated_cost_inr": (
            _money(mapping["estimated_cost_inr"])
            if mapping["estimated_cost_inr"] is not None
            else None
        ),
        "unpriced_successful_calls": int(mapping["unpriced_successful_calls"] or 0),
    }


def _aggregate(*filters: Any, group: Any | None = None) -> Any:
    columns = [
        func.count(ProviderCall.id).label("call_count"),
        func.sum(case((ProviderCall.status == "failed", 1), else_=0)).label("failed_calls"),
        func.sum(case((ProviderCall.status == "budget_blocked", 1), else_=0)).label(
            "budget_blocked_calls"
        ),
        func.sum(case((ProviderCall.cache_status == "hit", 1), else_=0)).label("cache_hits"),
        func.sum(
            case((ProviderCall.attempts > 1, ProviderCall.attempts - 1), else_=0)
        ).label("retry_count"),
        func.sum(ProviderCall.input_tokens).label("input_tokens"),
        func.sum(ProviderCall.output_tokens).label("output_tokens"),
        func.sum(ProviderCall.total_tokens).label("total_tokens"),
        func.sum(ProviderCall.input_units).label("input_units"),
        func.sum(ProviderCall.duration_ms).label("provider_time_ms"),
        func.sum(ProviderCall.estimated_cost_usd).label("estimated_cost_usd"),
        func.sum(ProviderCall.estimated_cost_inr).label("estimated_cost_inr"),
        func.sum(
            case(
                (
                    and_(
                        ProviderCall.status == "succeeded",
                        ProviderCall.estimated_cost_usd.is_(None),
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("unpriced_successful_calls"),
    ]
    if group is not None:
        columns.insert(0, group.label("group_name"))
    query = select(*columns).where(*filters)
    return query.group_by(group).order_by(group) if group is not None else query


@router.post("/usage", status_code=status.HTTP_204_NO_CONTENT)
def record_step_view(request: Request, body: StepView) -> None:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor", "approver")
    with session_scope(_sessions()) as session:
        project = session.scalar(
            select(WorkbenchProject).where(
                WorkbenchProject.id == body.project_id,
                WorkbenchProject.tenant_id == principal.tenant_id,
            )
        )
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=_actor_user_id(session, principal),
            event_type="workbench.step.viewed",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before={"step": body.previous_step} if body.previous_step else None,
            after={"step": body.step},
        )


@router.get("/usage")
def provider_usage(
    request: Request,
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    principal = require_role(
        request,
        "admin",
        "procurement_officer",
        "reviewer",
        "auditor",
        "approver",
    )
    with session_scope(_sessions()) as session:
        filters: list[Any] = [ProviderCall.tenant_id == principal.tenant_id]
        if project_id:
            owned = session.scalar(
                select(WorkbenchProject.id).where(
                    WorkbenchProject.id == project_id,
                    WorkbenchProject.tenant_id == principal.tenant_id,
                    WorkbenchProject.stage != "deleted",
                )
            )
            if not owned:
                from fastapi import HTTPException

                raise HTTPException(status_code=404, detail="project not found")
            filters.append(ProviderCall.project_id == project_id)

        summary = _usage_projection(session.execute(_aggregate(*filters)).one())
        tenant_summary = _usage_projection(
            session.execute(
                _aggregate(ProviderCall.tenant_id == principal.tenant_id)
            ).one()
        )
        budget_limit = Decimal(os.getenv("AI_BUDGET_LIMIT_INR", "0") or "0")
        tenant_spend = Decimal(tenant_summary["estimated_cost_inr"] or "0")
        remaining = max(Decimal(0), budget_limit - tenant_spend)
        stages = [
            {"stage": row.group_name, **_usage_projection(row)}
            for row in session.execute(_aggregate(*filters, group=ProviderCall.stage))
        ]
        capabilities = [
            {"capability": row.group_name, **_usage_projection(row)}
            for row in session.execute(_aggregate(*filters, group=ProviderCall.capability))
        ]
        operation_rows = list(
            session.execute(_aggregate(*filters, group=ProviderCall.operation_id))
        )
        operations = {
            operation.id: operation
            for operation in session.scalars(
                select(Operation).where(
                    Operation.tenant_id == principal.tenant_id,
                    Operation.id.in_([row.group_name for row in operation_rows] or [""]),
                )
            )
        }
        now = datetime.now(timezone.utc)
        operation_usage = []
        for row in operation_rows:
            operation = operations.get(row.group_name)
            started = operation.started_at if operation else None
            finished = operation.finished_at if operation else None
            operation_usage.append(
                {
                    "operation_id": row.group_name,
                    "stage": operation.stage if operation else None,
                    "status": operation.status if operation else None,
                    "attempt": operation.attempt if operation else None,
                    "queue_time_ms": (
                        round((started - operation.created_at).total_seconds() * 1000)
                        if operation and started and operation.created_at
                        else None
                    ),
                    "wall_time_ms": (
                        round(((finished or now) - started).total_seconds() * 1000)
                        if started
                        else None
                    ),
                    **_usage_projection(row),
                }
            )
        recent = list(
            session.scalars(
                select(ProviderCall)
                .where(*filters)
                .order_by(ProviderCall.started_at.desc(), ProviderCall.id.desc())
                .limit(limit)
            )
        )
        return {
            "project_id": project_id,
            "budget": {
                "enabled": budget_limit > 0,
                "limit_inr": _money(budget_limit),
                "spent_inr": _money(tenant_spend),
                "remaining_inr": _money(remaining),
                "percent_used": (
                    str((tenant_spend * 100 / budget_limit).quantize(Decimal("0.01")))
                    if budget_limit > 0
                    else None
                ),
                "exhausted": budget_limit > 0 and tenant_spend >= budget_limit,
                "blocked_calls": tenant_summary["budget_blocked_calls"],
                "pricing_revision": os.getenv("AI_PRICING_REVISION", "unversioned"),
                "usd_to_inr": os.getenv("USD_TO_INR_RATE", ""),
            },
            "summary": summary,
            "stages": stages,
            "capabilities": capabilities,
            "operations": operation_usage,
            "recent_calls": [
                {
                    "id": call.id,
                    "operation_id": call.operation_id,
                    "project_id": call.project_id,
                    "tender_version_id": call.tender_version_id,
                    "bid_id": call.bid_id,
                    "evaluation_id": call.evaluation_id,
                    "stage": call.stage,
                    "capability": call.capability,
                    "provider": call.provider,
                    "model": call.model,
                    "task": call.task,
                    "endpoint_host": call.endpoint_host,
                    "status": call.status,
                    "attempts": call.attempts,
                    "cache_status": call.cache_status,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "total_tokens": call.total_tokens,
                    "input_units": call.input_units,
                    "unit_name": call.unit_name,
                    "duration_ms": call.duration_ms,
                    "estimated_cost_usd": (
                        _money(call.estimated_cost_usd)
                        if call.estimated_cost_usd is not None
                        else None
                    ),
                    "estimated_cost_inr": (
                        _money(call.estimated_cost_inr)
                        if call.estimated_cost_inr is not None
                        else None
                    ),
                    "priced": call.estimated_cost_usd is not None,
                    "error_category": call.error_category,
                    "http_status": call.http_status,
                    "started_at": call.started_at.isoformat(),
                }
                for call in recent
            ],
        }
