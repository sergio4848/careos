"""Observability: safety events are counted and logged with correlation identifiers, and logs
never contain secrets or service-user personal data."""

from __future__ import annotations

import io
import json
import logging
import uuid
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from careos.bootstrap import Container, ProviderRegistry
from careos.core.logging import build_formatter, configure_logging, get_logger
from careos.modules.ai_orchestrator.orchestrator import AIOrchestrator
from careos.modules.identity.models import User
from careos.modules.identity.rbac import Role
from careos.modules.notification_engine.providers import MockVoiceProvider
from tests.factories import TEST_PASSWORD, ClientFactory, Tenant, sos_event
from tests.integration.helpers import (
    GATEWAY_HEADER,
    at,
    load_incident,
    make_executor,
    metric,
    raise_sos,
    send_event,
)

REQUIRED_METRICS = (
    "careos_sos_received_total",
    "careos_incidents_created_total",
    "careos_gateway_duplicate_events_total",
    "careos_gateway_rejections_total",
    "careos_incident_takeovers_total",
    "careos_incident_resolutions_total",
    "careos_provider_failures_total",
    "careos_ai_timeouts_total",
    "careos_realtime_delivery_failures_total",
    "careos_escalation_failures_total",
)


class Deltas:
    """Measure how much Prometheus samples moved during a block of work."""

    def __init__(self, *samples: tuple[str, dict[str, str]]) -> None:
        self._samples = samples
        self._before = [metric(name, **labels) for name, labels in samples]

    def __call__(self) -> list[float]:
        return [
            metric(name, **labels) - before
            for (name, labels), before in zip(self._samples, self._before, strict=True)
        ]


async def test_safety_metrics_are_counted_and_exposed(
    client_factory: ClientFactory, tenant: Tenant
) -> None:
    rejections = "careos_gateway_rejections_total"
    deltas = Deltas(
        ("careos_sos_received_total", {"adapter": "careos"}),
        ("careos_incidents_created_total", {"trigger_type": "SOS_BUTTON", "priority": "CRITICAL"}),
        (
            "careos_gateway_duplicate_events_total",
            {"adapter": "careos", "event_type": "SOS_BUTTON"},
        ),
        (rejections, {"adapter": "careos", "reason": "event_id_conflict"}),
        (rejections, {"adapter": "careos", "reason": "invalid_credential"}),
        (rejections, {"adapter": "careos", "reason": "unknown_device"}),
        (rejections, {"adapter": "careos", "reason": "invalid_payload"}),
        ("careos_incident_takeovers_total", {"result": "assigned"}),
        ("careos_incident_takeovers_total", {"result": "conflict"}),
        ("careos_incident_resolutions_total", {"category": "USER_SAFE"}),
    )
    gateway = await client_factory()
    event = sos_event(tenant.device_external_id)
    accepted = await send_event(gateway, tenant, event)
    incident_id = accepted.json()["incident_id"]
    await send_event(gateway, tenant, event)  # redelivery
    await send_event(gateway, tenant, {**event, "device": {"battery": 1, "signal": 1}})  # conflict
    await gateway.post(
        "/v1/gateway/careos/events",
        json=sos_event(tenant.device_external_id),
        headers={GATEWAY_HEADER: "cgk_demo0001_wrong-secret-value-000000000"},
    )
    await send_event(gateway, tenant, sos_event("DEV-UNKNOWN"))
    await send_event(gateway, tenant, {"event_type": "SOS_BUTTON"})

    first = await client_factory(tenant.emails[Role.OPERATOR])
    second = await client_factory(tenant.second_operator_email)
    assert (await first.post(f"/v1/incidents/{incident_id}/takeover")).status_code == 200
    assert (await second.post(f"/v1/incidents/{incident_id}/takeover")).status_code == 409
    await first.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})

    # SOS received counts every authenticated, well-formed SOS (redelivery, conflict and the
    # unknown device included); the bad credential and malformed payload never get that far.
    assert deltas() == [4, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    exposed = (await gateway.get("/metrics")).text
    for name in REQUIRED_METRICS:
        assert f"# TYPE {name} counter" in exposed, name


def structured(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("event") == event
    ]


async def test_logs_carry_correlation_and_safety_identifiers(
    client_factory: ClientFactory,
    tenant: Tenant,
    container: Container,
    providers: ProviderRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    gateway = await client_factory()
    event = sos_event(tenant.device_external_id)
    response = await gateway.post(
        "/v1/gateway/careos/events",
        json=event,
        headers={GATEWAY_HEADER: tenant.gateway_key, "X-Request-ID": "req-sos-000000001"},
    )
    incident_id = response.json()["incident_id"]
    await send_event(gateway, tenant, sos_event("DEV-UNKNOWN"))
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    await operator.post(
        f"/v1/incidents/{incident_id}/takeover", headers={"X-Request-ID": "req-takeover-0001"}
    )

    identifiers = {"organisation_id": str(tenant.organisation_id), "incident_id": incident_id}
    (accepted,) = structured(caplog, "gateway.event_accepted")
    assert accepted | identifiers == accepted
    assert accepted["request_id"] == "req-sos-000000001"
    assert accepted["event_id"] == event["event_id"] and accepted["adapter"] == "careos"
    (created,) = structured(caplog, "incident.created")
    assert created | identifiers == created and created["event_id"] == event["event_id"]
    assert created["request_id"] == "req-sos-000000001"
    (taken,) = structured(caplog, "incident.taken_over")
    assert taken | identifiers == taken and taken["request_id"] == "req-takeover-0001"
    (rejected,) = structured(caplog, "gateway.rejected")
    assert rejected["failure_category"] == "unknown_device"
    assert rejected["organisation_id"] == str(tenant.organisation_id)

    # Provider failures name the provider and category, with the incident and tenant.
    providers.voice = MockVoiceProvider("failure")
    providers.ai = AIOrchestrator(None, timeout_seconds=1)
    await operator.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})
    fresh_id = (await raise_sos(gateway, tenant))["incident_id"]
    fresh = await load_incident(container, fresh_id)
    await make_executor(container, providers).run_due(at(fresh, 1))
    failures = [
        f for f in structured(caplog, "escalation.provider_failed") if f["incident_id"] == fresh_id
    ]
    assert failures, "provider failure was not logged"
    assert failures[0]["provider"] == "mock-voice"
    assert failures[0]["provider_kind"] == "voice"
    assert failures[0]["failure_category"] == "provider_error"
    assert failures[0]["organisation_id"] == str(tenant.organisation_id)


