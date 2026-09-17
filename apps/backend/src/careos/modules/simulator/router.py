from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from careos.api.deps import AuditContextDep, ContainerDep, SessionDep, require
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission
from careos.modules.simulator import service
from careos.modules.simulator.service import SimulatorEventRequest, SimulatorEventResult

router = APIRouter(prefix="/v1/simulator", tags=["simulator (development only)"])


@router.post("/devices/{device_id}/events", response_model=SimulatorEventResult)
async def simulate_device_event(
    device_id: uuid.UUID,
    body: SimulatorEventRequest,
    principal: Annotated[Principal, Depends(require(Permission.SIMULATOR_USE))],
    session: SessionDep,
    container: ContainerDep,
    context: AuditContextDep,
) -> SimulatorEventResult:
    return await service.send_event(session, container, principal, device_id, body, context)
