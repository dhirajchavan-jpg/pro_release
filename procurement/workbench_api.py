"""Compatibility imports for the split workbench API."""

from procurement.api import _sessions
from procurement.platform.database import session_scope
from procurement.workbench import router
from procurement.workbench.common import _idempotency_key, _state
from procurement.workbench.evaluation import generate_project_report
from procurement.workbench.projects import create_contract_draft, list_projects


__all__ = [
    "_idempotency_key",
    "_sessions",
    "_state",
    "create_contract_draft",
    "generate_project_report",
    "list_projects",
    "router",
    "session_scope",
]
