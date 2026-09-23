"""Signed Twilio webhooks: authentication, idempotency, replay and tenant safety."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import select, update

from careos.bootstrap import Container
from careos.core.time import utcnow
from careos.modules.escalation_engine.models import (
    EscalationActionType,
    ScheduledAction,
    ScheduledActionStatus,
)
from careos.modules.identity.rbac import Role
from careos.modules.incident_engine.models import Incident, IncidentEvent
from careos.modules.notification_engine.models import (
    Call,
    CallStatus,
    CallTargetType,
    StructuredCallResponse,
)
from careos.modules.telephony.models import CallEvent, CallEventType
from careos.modules.telephony.security import compute_twilio_signature
from tests.factories import ClientFactory, Tenant
from tests.integration.helpers import asgi_client, count, metric, raise_sos

AUTH_TOKEN = "twilio-auth-token-test"
SID = "CA" + "a" * 32


def webhook_container(container: Container) -> Container:
    settings = container.settings.model_copy(update={"twilio_auth_token": SecretStr(AUTH_TOKEN)})
    return dataclasses.replace(container, settings=settings)


async def create_call(
    container: Container,
    tenant: Tenant,
    incident_id: str,
    *,
    sid: str | None = SID,
    target: CallTargetType = CallTargetType.SERVICE_USER,
) -> uuid.UUID:
    call_id = uuid.uuid4()
    async with container.session_factory() as session:
        session.add(
            Call(
                id=call_id,
                organisation_id=tenant.organisation_id,
                incident_id=uuid.UUID(incident_id),
                target_type=target,
                service_user_id=tenant.service_user_id if target.name == "SERVICE_USER" else None,
                provider="twilio",
                provider_call_sid=sid,
                status=CallStatus.QUEUED,
                started_at=utcnow(),
                to_number_masked="+44*******123",
            )
        )
        await session.commit()
    return call_id


def sign(path_and_query: str, form: dict[str, str]) -> dict[str, str]:
    url = f"http://testserver{path_and_query}"
    return {"X-Twilio-Signature": compute_twilio_signature(AUTH_TOKEN, url, form)}


async def post_status(
    client: httpx.AsyncClient,
    call_id: uuid.UUID,
    form: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    path = f"/v1/providers/twilio/voice/status?call={call_id}"
    return await client.post(
        path, data=form, headers=sign(path, form) if headers is None else headers
    )


def status_form(status: str, sequence: str = "0", **extra: str) -> dict[str, str]:
    return {"CallSid": SID, "CallStatus": status, "SequenceNumber": sequence, **extra}


async def call_row(container: Container, call_id: uuid.UUID) -> Call:
    async with container.session_factory() as session:
        call = await session.get(Call, call_id)
        assert call is not None
        return call


async def event_count(container: Container, call_id: uuid.UUID) -> int:
    return await count(container.session_factory, CallEvent, CallEvent.call_id == call_id)


async def test_signed_lifecycle_callbacks_advance_the_call(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id)
    async with asgi_client(webhook_container(container)) as client:
        for i, twilio_status in enumerate(["initiated", "ringing", "in-progress"]):
            response = await post_status(client, call_id, status_form(twilio_status, str(i)))
            assert response.status_code == 204, response.text
        done = await post_status(client, call_id, status_form("completed", "3", CallDuration="42"))
    assert done.status_code == 204
    call = await call_row(container, call_id)
    assert call.status is CallStatus.COMPLETED
    assert call.answered_at is not None and call.ended_at is not None
    assert call.duration_seconds == 42
    async with container.session_factory() as session:
        types = list(
            await session.scalars(
                select(CallEvent.event_type)
                .where(CallEvent.call_id == call_id)
                .order_by(CallEvent.created_at)
            )
        )
    assert types == [
        CallEventType.CALL_INITIATED,
        CallEventType.CALL_RINGING,
        CallEventType.CALL_ANSWERED,
        CallEventType.CALL_ENDED,
    ]


async def test_unsigned_and_mis_signed_requests_are_rejected(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id)
    missing_before = metric("careos_twilio_webhook_rejections_total", reason="missing_signature")
    bad_before = metric("careos_twilio_webhook_rejections_total", reason="bad_signature")
    async with asgi_client(webhook_container(container)) as client:
        unsigned = await post_status(client, call_id, status_form("ringing"), headers={})
        bad = await post_status(
            client, call_id, status_form("ringing"), headers={"X-Twilio-Signature": "AAAA"}
        )
        # A signature over different form values must also fail.
        forged = await client.post(
            f"/v1/providers/twilio/voice/status?call={call_id}",
            data=status_form("completed"),
            headers=sign(
                f"/v1/providers/twilio/voice/status?call={call_id}", status_form("ringing")
            ),
        )
    assert unsigned.status_code == 403 and bad.status_code == 403 and forged.status_code == 403
    assert (await call_row(container, call_id)).status is CallStatus.QUEUED
    assert await event_count(container, call_id) == 0
    assert (
        metric("careos_twilio_webhook_rejections_total", reason="missing_signature")
        > missing_before
    )
    assert metric("careos_twilio_webhook_rejections_total", reason="bad_signature") > bad_before


async def test_webhooks_unavailable_when_twilio_is_not_configured(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id)
    client = await client_factory()  # default settings: no auth token configured
    response = await post_status(client, call_id, status_form("ringing"))
    assert response.status_code == 503


async def test_unknown_call_and_sid_mismatch_are_rejected(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id, sid="CA" + "b" * 32)
    async with asgi_client(webhook_container(container)) as client:
        unknown = await post_status(client, uuid.uuid4(), status_form("ringing"))
        mismatched = await post_status(client, call_id, status_form("ringing"))  # SID differs
    assert unknown.status_code == 404
    assert mismatched.status_code == 403
    assert (await call_row(container, call_id)).status is CallStatus.QUEUED


async def test_duplicate_callbacks_produce_one_logical_event(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id)
    form = status_form("in-progress", "2")
    async with asgi_client(webhook_container(container)) as client:
        responses = await asyncio.gather(*(post_status(client, call_id, form) for _ in range(6)))
    assert {r.status_code for r in responses} == {204}
    assert (
        await count(
            container.session_factory,
            CallEvent,
            CallEvent.call_id == call_id,
            CallEvent.event_type == CallEventType.CALL_ANSWERED,
        )
        == 1
    )
    call = await call_row(container, call_id)
    assert call.status is CallStatus.IN_PROGRESS and call.answered_at is not None


async def test_late_and_out_of_order_callbacks_never_move_a_call_backwards(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id)
    async with asgi_client(webhook_container(container)) as client:
        assert (
            await post_status(client, call_id, status_form("completed", "5", CallDuration="30"))
        ).status_code == 204
        late = await post_status(client, call_id, status_form("ringing", "1"))
    assert late.status_code == 204  # acknowledged so Twilio stops retrying, but ignored
    call = await call_row(container, call_id)
    assert call.status is CallStatus.COMPLETED and call.duration_seconds == 30


async def test_cross_tenant_callback_cannot_touch_another_organisations_call(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    victim_incident = (await raise_sos(gateway, tenant))["incident_id"]
    victim_call = await create_call(container, tenant, victim_incident)
    async with asgi_client(webhook_container(container)) as client:
        # An attacker who somehow learned the victim's call id still cannot speak for it:
        # the CallSid bound to the call must match, and org/incident come from the call row —
        # attacker-supplied identifiers in the form are ignored entirely.
        attack_form = {
            "CallSid": "CA" + "e" * 32,
            "CallStatus": "completed",
            "SequenceNumber": "0",
            "organisation_id": str(other_tenant.organisation_id),
            "incident_id": str(uuid.uuid4()),
        }
        path = f"/v1/providers/twilio/voice/status?call={victim_call}"
        response = await client.post(path, data=attack_form, headers=sign(path, attack_form))
    assert response.status_code == 403
    call = await call_row(container, victim_call)
    assert call.status is CallStatus.QUEUED
    assert call.organisation_id == tenant.organisation_id


async def test_gather_records_structured_response_idempotently(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(
        container, tenant, incident_id, target=CallTargetType.TRUSTED_CONTACT
    )
    path = f"/v1/providers/twilio/voice/gather?call={call_id}"
    form = {"CallSid": SID, "Digits": "1"}
    async with asgi_client(webhook_container(container)) as client:
        responses = await asyncio.gather(
            *(client.post(path, data=form, headers=sign(path, form)) for _ in range(4))
        )
    assert {r.status_code for r in responses} == {200}
    assert "text/xml" in responses[0].headers["content-type"]
    call = await call_row(container, call_id)
    assert call.structured_response is StructuredCallResponse.CAN_RESPOND
    assert call.acknowledged is True
    assert (
        await count(
            container.session_factory,
            CallEvent,
            CallEvent.call_id == call_id,
            CallEvent.event_type == CallEventType.USER_RESPONSE_RECEIVED,
        )
        == 1
    )


async def test_gather_request_operator_schedules_an_immediate_operator_alert(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(
        container, tenant, incident_id, target=CallTargetType.TRUSTED_CONTACT
    )
    path = f"/v1/providers/twilio/voice/gather?call={call_id}"
    form = {"CallSid": SID, "Digits": "3"}
    async with asgi_client(webhook_container(container)) as client:
        assert (await client.post(path, data=form, headers=sign(path, form))).status_code == 200
    call = await call_row(container, call_id)
    assert call.structured_response is StructuredCallResponse.REQUEST_OPERATOR
    assert call.acknowledged is False  # asking for an operator is not "I am safe"
    async with container.session_factory() as session:
        due_now = await session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.incident_id == uuid.UUID(incident_id),
                ScheduledAction.action_type == EscalationActionType.OPERATOR_ESCALATION,
                ScheduledAction.status == ScheduledActionStatus.PENDING,
                ScheduledAction.due_at <= utcnow(),
            )
        )
        timeline: list[Any] = list(
            await session.scalars(
                select(IncidentEvent.event_type).where(
                    IncidentEvent.incident_id == uuid.UUID(incident_id)
                )
            )
        )
    assert due_now is not None, "operator alert must be pulled forward"
    assert any(t.value == "OPERATOR_ESCALATION_REQUESTED" for t in timeline)


async def test_callback_arriving_after_manual_resolution_changes_only_the_call(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id)
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    resolved = await operator.post(
        f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"}
    )
    assert resolved.status_code == 200
    async with asgi_client(webhook_container(container)) as client:
        response = await post_status(
            client, call_id, status_form("completed", "1", CallDuration="12")
        )
    assert response.status_code == 204
    call = await call_row(container, call_id)
    assert call.status is CallStatus.COMPLETED
    async with container.session_factory() as session:
        incident = await session.get(Incident, uuid.UUID(incident_id))
        assert incident is not None
    assert incident.status.value == "RESOLVED"  # the late callback cannot reopen or close it
    assert incident.resolved_at is not None and incident.closed_at is None


async def test_crash_window_recovery_binds_the_sid_from_the_first_callback(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    """ADR-017: a crash between the Twilio accept and the SID persist leaves a call row
    without a SID; the callback URL's opaque call id re-links them."""
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    call_id = await create_call(container, tenant, incident_id, sid=None)
    async with asgi_client(webhook_container(container)) as client:
        assert (await post_status(client, call_id, status_form("ringing"))).status_code == 204
        # ...and from then on other SIDs are rejected for this call.
        other = status_form("completed")
        other["CallSid"] = "CA" + "f" * 32
        rejected = await post_status(client, call_id, other)
    call = await call_row(container, call_id)
    assert call.provider_call_sid == SID and call.status is CallStatus.RINGING
    assert rejected.status_code == 403


