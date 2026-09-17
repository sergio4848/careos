from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

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
from careos.modules.telephony import service as telephony_service
from careos.modules.telephony.service import VoiceCallView

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


@router.get(
    "/{incident_id}/calls",
    response_model=list[VoiceCallView],
    summary="Voice escalation calls for an incident",
    description=(
        "Automated call attempts with their provider status and, when an AI voice session "
        "ran, its structured advisory. Advisory data is decision support only and is "
        "always labelled for human review."
    ),
)
async def list_calls(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))],
    session: SessionDep,
) -> list[VoiceCallView]:
    return await telephony_service.list_incident_calls(session, principal, incident_id)


@router.post(
    "/{incident_id}/calls/{call_id}/stop",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop an automated call",
)
async def stop_call(
    incident_id: uuid.UUID,
    call_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_TAKEOVER))],
    uow: UowDep,
    container: ContainerDep,
    context: AuditContextDep,
) -> None:
    await telephony_service.stop_automated_call(
        uow,
        principal,
        incident_id,
        call_id,
        voice_provider=container.providers.voice,
        context=context,
    )


@router.post(
    "/{incident_id}/escalate-now",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Escalate now",
    description="Brings the operator-alert escalation step forward to run immediately.",
)
async def escalate_now(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_TAKEOVER))],
    uow: UowDep,
    context: AuditContextDep,
) -> dict[str, int]:
    accelerated = await telephony_service.accelerate_operator_escalation(
        uow, principal, incident_id, context=context
    )
    return {"accelerated_steps": accelerated}


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
