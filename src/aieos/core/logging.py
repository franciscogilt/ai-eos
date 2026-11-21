"""Structured logging with secret redaction.

Redaction runs as the last processor before rendering, so it also covers
exception tracebacks and values bound through contextvars. It is a safety net:
code must still avoid logging secrets in the first place.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog
from pydantic import SecretBytes, SecretStr

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"pass(word|wd)?|secret|token|authorization|api[_-]?key|private[_-]?key|cookie|credential|"
    r"dsn|database_url|signature",
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[REDACTED_JWT]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_KEY_ID]"),
    # user:password@ in connection strings
    (
        re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@"),
        r"\g<scheme>\g<user>:[REDACTED]@",
    ),
)


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes)):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(v) for v in value]
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        sensitive_key = isinstance(key, str) and bool(_SENSITIVE_KEY.search(key))
        # Numbers/bools under a "token"-like key are counts (e.g. output_tokens), not secrets.
        if sensitive_key and not isinstance(value, (int, float, bool, type(None))):
            out[key] = REDACTED
        else:
            out[key] = redact_value(value)
    return out


def redact_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    return redact_mapping(event_dict)


def configure_logging(*, level: str = "INFO", json: bool = True, stream: TextIO | None = None) -> None:
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
