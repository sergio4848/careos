from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from careos.api.deps import AuditContextDep, ContainerDep, SessionDep, require
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission
from careos.modules.service_users import service
from careos.modules.service_users.schemas import (
    ServiceUserProfile,
    ServiceUserSummary,
    ServiceUserWrite,
    TrustedContactView,
    TrustedContactWrite,
)

router = APIRouter(prefix="/v1/service-users", tags=["service users"])

ReadPrincipal = Annotated[Principal, Depends(require(Permission.SERVICE_USERS_READ))]
ManagePrincipal = Annotated[Principal, Depends(require(Permission.SERVICE_USERS_MANAGE))]


@router.get("", response_model=list[ServiceUserSummary])
async def list_service_users(
    principal: ReadPrincipal, session: SessionDep
) -> list[ServiceUserSummary]:
    return await service.list_service_users(session, principal)


@router.post("", response_model=ServiceUserSummary, status_code=status.HTTP_201_CREATED)
async def create_service_user(
    body: ServiceUserWrite,
    principal: ManagePrincipal,
    session: SessionDep,
    context: AuditContextDep,
) -> ServiceUserSummary:
    su = await service.create_service_user(session, principal, body, context)
    return ServiceUserSummary(
        id=su.id,
        display_name=su.display_name,
        first_name=su.first_name,
        last_name=su.last_name,
        city=su.city,
        status=su.status,
        active_incident_count=0,
    )


@router.get("/{service_user_id}", response_model=ServiceUserProfile)
async def get_service_user(
    service_user_id: uuid.UUID,
    principal: ReadPrincipal,
    session: SessionDep,
    container: ContainerDep,
    context: AuditContextDep,
) -> ServiceUserProfile:
    return await service.get_profile(
        session,
        principal,
        service_user_id,
        low_battery_threshold=container.settings.device_low_battery_threshold,
        context=context,
    )


@router.put("/{service_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_service_user(
    service_user_id: uuid.UUID,
    body: ServiceUserWrite,
    principal: ManagePrincipal,
    session: SessionDep,
    context: AuditContextDep,
) -> None:
    await service.update_service_user(session, principal, service_user_id, body, context)


@router.post(
    "/{service_user_id}/contacts",
    response_model=TrustedContactView,
    status_code=status.HTTP_201_CREATED,
)
async def add_contact(
    service_user_id: uuid.UUID,
    body: TrustedContactWrite,
    principal: ManagePrincipal,
    session: SessionDep,
    context: AuditContextDep,
) -> TrustedContactView:
    return await service.add_contact(session, principal, service_user_id, body, context)


@router.put("/{service_user_id}/contacts/{contact_id}", response_model=TrustedContactView)
async def update_contact(
    service_user_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: TrustedContactWrite,
    principal: ManagePrincipal,
    session: SessionDep,
    context: AuditContextDep,
) -> TrustedContactView:
    return await service.update_contact(
        session, principal, service_user_id, contact_id, body, context
    )
