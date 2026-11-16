"""Redact credentials from API and worker logs."""

from __future__ import annotations

from copy import deepcopy
import logging
import os
import re
from typing import Any

from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter


_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|session[_-]?token|auth(?:orization)?|password|secret|x-api-key)\b\s*[:=]\s*)([^\s,;&]+)"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SECRET_ENV_NAME = re.compile(r"(?i)(?:api[_-]?key|token|password|secret)")


def redact_log_text(value: object) -> str:
    text = str(value)
    for name, secret in os.environ.items():
        if len(secret) >= 6 and _SECRET_ENV_NAME.search(name):
            text = text.replace(secret, "[REDACTED]")
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return _JWT.sub("[REDACTED]", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


class RedactingDefaultFormatter(DefaultFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


class RedactingAccessFormatter(AccessFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


def uvicorn_log_config() -> dict[str, Any]:
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"]["()"] = RedactingDefaultFormatter
    config["formatters"]["access"]["()"] = RedactingAccessFormatter
    return config


def configure_worker_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for handler in logging.getLogger().handlers:
        formatter = handler.formatter
        handler.setFormatter(
            RedactingFormatter(
                formatter._fmt if formatter else None,
                formatter.datefmt if formatter else None,
            )
        )
