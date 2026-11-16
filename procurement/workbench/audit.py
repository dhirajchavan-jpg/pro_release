"""Tenant-scoped audit-chain verification."""

from __future__ import annotations

from fastapi import APIRouter, Request

from procurement.api import _sessions
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.operations import verify_audit_chain


router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


@router.get("/audit/verify")
def audit_integrity(request: Request) -> dict:
    principal = require_role(request, "admin", "auditor")
    with session_scope(_sessions()) as session:
        return verify_audit_chain(session, principal.tenant_id)
