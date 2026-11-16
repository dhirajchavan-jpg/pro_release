"""Validated schema for configurable tender-understanding fields."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


FieldStatus = Literal[
    "found",
    "not_found",
    "not_applicable",
    "ambiguous",
    "need_more_evidence",
    "unresolved",
]

VALUE_TYPES = {
    "string",
    "string_array",
    "number",
    "integer",
    "boolean",
    "date",
    "money",
    "object",
    "object_array",
}

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalGuidance(_StrictModel):
    aliases: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    likely_sections: list[str] = Field(default_factory=list)
    anchor_phrases: list[str] = Field(default_factory=list)
    binding_phrases: list[str] = Field(default_factory=list)


class FieldValidation(_StrictModel):
    require_evidence: bool = True
    require_anchor_for_positive: bool = False
    allow_multiple: bool = False
    instructions: list[str] = Field(default_factory=list)


class RfpFieldSpec(_StrictModel):
    key: str
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    value_type: str = "string"
    prompt: str = ""
    retrieval_guidance: RetrievalGuidance = Field(default_factory=RetrievalGuidance)
    validation: FieldValidation = Field(default_factory=FieldValidation)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _KEY_RE.fullmatch(value):
            raise ValueError("field key must be lowercase snake_case")
        return value

    @field_validator("value_type")
    @classmethod
    def validate_value_type(cls, value: str) -> str:
        if value not in VALUE_TYPES:
            raise ValueError(f"unsupported value_type: {value}")
        return value

    @model_validator(mode="after")
    def validate_semantic_contract(self) -> "RfpFieldSpec":
        if not self.label.strip() or not self.description.strip():
            raise ValueError("field label and description must be non-empty")
        if (
            self.validation.require_anchor_for_positive
            and not any(value.strip() for value in self.retrieval_guidance.anchor_phrases)
        ):
            raise ValueError(
                "require_anchor_for_positive requires at least one anchor phrase"
            )
        return self


class RfpSectionSpec(_StrictModel):
    key: str
    label: str = Field(min_length=1)
    prompt: str = ""
    fields: list[RfpFieldSpec] = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _KEY_RE.fullmatch(value):
            raise ValueError("section key must be lowercase snake_case")
        return value


class RfpUnderstandingSchema(_StrictModel):
    schema_version: Literal["rfp-field-schema.v1"] = "rfp-field-schema.v1"
    schema_id: str = Field(min_length=1, max_length=120)
    prompt: str = ""
    sections: list[RfpSectionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_keys(self) -> "RfpUnderstandingSchema":
        section_keys = [section.key for section in self.sections]
        if len(section_keys) != len(set(section_keys)):
            raise ValueError("section keys must be unique")
        field_keys = [field.key for section in self.sections for field in section.fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("field keys must be unique across the schema")
        return self


def load_rfp_understanding_schema(path: Path) -> RfpUnderstandingSchema:
    data = json.loads(path.read_text(encoding="utf-8"))
    return RfpUnderstandingSchema.model_validate(data)


def schema_field_count(schema: RfpUnderstandingSchema) -> int:
    return sum(len(section.fields) for section in schema.sections)
