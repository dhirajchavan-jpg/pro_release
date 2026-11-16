"""Pydantic and document serialization at the domain boundary."""

import os
from typing import Any

from pydantic import BaseModel


MAX_TOTAL_CHARS = int(os.getenv("MAX_TOTAL_CHARS", "14000"))


def validate_model(model: type[BaseModel], data: Any) -> Any:
    if hasattr(model, "model_validate"):
        return model.model_validate(data)
    return model.parse_obj(data)


def model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return value


def document_context(documents: list[Any]) -> list[dict[str, Any]]:
    if not documents:
        return []
    per_document_chars = max(1, MAX_TOTAL_CHARS // len(documents))
    return [
        {
            "document_id": document.record.document_id,
            "file_name": document.record.file_name,
            "upload_side": document.record.upload_side,
            "extraction_status": document.record.extraction_status,
            "page_count": document.record.page_count,
            "logical_document_ids": document.record.logical_document_ids,
            "classification": model_dump(document.record.classification) if document.record.classification else None,
            "text": document.text[:per_document_chars],
        }
        for document in documents
    ]
