from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from prometheus_client import REGISTRY
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.api.app import create_app
from careos.bootstrap import Container, ProviderRegistry
from careos.modules.audit.models import AuditLog
from careos.modules.escalation_engine.executor import EscalationExecutor
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


def metric(name: str, **labels: str) -> float:
    """Current value of a Prometheus sample (0 when the label set was never observed)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def asgi_client(container: Container) -> httpx.AsyncClient:
    """HTTP client for an app built on a custom (e.g. fault-injected) container. Unhandled
    exceptions become 500 responses, as they would for a real client."""
    app = create_app(container.settings, container)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=app, raise_app_exceptions=False, client=("203.0.113.20", 5000)
        ),
        base_url="http://testserver",
    )


def make_executor(
    container: Container, providers: ProviderRegistry, **settings_overrides: object
) -> EscalationExecutor:
    settings = container.settings.model_copy(update=settings_overrides)
    return EscalationExecutor(
        settings=settings,
        session_factory=container.session_factory,
        publisher=container.realtime,
        voice=providers.voice,
        notifications=providers.notifications,
        ai=providers.ai,
        worker_id=f"test-worker-{uuid.uuid4().hex[:6]}",
    )


async def load_incident(container: Container, incident_id: uuid.UUID | str) -> Incident:
    async with container.session_factory() as session:
        incident = await session.get(Incident, uuid.UUID(str(incident_id)))
        assert incident is not None
        return incident


def at(incident: Incident, seconds: int) -> datetime:
    return incident.created_at + timedelta(seconds=seconds)
