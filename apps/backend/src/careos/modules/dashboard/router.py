from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, func, select

from careos.api.deps import ContainerDep, SessionDep, require
from careos.modules.devices.models import ConnectionStatus, Device, DeviceConnection
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission
from careos.modules.incident_engine.models import Incident, IncidentPriority
from careos.modules.incident_engine.state_machine import (
    ACTIVE_STATUSES,
    AWAITING_CLOSURE_STATUSES,
)

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


class DashboardSummary(BaseModel):
    active_incidents: int
    critical: int
    unacknowledged: int
    awaiting_closure: int
    devices_online: int
    devices_offline: int
    low_battery: int


@router.get("/summary", response_model=DashboardSummary)
async def summary(
    principal: Annotated[Principal, Depends(require(Permission.DASHBOARD_READ))],
    session: SessionDep,
    container: ContainerDep,
) -> DashboardSummary:
    org = principal.tenant_id
    active = Incident.status.in_(ACTIVE_STATUSES)
    incidents = (
        await session.execute(
            select(
                func.count().filter(active),
                func.count().filter(and_(active, Incident.priority == IncidentPriority.CRITICAL)),
                func.count().filter(
                    and_(
                        active,
                        Incident.acknowledged_at.is_(None),
                        Incident.assigned_user_id.is_(None),
                    )
                ),
                func.count().filter(Incident.status.in_(AWAITING_CLOSURE_STATUSES)),
            ).where(Incident.organisation_id == org)
        )
    ).one()
    threshold = container.settings.device_low_battery_threshold
    devices = (
        await session.execute(
            select(
                func.count().filter(DeviceConnection.status == ConnectionStatus.ONLINE),
                func.count().filter(
                    DeviceConnection.status.is_(None)
                    | (DeviceConnection.status != ConnectionStatus.ONLINE)
                ),
                func.count().filter(DeviceConnection.battery_level < threshold),
            )
            .select_from(Device)
            .outerjoin(DeviceConnection, DeviceConnection.device_id == Device.id)
            .where(Device.organisation_id == org, Device.deleted_at.is_(None))
        )
    ).one()
    return DashboardSummary(
        active_incidents=incidents[0],
        critical=incidents[1],
        unacknowledged=incidents[2],
        awaiting_closure=incidents[3],
        devices_online=devices[0],
        devices_offline=devices[1],
        low_battery=devices[2],
    )