async def test_scheduled_action_attempt_uniqueness_is_database_enforced(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    """The provider-idempotency key (scheduled_action_id, attempt) is a real constraint."""
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    async with container.session_factory() as session:
        action_id = await session.scalar(
            select(ScheduledAction.id)
            .where(ScheduledAction.incident_id == uuid.UUID(incident_id))
            .order_by(ScheduledAction.step_order)
            .limit(1)
        )
        await session.execute(
            update(ScheduledAction).where(ScheduledAction.id == action_id).values(attempts=1)
        )
        await session.commit()
    import pytest
    from sqlalchemy.exc import IntegrityError

    for should_fail in (False, True):
        async with container.session_factory() as session:
            session.add(
                Call(
                    id=uuid.uuid4(),
                    organisation_id=tenant.organisation_id,
                    incident_id=uuid.UUID(incident_id),
                    scheduled_action_id=action_id,
                    attempt=1,
                    target_type=CallTargetType.SERVICE_USER,
                    service_user_id=tenant.service_user_id,
                    provider="twilio",
                    status=CallStatus.QUEUED,
                    started_at=utcnow(),
                )
            )
            if should_fail:
                with pytest.raises(IntegrityError, match="uq_calls_scheduled_action_attempt"):
                    await session.commit()
            else:
                await session.commit()
