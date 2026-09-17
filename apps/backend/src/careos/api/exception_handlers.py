"""Uniform error envelope: ``{"error": {"code", "message", "details", "request_id"}}``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from careos.core.context import get_request_id
from careos.core.errors import CareOSError, RateLimitedError
from careos.core.logging import get_logger

log = get_logger(__name__)


def _envelope(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": get_request_id(),
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CareOSError)
    async def handle_domain_error(_: Request, exc: CareOSError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("request.domain_error", code=exc.code)
        response = _envelope(exc.status_code, exc.code, exc.message, exc.details)
        if isinstance(exc, RateLimitedError):
            response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Report locations and error types only; never echo submitted values (may be PII).
        errors = [
            {
                "loc": [str(p) for p in err.get("loc", ())],
                "type": err.get("type"),
                "msg": err.get("msg"),
            }
            for err in exc.errors()
        ]
        return _envelope(
            422, "validation_failed", "The request could not be validated.", {"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _envelope(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled_error", error=type(exc).__name__)
        return _envelope(500, "internal_error", "An internal error occurred.")
