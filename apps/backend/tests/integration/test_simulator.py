from __future__ import annotations

from careos.bootstrap import Container
from careos.modules.identity.rbac import Role
from tests.factories import ClientFactory, Tenant
from tests.integration.helpers import audit_actions, event_types, incident_count


async def test_simulator_sends_vendor_payload_through_the_real_gateway(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    response = await operator.post(
        f"/v1/simulator/devices/{tenant.device_id}/events",
        json={"event_type": "SOS_BUTTON", "battery": 84, "signal": 92},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vendor_payload"]["kind"] == "SOS"  # vendor format, not the CareOS contract
    assert body["vendor_payload"]["imei"] == tenant.device_external_id
    assert body["gateway_status_code"] == 202
    gateway = body["gateway_response"]
    assert gateway["outcome"] == "INCIDENT_CREATED"
    assert gateway["incident_status"] == "OPEN"
    assert await incident_count(container.session_factory, tenant.organisation_id) == 1
    assert (await event_types(container.session_factory, gateway["incident_id"]))[
        0
    ] == "SOS_RECEIVED"
    assert "SIMULATOR_EVENT_SENT" in await audit_actions(container.session_factory)


async def test_simulator_exact_replay_is_deduplicated(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    first = (
        await operator.post(f"/v1/simulator/devices/{tenant.device_id}/events", json={})
    ).json()
    replay = (
        await operator.post(
            f"/v1/simulator/devices/{tenant.device_id}/events",
            json={
                "event_id": first["event_id"],
                "timestamp_ms": first["vendor_payload"]["ts_ms"],
                "battery": first["vendor_payload"]["bat_pct"],
                "signal": first["vendor_payload"]["rssi_pct"],
            },
        )
    ).json()
    assert replay["gateway_response"]["duplicate"] is True
    assert replay["gateway_response"]["incident_id"] == first["gateway_response"]["incident_id"]
    assert await incident_count(container.session_factory, tenant.organisation_id) == 1


async def test_simulator_requires_permission(client_factory: ClientFactory, tenant: Tenant) -> None:
    caregiver = await client_factory(tenant.emails[Role.CAREGIVER])
    response = await caregiver.post(f"/v1/simulator/devices/{tenant.device_id}/events", json={})
    assert response.status_code == 403
