"""Sprint 1 acceptance tests.

SOS Simulator -> Device Gateway -> validation -> Incident Engine -> PostgreSQL ->
IncidentEvent timeline -> realtime notification -> takeover -> resolution -> audit.
All tests run against real PostgreSQL through the HTTP API.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from careos.bootstrap import Container
from careos.core.errors import InvalidStateTransitionError
from careos.modules.device_gateway.models import DeviceEventReceipt
from careos.modules.identity.rbac import Role
from careos.modules.incident_engine.models import Incident, IncidentAssignment
from careos.modules.incident_engine.state_machine import IncidentStatus, assert_transition
from tests.factories import ClientFactory, Tenant, sos_event
from tests.integration.helpers import (
    audit_actions,
    count,
    event_types,
    incident_count,
    raise_sos,
    send_event,
)


class RecordingSocket:
    """Stands in for an operator dashboard connected to the WebSocket hub."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, data: str) -> None:
        self.messages.append(data)


async def test_sos_creates_incident(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])

    # The simulator sends a vendor-format payload over HTTP to the gateway (never to incidents).
    response = await operator.post(
        f"/v1/simulator/devices/{tenant.device_id}/events",
        json={"event_type": "SOS_BUTTON", "battery": 84, "signal": 92},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vendor_payload"]["kind"] == "SOS"
    assert body["gateway_status_code"] == 202
    assert body["gateway_response"]["outcome"] == "INCIDENT_CREATED"
    incident_id = body["gateway_response"]["incident_id"]

    detail = (await operator.get(f"/v1/incidents/{incident_id}")).json()
    assert detail["priority"] == "CRITICAL"
    assert detail["status"] == "OPEN"
    assert detail["trigger_type"] == "SOS_BUTTON"
    assert detail["service_user"]["display_name"] == "Margaret Wilson"
    assert detail["device"]["external_id"] == tenant.device_external_id

    async with container.session_factory() as session:
        stored = await session.get(Incident, uuid.UUID(incident_id))
    assert stored is not None and stored.organisation_id == tenant.organisation_id

    summary = (await operator.get("/v1/dashboard/summary")).json()
    assert summary["active_incidents"] == summary["critical"] == summary["unacknowledged"] == 1


async def test_duplicate_event_does_not_create_second_incident(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    payload = sos_event(tenant.device_external_id)

    first = await send_event(gateway, tenant, payload)
    second = await send_event(gateway, tenant, payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert second.json()["incident_id"] == first.json()["incident_id"]

    # Concurrent redelivery of a new event (e.g. producer retries racing each other).
    burst = sos_event(tenant.device_external_id)
    clients = [await client_factory() for _ in range(5)]
    responses = await asyncio.gather(*(send_event(c, tenant, burst) for c in clients))
    assert all(r.status_code == 202 for r in responses)
    assert sum(1 for r in responses if not r.json()["duplicate"]) == 1

    assert await incident_count(container.session_factory, tenant.organisation_id) == 1
    assert await count(container.session_factory, DeviceEventReceipt) == 2


async def test_invalid_device_rejected(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()

    unknown = await send_event(gateway, tenant, sos_event("DEV-9999"))
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["reason"] == "unknown_device"

    malformed = await send_event(gateway, tenant, {"event_id": "evt_12345678", "device_id": "x"})
    assert malformed.status_code == 422

    bad_key = await gateway.post(
        "/v1/gateway/careos/events",
        json=sos_event(tenant.device_external_id),
        headers={"X-CareOS-Gateway-Key": "cgk_demo0001_not-the-right-secret-000"},
    )
    assert bad_key.status_code == 401

    assert await incident_count(container.session_factory, tenant.organisation_id) == 0
    assert await count(container.session_factory, DeviceEventReceipt) == 0
    actions = await audit_actions(container.session_factory)
    assert "GATEWAY_EVENT_REJECTED" in actions and "GATEWAY_AUTH_FAILED" in actions


async def test_cross_tenant_device_access_denied(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    # Another organisation's gateway credential cannot raise an alarm for this device.
    gateway = await client_factory()
    response = await send_event(gateway, other_tenant, sos_event(tenant.device_external_id))
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "unknown_device"
    assert await incident_count(container.session_factory, tenant.organisation_id) == 0
    assert await incident_count(container.session_factory, other_tenant.organisation_id) == 0

    # Another organisation's staff cannot see or trigger the device, or read its incidents.
    intruder = await client_factory(other_tenant.emails[Role.ORGANISATION_ADMIN])
    devices = (await intruder.get("/v1/devices")).json()
    assert tenant.device_external_id not in {d["external_id"] for d in devices}
    simulated = await intruder.post(f"/v1/simulator/devices/{tenant.device_id}/events", json={})
    assert simulated.status_code == 404

    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    assert (await intruder.get(f"/v1/incidents/{incident_id}")).status_code == 404
    assert (await intruder.post(f"/v1/incidents/{incident_id}/takeover")).status_code == 404
    assert (await intruder.get("/v1/incidents")).json() == []


async def test_invalid_state_transition(client_factory: ClientFactory, tenant: Tenant) -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_transition(IncidentStatus.CLOSED, IncidentStatus.OPEN)

    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]

    close_open = await operator.post(f"/v1/incidents/{incident_id}/close", json={})
    assert close_open.status_code == 409
    assert close_open.json()["error"]["code"] == "invalid_state_transition"

    await operator.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})
    assert (await operator.post(f"/v1/incidents/{incident_id}/close", json={})).status_code == 200

    for action in ("takeover", "resolve"):
        payload = {"category": "USER_SAFE"} if action == "resolve" else None
        reopened = await operator.post(f"/v1/incidents/{incident_id}/{action}", json=payload)
        assert reopened.status_code == 409, action
        assert reopened.json()["error"]["code"] == "invalid_state_transition"


async def test_operator_takeover(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    assert container.hub is not None
    other_dashboard = RecordingSocket()
    await container.hub.register(tenant.organisation_id, other_dashboard)  # type: ignore[arg-type]

    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]

    response = await operator.post(f"/v1/incidents/{incident_id}/takeover")
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "IN_PROGRESS"
    assert detail["assignee"]["name"] == f"Operator {tenant.slug}"

    async with container.session_factory() as session:
        assignment = await session.scalar(
            select(IncidentAssignment).where(
                IncidentAssignment.incident_id == uuid.UUID(incident_id)
            )
        )
    assert assignment is not None and assignment.released_at is None

    assert "OPERATOR_TAKEOVER" in await event_types(container.session_factory, incident_id)
    actions = await audit_actions(container.session_factory, resource_id=incident_id)
    assert "INCIDENT_TAKEOVER" in actions

    pushed = [m for m in other_dashboard.messages if incident_id in m]
    assert any('"incident.created"' in m for m in pushed)
    assert any('"OPERATOR_TAKEOVER"' in m for m in pushed)


async def test_double_operator_takeover(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    first = await client_factory(tenant.emails[Role.OPERATOR])
    second = await client_factory(tenant.second_operator_email)
    manager = await client_factory(tenant.emails[Role.CARE_MANAGER])

    for _ in range(3):  # repeated to exercise the race, not a lucky ordering
        incident_id = (await raise_sos(gateway, tenant))["incident_id"]
        responses = await asyncio.gather(
            first.post(f"/v1/incidents/{incident_id}/takeover"),
            second.post(f"/v1/incidents/{incident_id}/takeover"),
        )
        assert sorted(r.status_code for r in responses) == [200, 409]
        loser = next(r for r in responses if r.status_code == 409)
        assert loser.json()["error"]["code"] == "incident_already_assigned"
        active_owners = await count(
            container.session_factory,
            IncidentAssignment,
            IncidentAssignment.incident_id == uuid.UUID(incident_id),
            IncidentAssignment.released_at.is_(None),
        )
        assert active_owners == 1
        await manager.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})


async def test_incident_resolution(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    await operator.post(f"/v1/incidents/{incident_id}/takeover")

    invalid = await operator.post(
        f"/v1/incidents/{incident_id}/resolve", json={"category": "NOT_A_CATEGORY"}
    )
    assert invalid.status_code == 422

    resolved = await operator.post(
        f"/v1/incidents/{incident_id}/resolve",
        json={"category": "FAMILY_RESPONDED", "notes": "Sarah Wilson attended; Margaret is safe."},
    )
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "RESOLVED"
    assert body["resolution_category"] == "FAMILY_RESPONDED"
    assert body["resolution_notes"] == "Sarah Wilson attended; Margaret is safe."
    assert body["resolved_by"]["name"] == f"Operator {tenant.slug}"
    assert body["resolved_at"] is not None
    assert body["closed_at"] is None  # resolved is not closed
    assert body["is_active"] is False

    # Notes are optional.
    second_id = (await raise_sos(gateway, tenant))["incident_id"]
    no_notes = await operator.post(
        f"/v1/incidents/{second_id}/resolve", json={"category": "USER_SAFE"}
    )
    assert no_notes.status_code == 200
    assert no_notes.json()["resolution_notes"] is None


async def test_incident_event_timeline(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    await operator.post(f"/v1/incidents/{incident_id}/takeover")
    await operator.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})

    timeline = (await operator.get(f"/v1/incidents/{incident_id}/timeline")).json()
    assert [e["sequence"] for e in timeline] == list(range(1, len(timeline) + 1))
    types = [e["event_type"] for e in timeline]
    required = [
        "SOS_RECEIVED",
        "INCIDENT_CREATED",
        "INCIDENT_OPENED",
        "OPERATOR_TAKEOVER",
        "INCIDENT_RESOLVED",
    ]
    positions = [types.index(event) for event in required]
    assert positions == sorted(positions)

    by_type = {e["event_type"]: e for e in timeline}
    assert by_type["INCIDENT_OPENED"]["to_status"] == "OPEN"
    assert by_type["OPERATOR_TAKEOVER"]["to_status"] == "IN_PROGRESS"
    assert by_type["INCIDENT_RESOLVED"]["to_status"] == "RESOLVED"
    assert by_type["OPERATOR_TAKEOVER"]["actor"]["name"] == f"Operator {tenant.slug}"

    # Append-only: history cannot be rewritten, even with direct SQL.
    async with container.session_factory() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text("UPDATE incident_events SET message = 'tampered'"))


async def test_audit_log_created(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    manager = await client_factory(tenant.emails[Role.CARE_MANAGER])
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]

    await operator.get(f"/v1/incidents/{incident_id}")
    await operator.post(f"/v1/incidents/{incident_id}/takeover")
    await operator.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})

    actions = set(await audit_actions(container.session_factory, resource_id=incident_id))
    assert {
        "INCIDENT_CREATED",
        "INCIDENT_VIEWED",
        "INCIDENT_TAKEOVER",
        "INCIDENT_STATE_CHANGED",
        "INCIDENT_RESOLVED",
    } <= actions

    logs = (await manager.get("/v1/audit-logs", params={"resource_id": incident_id})).json()
    takeover = next(entry for entry in logs if entry["action"] == "INCIDENT_TAKEOVER")
    assert takeover["actor_name"] == f"Operator {tenant.slug}"
    assert takeover["outcome"] == "SUCCESS"

    # Operators cannot read the audit trail; nobody can modify it.
    assert (await operator.get("/v1/audit-logs")).status_code == 403
    async with container.session_factory() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text("DELETE FROM audit_logs"))
