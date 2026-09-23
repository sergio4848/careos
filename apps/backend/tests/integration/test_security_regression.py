"""Cross-tenant security regression suite.

Organisation B (Northshire) attacks organisation A (Demo Care UK) with every identifier A
owns. Nothing about A may leak through a response body, an error message, a count or a
WebSocket event: for every endpoint, B's response for A's real identifier must be
indistinguishable from its response for an identifier that does not exist at all.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from careos.api.app import create_app
from careos.bootstrap import Container, ProviderRegistry, build_container
from careos.core.config import Settings
from careos.core.time import utcnow
from careos.modules.escalation_engine.models import EscalationPolicy
from careos.modules.identity.models import User
from careos.modules.identity.rbac import Role
from careos.modules.incident_engine.models import IncidentAssignment
from careos.modules.notification_engine.models import Call, CallStatus, CallTargetType
from careos.modules.service_users.models import TrustedContact
from tests.factories import TEST_PASSWORD, ClientFactory, Tenant, sos_event
from tests.integration.helpers import (
    GATEWAY_HEADER,
    count,
    incident_count,
    raise_sos,
    send_event,
)

SERVICE_USER_BODY = {"first_name": "Mallory", "last_name": "Intruder"}
CONTACT_BODY = {
    "full_name": "Mallory Intruder",
    "relationship": "Neighbour",
    "phone_number": "+447700900999",
    "priority": 5,
}
POLICY_BODY = {
    "name": "hijack",
    "steps": [{"delay_seconds": 0, "action_type": "OPERATOR_ESCALATION"}],
}

# (method, path, body). Placeholders are filled with A's identifiers or with random ones.
ENDPOINTS: tuple[tuple[str, str, Any], ...] = (
    ("GET", "/v1/incidents/{incident}", None),
    ("GET", "/v1/incidents/{incident}/timeline", None),
    ("POST", "/v1/incidents/{incident}/takeover", None),
    ("GET", "/v1/incidents/{incident}/calls", None),
    ("POST", "/v1/incidents/{incident}/calls/{voice_call}/stop", None),
    ("POST", "/v1/incidents/{incident}/escalate-now", None),
    ("POST", "/v1/incidents/{incident}/resolve", {"category": "USER_SAFE", "notes": "x"}),
    ("POST", "/v1/incidents/{incident}/close", {}),
    ("GET", "/v1/service-users/{service_user}", None),
    ("PUT", "/v1/service-users/{service_user}", SERVICE_USER_BODY),
    ("POST", "/v1/service-users/{service_user}/contacts", CONTACT_BODY),
    ("PUT", "/v1/service-users/{service_user}/contacts/{contact}", CONTACT_BODY),
    ("PUT", "/v1/escalation-policies/{policy}", POLICY_BODY),
    ("POST", "/v1/simulator/devices/{device}/events", {"event_type": "SOS_BUTTON"}),
    ("PATCH", "/v1/users/{user}/role", {"role": "CAREGIVER"}),
    (
        "POST",
        "/v1/devices",
        {
            "external_id": "ROGUE-1",
            "device_type": "SOS_PENDANT",
            "manufacturer": "Acme",
            "service_user_id": "{service_user}",
        },
    ),
    # Smuggling a foreign reference through a request body on B's *own* record.
    (
        "PUT",
        "/v1/service-users/{own_service_user}",
        {**SERVICE_USER_BODY, "escalation_policy_id": "{policy}"},
    ),
)


def fill(template: Any, ids: dict[str, str]) -> Any:
    if isinstance(template, str):
        return template.format(**ids)
    if isinstance(template, dict):
        return {key: fill(value, ids) for key, value in template.items()}
    return template


def comparable(response: httpx.Response) -> tuple[int, Any]:
    body = response.json()
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body["error"].pop("request_id", None)
    return response.status_code, body


async def tenant_a_identifiers(
    container: Container, tenant: Tenant, incidents: list[dict[str, Any]]
) -> dict[str, str]:
    async with container.session_factory() as session:
        contact = await session.scalar(
            select(TrustedContact.id).where(
                TrustedContact.organisation_id == tenant.organisation_id
            )
        )
        policy = await session.scalar(
            select(EscalationPolicy.id).where(
                EscalationPolicy.organisation_id == tenant.organisation_id
            )
        )
        user = await session.scalar(
            select(User.id).where(User.email == tenant.emails[Role.OPERATOR])
        )
        voice_call = Call(
            id=uuid.uuid4(),
            organisation_id=tenant.organisation_id,
            incident_id=uuid.UUID(incidents[0]["incident_id"]),
            target_type=CallTargetType.SERVICE_USER,
            service_user_id=tenant.service_user_id,
            provider="twilio",
            status=CallStatus.IN_PROGRESS,
            started_at=utcnow(),
        )
        session.add(voice_call)
        await session.commit()
    return {
        "incident": incidents[0]["incident_id"],
        "voice_call": str(voice_call.id),
        "service_user": str(tenant.service_user_id),
        "contact": str(contact),
        "policy": str(policy),
        "device": str(tenant.device_id),
        "user": str(user),
    }


def leaks(text: str, secrets: list[str]) -> list[str]:
    return [secret for secret in secrets if secret in text]


async def test_foreign_identifiers_are_indistinguishable_from_nonexistent_ones(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    incidents = [
        await raise_sos(gateway, tenant),
        await raise_sos(gateway, tenant, device_id=tenant.unassigned_device_external_id),
    ]
    a_ids = await tenant_a_identifiers(container, tenant, incidents)
    secrets = [
        *a_ids.values(),
        str(tenant.organisation_id),
        tenant.device_external_id,
        tenant.slug,
        *(i["incident_reference"] for i in incidents),
    ]
    intruder = await client_factory(other_tenant.emails[Role.ORGANISATION_ADMIN])
    own = {"own_service_user": str(other_tenant.service_user_id)}

    for method, path, body in ENDPOINTS:
        fake_ids = {key: str(uuid.uuid4()) for key in a_ids}
        real = await intruder.request(
            method, fill(path, {**a_ids, **own}), json=fill(body, {**a_ids, **own})
        )
        fake = await intruder.request(
            method, fill(path, {**fake_ids, **own}), json=fill(body, {**fake_ids, **own})
        )
        assert real.status_code == 404, (method, path, real.text)
        assert comparable(real) == comparable(fake), (method, path)
        assert leaks(real.text, secrets) == [], (method, path)

    # Nothing happened to A as a side effect of the attempts.
    assert await incident_count(container.session_factory, tenant.organisation_id) == 2
    assert await count(container.session_factory, IncidentAssignment) == 0
    owner = await client_factory(tenant.emails[Role.OPERATOR])
    for incident in incidents:
        detail = (await owner.get(f"/v1/incidents/{incident['incident_id']}")).json()
        assert detail["status"] in ("OPEN", "ESCALATED") and detail["assignee"] is None


async def test_lists_counts_and_audit_never_reflect_another_organisation(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    intruder = await client_factory(other_tenant.emails[Role.ORGANISATION_ADMIN])
    views = (
        ("/v1/incidents", {"scope": "active"}),
        ("/v1/incidents", {"scope": "awaiting_closure"}),
        ("/v1/incidents", {"scope": "recent"}),
        ("/v1/service-users", None),
        ("/v1/devices", None),
        ("/v1/escalation-policies", None),
        ("/v1/users", None),
        ("/v1/dashboard/summary", None),
    )

    async def snapshot() -> list[Any]:
        return [(await intruder.get(path, params=params)).json() for path, params in views]

    before = await snapshot()

    # Organisation A is busy: alarms, a takeover, a resolution, new contacts, audit entries.
    gateway = await client_factory()
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    manager = await client_factory(tenant.emails[Role.CARE_MANAGER])
    first = await raise_sos(gateway, tenant)
    second = await raise_sos(gateway, tenant, device_id=tenant.unassigned_device_external_id)
    await operator.post(f"/v1/incidents/{first['incident_id']}/takeover")
    await operator.post(
        f"/v1/incidents/{first['incident_id']}/resolve", json={"category": "USER_SAFE"}
    )
    await manager.post(f"/v1/service-users/{tenant.service_user_id}/contacts", json=CONTACT_BODY)
    await manager.get(f"/v1/service-users/{tenant.service_user_id}")

    assert await snapshot() == before  # identical bodies, therefore identical counts

    secrets = [
        str(tenant.organisation_id),
        str(tenant.service_user_id),
        str(tenant.device_id),
        first["incident_id"],
        second["incident_id"],
        first["incident_reference"],
        tenant.slug,
    ]
    for params in (
        None,
        {"resource_id": first["incident_id"]},
        {"resource_type": "incident"},
        {"action": "INCIDENT_CREATED"},
        {"action": "INCIDENT_TAKEOVER"},
    ):
        response = await intruder.get("/v1/audit-logs", params=params)
        assert response.status_code == 200
        assert leaks(response.text, secrets) == []
        if params is not None:
            assert response.json() == []


async def test_gateway_credential_cannot_reach_another_organisations_device(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    foreign = await send_event(gateway, other_tenant, sos_event(tenant.device_external_id))
    unknown = await send_event(gateway, other_tenant, sos_event("DEV-DOES-NOT-EXIST"))
    assert foreign.status_code == 422
    assert comparable(foreign) == comparable(unknown)
    assert await incident_count(container.session_factory, tenant.organisation_id) == 0
    assert await incident_count(container.session_factory, other_tenant.organisation_id) == 0


# ----------------------------------------------------------------------------- WebSocket


@pytest.fixture
def sync_client(
    settings: Settings, providers: ProviderRegistry, container: Container
) -> Iterator[TestClient]:
    engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
    isolated = build_container(settings, with_hub=True, providers=providers, engine=engine)
    with TestClient(create_app(settings, isolated), base_url="http://testserver") as client:
        yield client


def login(client: TestClient, email: str) -> None:
    response = client.post("/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert response.status_code == 200


def raise_alarm(client: TestClient, tenant: Tenant) -> str:
    response = client.post(
        "/v1/gateway/careos/events",
        json=sos_event(tenant.device_external_id),
        headers={GATEWAY_HEADER: tenant.gateway_key},
    )
    assert response.status_code == 202
    return str(response.json()["incident_id"])


def test_websocket_events_never_reach_another_organisation(
    sync_client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    origin = {"origin": "http://localhost:3000"}
    login(sync_client, other_tenant.emails[Role.OPERATOR])
    with sync_client.websocket_connect("/v1/ws", headers=origin) as socket_b:
        assert socket_b.receive_json() == {"type": "connection.ready"}
        login(sync_client, tenant.emails[Role.OPERATOR])  # the next socket authenticates as A
        with sync_client.websocket_connect("/v1/ws", headers=origin) as socket_a:
            assert socket_a.receive_json() == {"type": "connection.ready"}

            incident_a = raise_alarm(sync_client, tenant)
            received_a = [socket_a.receive_json(), socket_a.receive_json()]
            assert {m["type"] for m in received_a} == {"incident.created", "device.updated"}
            assert any(m.get("incident_id") == incident_a for m in received_a)

            # Delivery is complete before the gateway responds, so a ping is a sync barrier:
            # anything addressed to B would arrive before the pong.
            socket_b.send_text("ping")
            assert socket_b.receive_text() == "pong"

            incident_b = raise_alarm(sync_client, other_tenant)
            received_b = [socket_b.receive_json(), socket_b.receive_json()]
            assert {m["organisation_id"] for m in received_b} == {str(other_tenant.organisation_id)}
            assert any(m.get("incident_id") == incident_b for m in received_b)
            assert incident_a not in json.dumps(received_b)
            socket_a.send_text("ping")
            assert socket_a.receive_text() == "pong"
