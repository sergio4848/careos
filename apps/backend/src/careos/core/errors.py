"""Domain error hierarchy.

Domain and application services raise these; the API layer maps them to HTTP
responses in one place (``careos.api.exception_handlers``). Messages must never
contain personal data because they are returned to clients and logged.
"""

from __future__ import annotations

from typing import Any


class CareOSError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "An internal error occurred."

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class AuthenticationRequiredError(CareOSError):
    status_code = 401
    code = "authentication_required"
    default_message = "Authentication is required."


class InvalidCredentialsError(CareOSError):
    status_code = 401
    code = "invalid_credentials"
    default_message = "Invalid email or password."


class PermissionDeniedError(CareOSError):
    status_code = 403
    code = "permission_denied"
    default_message = "You do not have permission to perform this action."


class CsrfValidationError(CareOSError):
    status_code = 403
    code = "csrf_failed"
    default_message = "CSRF token missing or invalid."


class NotFoundError(CareOSError):
    """Also used for cross-tenant access so resource existence is not disclosed."""

    status_code = 404
    code = "not_found"
    default_message = "Resource not found."


class ConflictError(CareOSError):
    status_code = 409
    code = "conflict"
    default_message = "The request conflicts with the current state of the resource."


class InvalidStateTransitionError(ConflictError):
    code = "invalid_state_transition"
    default_message = "This action is not allowed in the incident's current state."


class IncidentAlreadyAssignedError(ConflictError):
    code = "incident_already_assigned"
    default_message = "The incident has already been taken over by another operator."


class ValidationFailedError(CareOSError):
    status_code = 422
    code = "validation_failed"
    default_message = "The request could not be validated."


class RateLimitedError(CareOSError):
    status_code = 429
    code = "rate_limited"
    default_message = "Too many requests. Please retry later."

    def __init__(self, retry_after_seconds: int, message: str | None = None):
        super().__init__(message, details={"retry_after_seconds": retry_after_seconds})
        self.retry_after_seconds = retry_after_seconds


class FeatureDisabledError(CareOSError):
    status_code = 404
    code = "feature_disabled"
    default_message = "This feature is not enabled in the current environment."
