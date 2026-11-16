"""Validated request models shared by workbench routes."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LedgerCorrection(BaseModel):
    field_key: str = Field(min_length=1, max_length=255)
    status: str = Field(pattern="^(found|not_found|not_applicable|unresolved)$")
    value: Any = None
    conditions: list[Any] = Field(default_factory=list)
    exceptions: list[Any] = Field(default_factory=list)
    required_documents: list[Any] = Field(default_factory=list)
    reason: str = Field(min_length=10, max_length=4000)
    reviewed_by: str = Field(min_length=1, max_length=255)
    reviewed_at: str


class FreezeContract(BaseModel):
    draft: dict[str, Any]
    reviewed_by: str = Field(min_length=1, max_length=255)
    reviewed_at: str
    continue_with_limitations: bool = False


class ContractDraftUpdate(BaseModel):
    draft: dict[str, Any]
    revision: int = Field(ge=0)
    reason: str = Field(min_length=10, max_length=4000)
    reviewed_by: str = Field(min_length=1, max_length=255)
    reviewed_at: str


class ClassificationCorrection(BaseModel):
    logical_documents: list[dict[str, Any]] | None = None
    physical_routes: list[dict[str, Any]] | None = None
    reason: str = Field(min_length=3, max_length=4000)
    reviewed_by: str = Field(min_length=1, max_length=255)
    reviewed_at: str


class ClassificationApproval(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=255)
    reviewed_at: str
    reason: str = Field(default="Bidder document classification approved.", min_length=3, max_length=4000)


class ProjectUpdate(BaseModel):
    tender_number: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=500)


class HumanCriterionReview(BaseModel):
    decision: str = Field(pattern="^(meets_requirement|does_not_meet|clarification_required)$")
    checkpoint_id: str = Field(default="", max_length=255)
    corrected_value: str = Field(default="", max_length=4000)
    reason: str = Field(min_length=3, max_length=4000)
    reviewed_by: str = Field(min_length=1, max_length=255)
    reviewed_at: str


class ClarificationCreate(BaseModel):
    vendor_key: str = Field(min_length=1, max_length=120)
    criterion_id: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=10, max_length=8000)


class ClarificationResponse(BaseModel):
    response: str = Field(min_length=1, max_length=16000)


class ClarificationClose(BaseModel):
    reason: str = Field(min_length=3, max_length=4000)
