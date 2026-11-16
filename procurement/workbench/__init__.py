"""Workbench API assembled from auditable workflow route groups."""

from fastapi import APIRouter

from procurement.workbench.audit import router as audit_router
from procurement.workbench.bidders import router as bidders_router
from procurement.workbench.compliance import router as compliance_router
from procurement.workbench.evaluation import router as evaluation_router
from procurement.workbench.projects import router as projects_router
from procurement.workbench.usage import router as usage_router


router = APIRouter()
router.routes.extend(audit_router.routes)
router.routes.extend(projects_router.routes)
router.routes.extend(bidders_router.routes)
router.routes.extend(evaluation_router.routes)
router.routes.extend(compliance_router.routes)
router.routes.extend(usage_router.routes)
