from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from careos.core.errors import ConflictError, NotFoundError
from careos.core.time import utcnow
from careos.modules.audit import service as audit
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.devices.models import ConnectionStatus, Device, DeviceConnection
from careos.modules.devices.schemas import (
    ConnectionView,
    CreateDeviceRequest,
    DeviceView,
    ServiceUserRef,
)
from careos.modules.identity.principal import Principal
from careos.modules.service_users.models import ServiceUser


def _query(
    organisation_id: uuid.UUID,
) -> Select[tuple[Device, DeviceConnection, ServiceUser]]:
    return (
        select(Device, DeviceConnection, ServiceUser)
        .outerjoin(DeviceConnection, DeviceConnection.device_id == Device.id)
        .outerjoin(ServiceUser, ServiceUser.id == Device.service_user_id)
        .where(Device.organisation_id == organisation_id, Device.deleted_at.is_(None))
    )


def to_view(
    device: Device,
    connection: DeviceConnection | None,
    service_user: ServiceUser | None,
    low_battery_threshold: int,
) -> DeviceView:
    return DeviceView(
        id=device.id,
        external_id=device.external_id,
        device_type=device.device_type,
        manufacturer=device.manufacturer,
        model=device.model,
        adapter=device.adapter,
        status=device.status,
        service_user=(
            ServiceUserRef(
                id=service_user.id, display_name=service_user.display_name, city=service_user.city
            )
            if service_user
            else None
        ),
        connection=(
            ConnectionView(
                status=connection.status,
                last_seen_at=connection.last_seen_at,
                battery_level=connection.battery_level,
                signal_strength=connection.signal_strength,
            )
            if connection
            else None
        ),
        low_battery=bool(
            connection
            and connection.battery_level is not None
            and connection.battery_level < low_battery_threshold
        ),
        created_at=device.created_at,
    )


async def list_devices(
    session: AsyncSession,
    principal: Principal,
    low_battery_threshold: int,
    service_user_id: uuid.UUID | None = None,
) -> list[DeviceView]:
    stmt = _query(principal.tenant_id).order_by(Device.external_id)
    if service_user_id is not None:
        stmt = stmt.where(Device.service_user_id == service_user_id)
    rows = await session.execute(stmt)
    return [to_view(d, c, su, low_battery_threshold) for d, c, su in rows.tuples()]


async def create_device(
    session: AsyncSession,
    principal: Principal,
    body: CreateDeviceRequest,
    low_battery_threshold: int,
    context: AuditContext,
) -> DeviceView:
    org = principal.tenant_id
    if body.service_user_id is not None:
        exists = await session.scalar(
            select(ServiceUser.id).where(
                ServiceUser.id == body.service_user_id,
                ServiceUser.organisation_id == org,
                ServiceUser.deleted_at.is_(None),
            )
        )
        if exists is None:
            raise NotFoundError("Service user not found.")
    duplicate = await session.scalar(
        select(Device.id).where(
            Device.organisation_id == org, Device.external_id == body.external_id
        )
    )
    if duplicate is not None:
        raise ConflictError("A device with this identifier is already registered.")
    device = Device(
        organisation_id=org,
        external_id=body.external_id,
        device_type=body.device_type,
        manufacturer=body.manufacturer,
        model=body.model,
        adapter=body.adapter,
        service_user_id=body.service_user_id,
    )
    session.add(device)
    await session.flush()
    session.add(
        DeviceConnection(organisation_id=org, device_id=device.id, status=ConnectionStatus.UNKNOWN)
    )
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.DEVICE_ADDED,
            resource_type="device",
            resource_id=str(device.id),
            details={"external_id": device.external_id, "device_type": device.device_type.value},
        ),
        context,
    )
    await session.commit()
    row = (await session.execute(_query(org).where(Device.id == device.id))).tuples().one()
    return to_view(*row, low_battery_threshold)


async def mark_stale_devices_offline(session: AsyncSession, offline_after_seconds: int) -> int:
    """Worker sweep: devices silent for longer than the threshold are reported OFFLINE."""
    cutoff = utcnow() - timedelta(seconds=offline_after_seconds)
    result = await session.execute(
        update(DeviceConnection)
        .where(
            DeviceConnection.status == ConnectionStatus.ONLINE,
            DeviceConnection.last_seen_at < cutoff,
        )
        .values(status=ConnectionStatus.OFFLINE, updated_at=utcnow())
        .returning(DeviceConnection.organisation_id, DeviceConnection.device_id)
    )
    rows = result.all()
    await session.commit()
    return len(rows)
