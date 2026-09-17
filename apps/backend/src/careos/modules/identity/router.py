from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from careos.api.deps import (
    SESSION_COOKIE,
    AuditContextDep,
    ContainerDep,
    PrincipalDep,
    SessionDep,
    require,
)
from careos.core.config import Settings
from careos.core.errors import AuthenticationRequiredError
from careos.modules.identity.models import User
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission, permissions_for
from careos.modules.identity.schemas import (
    ChangeRoleRequest,
    CreateUserRequest,
    LoginRequest,
    SessionView,
    UserView,
)
from careos.modules.identity.service import UserAdminService
from careos.modules.organisations.models import Organisation
from careos.modules.organisations.schemas import OrganisationView

auth_router = APIRouter(prefix="/v1/auth", tags=["auth"])
users_router = APIRouter(prefix="/v1/users", tags=["users"])


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain,
        path="/",
    )


async def _session_view(session: SessionDep, user: User, csrf_token: str) -> SessionView:
    organisation = (
        await session.get(Organisation, user.organisation_id) if user.organisation_id else None
    )
    return SessionView(
        user=UserView.model_validate(user),
        organisation=OrganisationView.model_validate(organisation) if organisation else None,
        permissions=sorted(p.value for p in permissions_for(user.role)),
        csrf_token=csrf_token,
    )


@auth_router.post("/login", response_model=SessionView)
async def login(
    body: LoginRequest,
    response: Response,
    session: SessionDep,
    container: ContainerDep,
    context: AuditContextDep,
) -> SessionView:
    result = await container.auth.login(session, body.email, body.password, context)
    _set_session_cookie(response, result.token, container.settings)
    return await _session_view(session, result.user, result.csrf_token)


@auth_router.get("/me", response_model=SessionView)
async def me(
    request: Request, principal: PrincipalDep, session: SessionDep, container: ContainerDep
) -> SessionView:
    user = await session.get(User, principal.user_id)
    if user is None:
        raise AuthenticationRequiredError()
    token = request.cookies.get(SESSION_COOKIE, "")
    return await _session_view(session, user, container.auth.csrf_token_for(token))


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    principal: PrincipalDep,
    session: SessionDep,
    container: ContainerDep,
    context: AuditContextDep,
) -> Response:
    await container.auth.logout(session, principal, context)
    response.status_code = status.HTTP_204_NO_CONTENT
    response.delete_cookie(SESSION_COOKIE, domain=container.settings.cookie_domain, path="/")
    return response


_users = UserAdminService()


@users_router.get("", response_model=list[UserView])
async def list_users(
    principal: Annotated[Principal, Depends(require(Permission.USERS_READ))],
    session: SessionDep,
) -> list[User]:
    return await _users.list_users(session, principal)


@users_router.post("", response_model=UserView, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    principal: Annotated[Principal, Depends(require(Permission.USERS_MANAGE))],
    session: SessionDep,
    context: AuditContextDep,
) -> User:
    return await _users.create_user(
        session,
        principal,
        email=str(body.email),
        full_name=body.full_name,
        role=body.role,
        password=body.password,
        context=context,
    )


@users_router.patch("/{user_id}/role", response_model=UserView)
async def change_role(
    user_id: uuid.UUID,
    body: ChangeRoleRequest,
    principal: Annotated[Principal, Depends(require(Permission.USERS_MANAGE))],
    session: SessionDep,
    context: AuditContextDep,
) -> User:
    return await _users.change_role(session, principal, user_id, body.role, context)
