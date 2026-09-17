from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.modules.audit.models import AuditLog
from careos.modules.incident_engine.models import Incident, IncidentEvent
from tests.factories import Tenant, sos_event

GATEWAY_HEADER = "X-CareOS-Gateway-Key"


async def send_event(
    client: httpx.AsyncClient, tenant: Tenant, payload: dict[str, Any], adapter: str = "careos"
) -> httpx.Response:
    return await client.post(
        f"/v1/gateway/{adapter}/events", json=payload, headers={GATEWAY_HEADER: tenant.gateway_key}
    )


async def raise_sos(client: httpx.AsyncClient, tenant: Tenant, **overrides: Any) -> dict[str, Any]:
    response = await send_event(client, tenant, sos_event(tenant.device_external_id, **overrides))
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def count(session_factory: async_sessionmaker[AsyncSession], model: Any, *where: Any) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def incident_count(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> int:
    return await count(session_factory, Incident, Incident.organisation_id == org_id)


async def event_types(
    session_factory: async_sessionmaker[AsyncSession], incident_id: uuid.UUID | str
) -> list[str]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(IncidentEvent.event_type)
            .where(IncidentEvent.incident_id == uuid.UUID(str(incident_id)))
            .order_by(IncidentEvent.sequence)
        )
        return [row.value for row in rows]


async def audit_actions(
    session_factory: async_sessionmaker[AsyncSession], **filters: Any
) -> list[str]:
    async with session_factory() as session:
        stmt = select(AuditLog.action).order_by(AuditLog.created_at)
        for key, value in filters.items():
            stmt = stmt.where(getattr(AuditLog, key) == value)
        return list(await session.scalars(stmt))
