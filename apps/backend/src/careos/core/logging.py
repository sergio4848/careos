"""Structured logging with request correlation and PII/secret redaction.

Every log line is a JSON object carrying ``request_id`` (when available). Keys that
commonly hold secrets or personal data are redacted before rendering, so a careless
``log.info(..., payload=...)`` cannot leak passwords, tokens or phone numbers.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "api_key",
    "gateway_key",
    "phone",
    "email",
    "notes",
    "address",
    "postcode",
    "latitude",
    "longitude",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: (REDACTED if _is_sensitive(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key == "event":
            continue
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


# Database errors can carry row values: PostgreSQL's DETAIL line ("Key (email)=(...) already
# exists") and SQLAlchemy's rendered parameters. Keep the error type and constraint, drop values.
_DATABASE_VALUE_LINES = re.compile(r"^(\s*)(DETAIL|HINT):.*$|\[parameters: .*?\]", re.MULTILINE)


def scrub_exception_values(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    exception = event_dict.get("exception")
    if isinstance(exception, str):
        event_dict["exception"] = _DATABASE_VALUE_LINES.sub(
            lambda match: f"{match.group(1) or ''}{match.group(2) or 'parameters'}: {REDACTED}",
            exception,
        )
    return event_dict


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_sensitive,
    ]


def build_formatter(json_logs: bool = True) -> logging.Formatter:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            scrub_exception_values,
            renderer,
        ],
    )


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    shared_processors = _shared_processors()
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_formatter(json_logs))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Uvicorn's access log duplicates our request log (and would print query strings).
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
