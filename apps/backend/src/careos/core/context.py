"""Request-scoped context (correlation IDs) shared by logging, audit and realtime."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("careos_request_id", default=None)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")


def new_request_id(candidate: str | None = None) -> str:
    """Accept a well-formed upstream ID (e.g. from a load balancer) or mint a new one."""
    if candidate and _VALID_REQUEST_ID.match(candidate):
        return candidate
    return uuid.uuid4().hex


def set_request_id(request_id: str | None) -> None:
    _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()
