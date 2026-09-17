from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from careos.api.deps import AuditContextDep, SessionDep, require
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission
from careos.modules.organisations import service
from careos.modules.organisations.models import Organisation
from careos.modules.organisations.schemas import CreateOrganisationRequest, OrganisationView

router = APIRouter(tags=["organisations"])


@router.get("/v1/organisations/current", response_model=OrganisationView)
async def current_organisation(
    principal: Annotated[Principal, Depends(require(Permission.ORGANISATION_READ))],
    session: SessionDep,
) -> Organisation:
    return await service.get_current(session, principal)


@router.get("/v1/platform/organisations", response_model=list[OrganisationView])
async def list_organisations(
    _: Annotated[Principal, Depends(require(Permission.PLATFORM_ORGANISATIONS_MANAGE))],
    session: SessionDep,
) -> list[Organisation]:
    return await service.list_all(session)


@router.post(
    "/v1/platform/organisations",
    response_model=OrganisationView,
    status_code=status.HTTP_201_CREATED,
)
async def create_organisation(
    body: CreateOrganisationRequest,
    principal: Annotated[Principal, Depends(require(Permission.PLATFORM_ORGANISATIONS_MANAGE))],
    session: SessionDep,
    context: AuditContextDep,
) -> Organisation:
    return await service.onboard(session, principal, body, context)
