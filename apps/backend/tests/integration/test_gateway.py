from __future__ import annotations

import uuid

from sqlalchemy import select

from careos.bootstrap import Container
from careos.modules.devices.models import DeviceConnection
from careos.modules.escalation_engine.models import ScheduledAction
from tests.factories import ClientFactory, Tenant, sos_event
from tests.integration.helpers import (
    audit_actions,
    event_types,
    incident_count,
    raise_sos,
    send_event,
)


async def test_event_id_reuse_with_different_payload_is_a_conflict(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    payload = sos_event(tenant.device_external_id)
    await send_event(client, tenant, payload)
    conflicting = {**payload, "device": {"battery": 5, "signal": 10}}
    response = await send_event(client, tenant, conflicting)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "event_id_conflict"
    assert "GATEWAY_EVENT_REJECTED" in await audit_actions(container.session_factory)


async def test_repeated_press_attaches_to_active_incident(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    first = await raise_sos(client, tenant)
    second = await raise_sos(client, tenant)
    assert second["outcome"] == "ATTACHED_TO_INCIDENT"
    assert second["incident_id"] == first["incident_id"]
    assert await incident_count(container.session_factory, tenant.organisation_id) == 1
    assert "ALARM_REPEATED" in await event_types(container.session_factory, first["incident_id"])


async def test_service_user_hint_must_match_registry(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    response = await send_event(
        client, tenant, sos_event(tenant.device_external_id, service_user_id=str(uuid.uuid4()))
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "service_user_mismatch"


async def test_invalid_gateway_credentials(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    payload = sos_event(tenant.device_external_id)
    missing = await client.post("/v1/gateway/careos/events", json=payload)
    wrong = await client.post(
        "/v1/gateway/careos/events",
        json=payload,
        headers={"X-CareOS-Gateway-Key": "cgk_demo0001_wrong-secret-value-000000"},
    )
    assert missing.status_code == wrong.status_code == 401
    assert "GATEWAY_AUTH_FAILED" in await audit_actions(container.session_factory)


async def test_invalid_payload_is_rejected_without_echoing_values(
    client_factory: ClientFactory, tenant: Tenant
) -> None:
    client = await client_factory()
    response = await send_event(
        client, tenant, {"event_id": "evt_1234567890", "device_id": "Margaret Wilson's pendant"}
    )
    assert response.status_code == 422
    assert "Margaret" not in response.text


async def test_heartbeat_updates_telemetry_without_incident(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    response = await send_event(
        client,
        tenant,
        sos_event(
            tenant.device_external_id, event_type="HEARTBEAT", device={"battery": 11, "signal": 50}
        ),
    )
    assert response.status_code == 202
    assert response.json()["outcome"] == "TELEMETRY_ONLY"
    assert await incident_count(container.session_factory, tenant.organisation_id) == 0
    async with container.session_factory() as session:
        connection = await session.scalar(
            select(DeviceConnection).where(DeviceConnection.device_id == tenant.device_id)
        )
    assert connection is not None and connection.battery_level == 11


async def test_unassigned_device_alarm_goes_straight_to_operators(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    response = await send_event(client, tenant, sos_event(tenant.unassigned_device_external_id))
    assert response.status_code == 202
    body = response.json()
    types = await event_types(container.session_factory, body["incident_id"])
    assert "VALIDATION_WARNING" in types
    async with container.session_factory() as session:
        actions = (
            await session.scalars(
                select(ScheduledAction.action_type).where(
                    ScheduledAction.incident_id == uuid.UUID(body["incident_id"])
                )
            )
        ).all()
    assert [a.value for a in actions] == ["OPERATOR_ESCALATION"]
