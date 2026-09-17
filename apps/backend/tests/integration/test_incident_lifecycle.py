from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from careos.bootstrap import Container
from careos.modules.escalation_engine.models import ScheduledAction, ScheduledActionStatus
from careos.modules.identity.rbac import Role
from tests.factories import ClientFactory, Tenant
from tests.integration.helpers import audit_actions, event_types, raise_sos


async def test_false_alarm_category_moves_to_false_alarm_state(
    client_factory: ClientFactory, tenant: Tenant
) -> None:
    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    response = await operator.post(
        f"/v1/incidents/{incident_id}/resolve",
        json={"category": "FALSE_ALARM", "notes": "Pressed accidentally while dressing"},
    )
    assert response.status_code == 200 and response.json()["status"] == "FALSE_ALARM"


async def test_only_owner_or_override_can_resolve(
    client_factory: ClientFactory, tenant: Tenant
) -> None:
    gateway = await client_factory()
    owner = await client_factory(tenant.emails[Role.OPERATOR])
    other_operator = await client_factory(tenant.second_operator_email)
    manager = await client_factory(tenant.emails[Role.CARE_MANAGER])
    caregiver = await client_factory(tenant.emails[Role.CAREGIVER])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    await owner.post(f"/v1/incidents/{incident_id}/takeover")
    payload = {"category": "USER_SAFE", "notes": "Confirmed safe by phone"}

    assert (
        await other_operator.post(f"/v1/incidents/{incident_id}/resolve", json=payload)
    ).status_code == 409
    assert (
        await caregiver.post(f"/v1/incidents/{incident_id}/resolve", json=payload)
    ).status_code == 403
    assert (await caregiver.get(f"/v1/incidents/{incident_id}")).status_code == 200
    assert (
        await manager.post(f"/v1/incidents/{incident_id}/resolve", json=payload)
    ).status_code == 200


async def test_incident_view_is_audited(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    for _ in range(3):  # live consoles refetch on every realtime update
        assert (await operator.get(f"/v1/incidents/{incident_id}")).status_code == 200
    actions = await audit_actions(container.session_factory, resource_id=incident_id)
    assert actions.count("INCIDENT_VIEWED") == 1


async def test_timeline_and_audit_records_are_immutable(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    for statement in (
        "UPDATE incident_events SET message = 'tampered'",
        "DELETE FROM incident_events",
        "UPDATE audit_logs SET action = 'tampered'",
        "DELETE FROM audit_logs",
    ):
        async with container.session_factory() as session:
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(text(statement))
    assert len(await event_types(container.session_factory, incident_id)) == 5


async def test_resolved_incident_cancels_pending_escalation(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    await operator.post(
        f"/v1/incidents/{incident_id}/resolve",
        json={"category": "USER_SAFE", "notes": "Resolved before escalation"},
    )
    async with container.session_factory() as session:
        statuses = set(
            await session.scalars(
                select(ScheduledAction.status).where(
                    ScheduledAction.incident_id == uuid.UUID(incident_id)
                )
            )
        )
    assert statuses == {ScheduledActionStatus.CANCELLED}
