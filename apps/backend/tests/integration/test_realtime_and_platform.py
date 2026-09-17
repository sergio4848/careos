"""WebSocket delivery, health/readiness, security headers and administrative audit trail."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from careos.api.app import create_app
from careos.bootstrap import Container, ProviderRegistry, build_container
from careos.core.config import Settings
from careos.modules.identity.rbac import Role
from tests.factories import TEST_PASSWORD, ClientFactory, Tenant, sos_event
from tests.integration.helpers import GATEWAY_HEADER, audit_actions, raise_sos

ORIGIN = {"origin": "http://localhost:3000"}


@pytest.fixture
def sync_client(
    settings: Settings, providers: ProviderRegistry, container: Container
) -> Iterator[TestClient]:
    """TestClient runs the app on its own event loop, so it gets its own NullPool engine."""
    engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
    isolated = build_container(settings, with_hub=True, providers=providers, engine=engine)
    with TestClient(create_app(settings, isolated), base_url="http://testserver") as client:
        yield client


def test_operator_socket_receives_incident_created_without_personal_data(
    sync_client: TestClient, tenant: Tenant
) -> None:
    login = sync_client.post(
        "/v1/auth/login", json={"email": tenant.emails[Role.OPERATOR], "password": TEST_PASSWORD}
    )
    assert login.status_code == 200
    with sync_client.websocket_connect("/v1/ws", headers=ORIGIN) as socket:
        assert socket.receive_json() == {"type": "connection.ready"}
        response = sync_client.post(
            "/v1/gateway/careos/events",
            json=sos_event(tenant.device_external_id),
            headers={GATEWAY_HEADER: tenant.gateway_key},
        )
        assert response.status_code == 202
        message = socket.receive_json()
        assert message["type"] == "incident.created"
        assert message["organisation_id"] == str(tenant.organisation_id)
        assert message["incident_id"] == response.json()["incident_id"]
        assert message["payload"]["priority"] == "CRITICAL"
        assert "Margaret" not in json.dumps(message)
        assert socket.receive_json()["type"] == "device.updated"


def test_socket_requires_session_and_allowed_origin(
    sync_client: TestClient, tenant: Tenant
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as unauthenticated,
        sync_client.websocket_connect("/v1/ws", headers=ORIGIN),
    ):
        pass
    assert unauthenticated.value.code == 4401

    sync_client.post(
        "/v1/auth/login", json={"email": tenant.emails[Role.OPERATOR], "password": TEST_PASSWORD}
    )
    with (
        pytest.raises(WebSocketDisconnect) as hijack,
        sync_client.websocket_connect("/v1/ws", headers={"origin": "https://evil.example"}),
    ):
        pass
    assert hijack.value.code == 1008


async def test_health_and_readiness(client_factory: ClientFactory) -> None:
    client = await client_factory()
    assert (await client.get("/health")).json() == {"status": "ok"}
    ready = await client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "database": "ok", "redis": "not_configured"}
    assert (await client.get("/metrics")).status_code == 200


async def test_security_headers_and_request_correlation(client_factory: ClientFactory) -> None:
    client = await client_factory()
    response = await client.get("/v1/auth/me", headers={"X-Request-ID": "upstream-req-12345"})
    assert response.status_code == 401
    assert response.headers["x-request-id"] == "upstream-req-12345"
    assert response.json()["error"]["request_id"] == "upstream-req-12345"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    spoofed = await client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert spoofed.headers["x-request-id"] != "bad id\nwith newline"


async def test_oversized_body_is_rejected(client_factory: ClientFactory, tenant: Tenant) -> None:
    client = await client_factory()
    response = await client.post(
        "/v1/gateway/careos/events",
        content=b"{" + b" " * 300_000 + b"}",
        headers={GATEWAY_HEADER: tenant.gateway_key, "content-type": "application/json"},
    )
    assert response.status_code == 413


async def test_administrative_changes_are_audited(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    manager = await client_factory(tenant.emails[Role.CARE_MANAGER])

    contact = await manager.post(
        f"/v1/service-users/{tenant.service_user_id}/contacts",
        json={
            "full_name": "Neighbour Ann",
            "relationship": "Neighbour",
            "phone_number": "+44 7700 900777",
            "priority": 3,
        },
    )
    assert contact.status_code == 201
    duplicate_priority = await manager.post(
        f"/v1/service-users/{tenant.service_user_id}/contacts",
        json={
            "full_name": "Other",
            "relationship": "Friend",
            "phone_number": "+44 7700 900778",
            "priority": 3,
        },
    )
    assert duplicate_priority.status_code == 409
    updated = await manager.put(
        f"/v1/service-users/{tenant.service_user_id}/contacts/{contact.json()['id']}",
        json={
            "full_name": "Neighbour Ann",
            "relationship": "Neighbour",
            "phone_number": "+44 7700 900779",
            "priority": 3,
        },
    )
    assert updated.status_code == 200

    device = await manager.post(
        "/v1/devices",
        json={
            "external_id": "WATCH-77",
            "device_type": "SMARTWATCH",
            "manufacturer": "Acme",
            "service_user_id": str(tenant.service_user_id),
        },
    )
    assert device.status_code == 201 and device.json()["connection"]["status"] == "UNKNOWN"

    policy = (await manager.get("/v1/escalation-policies")).json()[0]
    unsafe = await manager.put(
        f"/v1/escalation-policies/{policy['id']}",
        json={
            "name": "No humans",
            "steps": [{"delay_seconds": 0, "action_type": "AUTOMATED_USER_CONTACT"}],
        },
    )
    assert unsafe.status_code == 422
    changed = await manager.put(
        f"/v1/escalation-policies/{policy['id']}",
        json={
            "name": "Faster escalation",
            "is_default": True,
            "steps": [
                {"delay_seconds": 0, "action_type": "CALL_TRUSTED_CONTACT", "contact_priority": 1},
                {"delay_seconds": 20, "action_type": "OPERATOR_ESCALATION"},
            ],
        },
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 2

    profile = await manager.get(f"/v1/service-users/{tenant.service_user_id}")
    assert profile.status_code == 200
    assert [c["full_name"] for c in profile.json()["contacts"]][:2] == [
        "Sarah Wilson",
        "James Wilson",
    ]

    actions = set(await audit_actions(container.session_factory))
    assert {
        "TRUSTED_CONTACT_CREATED",
        "TRUSTED_CONTACT_UPDATED",
        "DEVICE_ADDED",
        "ESCALATION_POLICY_CHANGED",
        "SERVICE_USER_VIEWED",
    } <= actions

    gateway = await client_factory()
    incident = await raise_sos(gateway, tenant)
    async with container.session_factory() as session:
        from sqlalchemy import select

        from careos.modules.escalation_engine.models import ScheduledAction

        steps = (
            await session.scalars(
                select(ScheduledAction.action_type).where(
                    ScheduledAction.incident_id == incident["incident_id"]
                )
            )
        ).all()
    assert [s.value for s in steps] == ["CALL_TRUSTED_CONTACT", "OPERATOR_ESCALATION"]
