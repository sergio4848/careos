"""FastAPI dependencies: sessions, authentication, CSRF and permission checks.

Authorisation is centralised here. Route handlers declare the permission they need::

    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))]

and receive a Principal whose ``tenant_id`` scopes every query in the service layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie, APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from careos.bootstrap import Container
from careos.core.context import get_request_id
from careos.core.errors import CsrfValidationError, PermissionDeniedError
from careos.core.security import constant_time_equals
from careos.db.uow import UnitOfWork
from careos.modules.audit import service as audit
from careos.modules.audit.models import AuditOutcome
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission

SESSION_COOKIE = "careos_session"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

session_cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False, scheme_name="Session")
csrf_header_scheme = APIKeyHeader(name=CSRF_HEADER, auto_error=False, scheme_name="CSRF")


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    async with container.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_uow(session: SessionDep, container: ContainerDep) -> UnitOfWork:
    return container.uow(session)


UowDep = Annotated[UnitOfWork, Depends(get_uow)]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def get_audit_context(request: Request) -> AuditContext:
    return AuditContext(
        request_id=get_request_id(),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


AuditContextDep = Annotated[AuditContext, Depends(get_audit_context)]


async def get_principal(
    request: Request,
    session: SessionDep,
    container: ContainerDep,
    session_token: Annotated[str | None, Security(session_cookie_scheme)],
    csrf_token: Annotated[str | None, Security(csrf_header_scheme)],
) -> Principal:
    principal = await container.auth.authenticate(session, session_token)
    if request.method not in _SAFE_METHODS:
        expected = container.auth.csrf_token_for(session_token or "")
        if not csrf_token or not constant_time_equals(csrf_token, expected):
            raise CsrfValidationError()
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(permission: Permission) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory enforcing a permission; denials are audited."""

    async def dependency(
        request: Request,
        principal: PrincipalDep,
        container: ContainerDep,
        context: AuditContextDep,
    ) -> Principal:
        if not principal.has(permission):
            route = request.scope.get("route")
            await audit.record_isolated(
                container.session_factory,
                AuditEntry.by(
                    principal,
                    AuditAction.ACCESS_DENIED,
                    outcome=AuditOutcome.DENIED,
                    details={
                        "permission": permission.value,
                        "route": getattr(route, "path", None),
                        "method": request.method,
                    },
                ),
                context,
            )
            raise PermissionDeniedError()
        return principal

    dependency.__name__ = f"require_{permission.name.lower()}"
    return dependency
