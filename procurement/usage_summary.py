"""Print the single-tenant provider usage summary without exposing credentials."""

from __future__ import annotations

import argparse
from decimal import Decimal
import os
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from procurement.domain.models import Operation, ProviderCall
from procurement.platform.database import make_session_factory
from procurement.platform.env import load_local_env
from procurement.workbench.usage import _aggregate, _usage_projection


def _money(value: object) -> str:
    return f"INR {Decimal(value or 0):,.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Show AI usage, cost, and cache activity.")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--operation-id", default="")
    args = parser.parse_args()
    load_local_env()
    tenant_id = os.getenv("PROCUREMENT_LOCAL_TENANT_ID", "").strip()
    with make_session_factory()() as session:
        if not tenant_id:
            from sqlalchemy import select
            from procurement.domain.models import Tenant
            tenant = session.scalar(select(Tenant.id).order_by(Tenant.created_at).limit(1))
            tenant_id = tenant or ""
        if not tenant_id:
            raise SystemExit("No tenant workspace is configured")

        tenant_filters = [ProviderCall.tenant_id == tenant_id]
        filters = list(tenant_filters)
        if args.project_id:
            filters.append(ProviderCall.project_id == args.project_id)
        if args.operation_id:
            filters.append(ProviderCall.operation_id == args.operation_id)
        tenant_summary = _usage_projection(session.execute(_aggregate(*tenant_filters)).one())
        summary = _usage_projection(session.execute(_aggregate(*filters)).one())
        stages = [
            {"name": row.group_name or "unassigned", **_usage_projection(row)}
            for row in session.execute(_aggregate(*filters, group=ProviderCall.stage))
        ]
        operation_usage = {
            row.group_name: _usage_projection(row)
            for row in session.execute(_aggregate(*filters, group=ProviderCall.operation_id))
        }
        operations = list(session.scalars(
            select(Operation).where(
                Operation.tenant_id == tenant_id,
                Operation.id.in_(list(operation_usage) or [""]),
            ).order_by(Operation.created_at.desc()).limit(8)
        ))
        providers = [
            {"name": row.group_name or "unassigned", **_usage_projection(row)}
            for row in session.execute(
                _aggregate(*filters, group=func.concat(ProviderCall.provider, " / ", ProviderCall.model))
            )
        ]

    limit = Decimal(os.getenv("AI_BUDGET_LIMIT_INR", "0") or "0")
    tenant_spent = Decimal(tenant_summary["estimated_cost_inr"] or "0")
    scope_spent = Decimal(summary["estimated_cost_inr"] or "0")
    scope = (
        f"operation {args.operation_id}" if args.operation_id else
        f"project {args.project_id}" if args.project_id else
        "this installation"
    )
    print(f"=== AI usage for {scope} ===")
    print(f"Installation: {Path.cwd()}")
    print(f"Tenant: {tenant_id}")
    print(f"Scope spend: {_money(scope_spent)} | Installation spend: {_money(tenant_spent)}")
    if limit > 0:
        print(f"Budget: {_money(limit)} | Remaining: {_money(max(Decimal(0), limit - tenant_spent))}")
    print(
        f"Calls: {summary['call_count']} | Tokens: {summary['total_tokens']:,} "
        f"| Cache hits: {summary['cache_hits']} | Failed: {summary['failed_calls']}"
    )
    if stages:
        print("By stage:")
        for stage in stages:
            print(
                f"  {stage['name']}: {_money(stage['estimated_cost_inr'])}; "
                f"{stage['total_tokens']:,} tokens; {stage['cache_hits']} cache hits"
            )
    if operations:
        print("Recent operations:")
        for operation in operations:
            usage = operation_usage[operation.id]
            print(
                f"  {operation.stage}: {operation.status}; {_money(usage['estimated_cost_inr'])}; "
                f"{usage['call_count']} calls; {usage['total_tokens']:,} tokens ({operation.id})"
            )
    if providers:
        print("By provider and model:")
        for provider in providers:
            print(
                f"  {provider['name']}: {_money(provider['estimated_cost_inr'])}; "
                f"{provider['call_count']} calls; {provider['total_tokens']:,} tokens"
            )


if __name__ == "__main__":
    try:
        main()
    except (OperationalError, ProgrammingError):
        raise SystemExit(
            "Cost summary unavailable because the database is not initialized or running. "
            "Run Start_All.bat first."
        ) from None
