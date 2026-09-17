from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from careos.api.deps import AuditContextDep, ContainerDep, SessionDep, require
from careos.modules.devices import service
from careos.modules.devices.schemas import CreateDeviceRequest, DeviceView
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission

router = APIRouter(prefix="/v1/devices", tags=["devices"])


@router.get("", response_model=list[DeviceView])
async def list_devices(
    principal: Annotated[Principal, Depends(require(Permission.DEVICES_READ))],
    session: SessionDep,
    container: ContainerDep,
    service_user_id: uuid.UUID | None = None,
) -> list[DeviceView]:
    return await service.list_devices(
        session, principal, container.settings.device_low_battery_threshold, service_user_id
    )


@router.post("", response_model=DeviceView, status_code=status.HTTP_201_CREATED)
async def create_device(
    body: CreateDeviceRequest,
    principal: Annotated[Principal, Depends(require(Permission.DEVICES_MANAGE))],
    session: SessionDep,
    container: ContainerDep,
    context: AuditContextDep,
) -> DeviceView:
    return await service.create_device(
        session, principal, body, container.settings.device_low_battery_threshold, context
    )