async def test_logs_never_contain_secrets_or_service_user_personal_data(
    client_factory: ClientFactory,
    tenant: Tenant,
    container: Container,
    providers: ProviderRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    anonymous = await client_factory()
    await anonymous.post(
        "/v1/auth/login", json={"email": tenant.emails[Role.OPERATOR], "password": "Wrong-Pass-1"}
    )
    login = await anonymous.post(
        "/v1/auth/login", json={"email": tenant.emails[Role.OPERATOR], "password": TEST_PASSWORD}
    )
    csrf = login.json()["csrf_token"]
    session_cookie = next(iter(anonymous.cookies.values()))
    anonymous.headers["X-CSRF-Token"] = csrf
    incident_id = (await raise_sos(anonymous, tenant))["incident_id"]
    await anonymous.get(f"/v1/incidents/{incident_id}")
    await anonymous.get(f"/v1/service-users/{tenant.service_user_id}")
    await anonymous.post(
        f"/v1/incidents/{incident_id}/resolve",
        json={"category": "FAMILY_RESPONDED", "notes": "Sarah Wilson found Margaret on the floor"},
    )
    providers.voice = MockVoiceProvider("failure")
    fresh_id = (await raise_sos(anonymous, tenant))["incident_id"]
    await make_executor(container, providers).run_due(
        at(await load_incident(container, fresh_id), 1)
    )

    logged = caplog.text + json.dumps([r.msg for r in caplog.records], default=str)
    assert "incident.created" in logged  # the capture really saw application logs
    forbidden = [
        TEST_PASSWORD,
        "Wrong-Pass-1",
        tenant.gateway_key,
        tenant.gateway_key.split("_", 2)[2],  # the secret part after the public prefix
        session_cookie,
        csrf,
        tenant.emails[Role.OPERATOR],
        "Margaret",
        "Wilson",
        "+44 20 7946 0018",
        "+44 7700 900123",
        "found Margaret on the floor",
    ]
    assert [value for value in forbidden if value in logged] == []


async def test_database_errors_are_logged_without_row_values(container: Container) -> None:
    email = f"duplicate.person.{uuid.uuid4().hex[:6]}@example.com"
    async with container.session_factory() as session:
        for _ in range(2):
            session.add(
                User(
                    organisation_id=None,
                    email=email,
                    full_name="Pat Person",
                    role=Role.PLATFORM_ADMIN,
                    password_hash="x",
                )
            )
        with pytest.raises(IntegrityError) as caught:
            await session.commit()
    assert email in str(caught.value)  # the raw driver error does contain the value...

    # Render through the production pipeline (structlog processors + formatter).
    configure_logging("INFO", json_logs=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_formatter(json_logs=True))
    stdlib_logger = logging.getLogger("careos.tests.database_errors")
    stdlib_logger.addHandler(handler)
    stdlib_logger.propagate = False
    try:
        try:
            raise caught.value
        except IntegrityError:
            get_logger("careos.tests.database_errors").exception("request.unhandled_error")
    finally:
        stdlib_logger.removeHandler(handler)
    rendered = stream.getvalue()
    assert "IntegrityError" in rendered
    assert email not in rendered and "Pat Person" not in rendered  # ...the log line does not
