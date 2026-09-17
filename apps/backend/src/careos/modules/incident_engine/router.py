from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from careos.api.deps import AuditContextDep, ContainerDep, SessionDep, UowDep, require
from careos.modules.audit import service as audit
from careos.modules.audit.service import AuditAction
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission
from careos.modules.incident_engine import queries
from careos.modules.incident_engine.schemas import (
    CloseIncidentRequest,
    IncidentDetail,
    IncidentEventView,
    IncidentSummary,
    ResolveIncidentRequest,
)
from careos.modules.incident_engine.service import IncidentEngine

router = APIRouter(prefix="/v1/incidents", tags=["incidents"])


def get_engine(container: ContainerDep) -> IncidentEngine:
    return IncidentEngine(escalation_max_attempts=container.settings.escalation_max_attempts)


EngineDep = Annotated[IncidentEngine, Depends(get_engine)]


@router.get("", response_model=list[IncidentSummary])
async def list_incidents(
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))],
    session: SessionDep,
    scope: queries.IncidentScope = "active",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[IncidentSummary]:
    return await queries.list_incidents(session, principal, scope, limit)


@router.get("/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))],
    session: SessionDep,
    context: AuditContextDep,
) -> IncidentDetail:
    detail = await queries.get_detail(session, principal, incident_id)
    await audit.record_view(
        session,
        principal,
        AuditAction.INCIDENT_VIEWED,
        resource_type="incident",
        resource_id=str(incident_id),
        context=context,
    )
    await session.commit()
    return detail


@router.get("/{incident_id}/timeline", response_model=list[IncidentEventView])
async def get_timeline(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))],
    session: SessionDep,
) -> list[IncidentEventView]:
    return await queries.get_timeline(session, principal, incident_id)


@router.post("/{incident_id}/takeover", response_model=IncidentDetail)
async def take_over(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_TAKEOVER))],
    uow: UowDep,
    engine: EngineDep,
    context: AuditContextDep,
) -> IncidentDetail:
    await engine.take_over(uow, principal, incident_id, context)
    return await queries.get_detail(uow.session, principal, incident_id)


@router.post("/{incident_id}/resolve", response_model=IncidentDetail)
async def resolve(
    incident_id: uuid.UUID,
    body: ResolveIncidentRequest,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_RESOLVE))],
    uow: UowDep,
    engine: EngineDep,
    context: AuditContextDep,
) -> IncidentDetail:
    await engine.resolve(
        uow, principal, incident_id, category=body.category, notes=body.notes, context=context
    )
    return await queries.get_detail(uow.session, principal, incident_id)


@router.post("/{incident_id}/close", response_model=IncidentDetail)
async def close(
    incident_id: uuid.UUID,
    body: CloseIncidentRequest,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_CLOSE))],
    uow: UowDep,
    engine: EngineDep,
    context: AuditContextDep,
) -> IncidentDetail:
    await engine.close(uow, principal, incident_id, notes=body.notes, context=context)
    return await queries.get_detail(uow.session, principal, incident_id)
