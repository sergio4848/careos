"""End-to-end voice escalation: SOS → durable step → (fake) Twilio call → webhook-fed state
→ AI advisory → deterministic escalation → operator control → manual resolution.

Twilio is a scripted in-process HTTP transport; call progress is injected through the same
store the signed webhooks use. No real network, no real credentials.
"""

from __future__ import annotations

import asyncio
import datetime
import re
import uuid

import httpx
from pydantic import SecretStr
from sqlalchemy import select, update

from careos.bootstrap import Container
from careos.core.config import Settings
from careos.core.time import utcnow
from careos.modules.ai_orchestrator.models import AISession, UrgencySignal
from careos.modules.ai_orchestrator.orchestrator import AIOrchestrator
from careos.modules.ai_orchestrator.voice import VoiceResult
from careos.modules.escalation_engine.executor import EscalationExecutor
from careos.modules.escalation_engine.models import (
    EscalationActionType,
    ScheduledAction,
    ScheduledActionStatus,
)
from careos.modules.identity.rbac import Role
from careos.modules.incident_engine.models import Incident
from careos.modules.notification_engine.models import (
    Call,
    CallStatus,
    CallTargetType,
    StructuredCallResponse,
)
from careos.modules.notification_engine.providers import MockNotificationProvider
from careos.modules.service_users.models import ServiceUser, TrustedContact
from careos.modules.telephony.provider import TwilioVoiceProvider
from careos.modules.telephony.store import CallControlStore, MediaContext
from tests.factories import ClientFactory, Tenant
from tests.integration.helpers import audit_actions, count, event_types, metric, raise_sos

SERVICE_USER_NUMBER = "+442079460018"
CONTACT_1_NUMBER = "+447700900123"
CONTACT_2_NUMBER = "+447700900456"
ALLOWLIST = f"{SERVICE_USER_NUMBER},{CONTACT_1_NUMBER},{CONTACT_2_NUMBER}"


class FakeTwilio:
    """Accepts call-creation and hangup requests; hands out SIDs; captures TwiML."""

    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.hangups: list[str] = []
        self.first_request = asyncio.Event()

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            if request.url.path.endswith("/Calls.json"):
                sid = "CA" + uuid.uuid4().hex
                self.created.append({**form, "sid": sid})
                self.first_request.set()
                return httpx.Response(201, json={"sid": sid})
            self.hangups.append(request.url.path.rsplit("/", 1)[-1].removesuffix(".json"))
            return httpx.Response(200, json={})

        return httpx.MockTransport(handler)

    def media_token(self, index: int = -1) -> str:
        match = re.search(r'name="token" value="([^"]+)"', self.created[index]["Twiml"])
        assert match, "welfare TwiML must carry a media token"
        return match.group(1)

    def sid(self, index: int = -1) -> str:
        return self.created[index]["sid"]


def live_settings(container: Container) -> Settings:
    return container.settings.model_copy(
        update={
            "voice_provider": "twilio",
            "real_telephony_enabled": True,
            "twilio_account_sid": "AC" + "0" * 32,
            "twilio_auth_token": SecretStr("twilio-auth-token-test"),
            "twilio_from_number": "+441134960000",
            "twilio_webhook_base_url": "https://careos.example",
            "telephony_allowed_numbers": ALLOWLIST.split(","),
            "twilio_ring_timeout_seconds": 5,
            "voice_call_max_duration_seconds": 10,
            "voice_call_poll_interval_seconds": 0.02,
        }
    )


class VoiceRig:
    """Executor + Twilio provider + store wired against the test container."""

    def __init__(self, container: Container, settings: Settings | None = None) -> None:
        self.container = container
        self.settings = settings or live_settings(container)
        self.twilio = FakeTwilio()
        self.store = CallControlStore(
            container.session_factory, container.realtime, publish_timeout_seconds=1
        )
        self.provider = TwilioVoiceProvider(
            self.settings, self.store, transport=self.twilio.transport()
        )
        self.executor = EscalationExecutor(
            settings=self.settings,
            session_factory=container.session_factory,
            publisher=container.realtime,
            voice=self.provider,
            notifications=MockNotificationProvider(),
            ai=AIOrchestrator(None, timeout_seconds=1),
            worker_id=f"voice-rig-{uuid.uuid4().hex[:6]}",
        )

    async def call_for_action(self, incident_id: str, step_order: int) -> Call:
        async with self.container.session_factory() as session:
            call = await session.scalar(
                select(Call)
                .join(ScheduledAction, ScheduledAction.id == Call.scheduled_action_id)
                .where(
                    Call.incident_id == uuid.UUID(incident_id),
                    ScheduledAction.step_order == step_order,
                )
                .order_by(Call.started_at.desc())
            )
            assert call is not None
            return call

    async def wait_for_dial(self) -> None:
        async with asyncio.timeout(5):
            await self.twilio.first_request.wait()
        self.twilio.first_request.clear()

    async def progress(self, call_id: uuid.UUID, *statuses: str, duration: str | None = None):
        for i, status in enumerate(statuses):
            applied = await self.store.apply_provider_status(
                call_id,
                provider_sid=self.twilio.sid(),
                twilio_status=status,
                sequence=str(i),
                call_duration=duration if status == "completed" else None,
                error_code=None,
            )
            assert applied.accepted, applied.reason


async def use_e164_numbers(container: Container, tenant: Tenant) -> None:
    async with container.session_factory() as session:
        await session.execute(
            update(ServiceUser)
            .where(ServiceUser.id == tenant.service_user_id)
            .values(phone_number=SERVICE_USER_NUMBER)
        )
        await session.execute(
            update(TrustedContact)
            .where(TrustedContact.organisation_id == tenant.organisation_id)
            .values(phone_number=CONTACT_1_NUMBER)
        )
        await session.commit()


async def incident_row(container: Container, incident_id: str) -> Incident:
    async with container.session_factory() as session:
        incident = await session.get(Incident, uuid.UUID(incident_id))
        assert incident is not None
        return incident


async def media_context(container: Container, tenant: Tenant, call: Call) -> MediaContext:
    return MediaContext(
        organisation_id=tenant.organisation_id,
        incident_id=call.incident_id,
        call_id=call.id,
        preferred_name="Margaret",
        language="en-GB",
        trigger_type="SOS_BUTTON",
    )


async def test_full_acceptance_flow_sos_to_manual_resolution(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    """Sprint 2 acceptance demo, end to end (§36) with a scripted Twilio and AI."""
    await use_e164_numbers(container, tenant)
    rig = VoiceRig(container)
    started_before = metric(
        "careos_calls_started_total", provider="twilio", target_type="service_user"
    )
    answered_before = metric("careos_calls_answered_total", provider="twilio")

    # 1-3: SOS → CRITICAL incident → due ScheduledAction.
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await incident_row(container, incident_id)
    assert incident.priority.value == "CRITICAL"

    # 4-6: the worker requests a real outbound call; Twilio "answers".
    run = asyncio.create_task(
        rig.executor.run_due(incident.created_at + datetime.timedelta(seconds=1))
    )
    await rig.wait_for_dial()
    call = await rig.call_for_action(incident_id, step_order=1)
    assert call.status in (CallStatus.QUEUED, CallStatus.INITIATED)
    assert call.to_number_masked and SERVICE_USER_NUMBER not in (call.to_number_masked or "")
    await rig.progress(call.id, "initiated", "ringing", "in-progress")

    # 7-11: media stream connects; the AI discloses itself; Margaret asks for help; the
    # structured advisory is persisted. (The bridge's store calls, driven directly.)
    token = rig.twilio.media_token()
    context = await rig.store.attach_media_stream(token)
    assert context is not None and context.preferred_name == "Margaret"
    ai_session_id = await rig.store.start_ai_session(context, provider="mock-ai-voice")
    await rig.store.finish_ai_session(
        ai_session_id,
        context,
        result=VoiceResult(
            contact_established=True,
            requested_human_help=True,
            urgency_signal=UrgencySignal.ASSISTANCE_REQUESTED,
            language="en-GB",
            summary="Margaret answered and requested assistance.",
        ),
    )
    await rig.progress(call.id, "completed", duration="35")
    assert await asyncio.wait_for(run, timeout=10) == 1

    # 12: incident remains active; the person's explicit request acknowledged it.
    incident = await incident_row(container, incident_id)
    assert incident.status.value == "ACKNOWLEDGED" and incident.is_active
    assert incident.resolved_at is None and incident.closed_at is None
    assert incident.priority.value == "CRITICAL"  # advisory changed nothing

    async with container.session_factory() as session:
        ai_row = await session.get(AISession, ai_session_id)
        assert ai_row is not None
    assert ai_row.urgency_signal is UrgencySignal.ASSISTANCE_REQUESTED
    assert ai_row.advisory_summary == "Margaret answered and requested assistance."

    # 13: the operator console sees the call and the clearly-labelled advisory.
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    calls_view = (await operator.get(f"/v1/incidents/{incident_id}/calls")).json()
    assert len(calls_view) == 1
    view = calls_view[0]
    assert view["status"] == "COMPLETED" and view["duration_seconds"] == 35
    assert view["target_label"] == "Service user"
    assert view["to_number_masked"].startswith("+44") and "*" in view["to_number_masked"]
    assert view["ai"]["urgency_signal"] == "ASSISTANCE_REQUESTED"
    assert view["ai"]["disclaimer"] == "AI ADVISORY — HUMAN REVIEW REQUIRED"

    # 14: TAKE OVER stops future automated escalation.
    takeover = await operator.post(f"/v1/incidents/{incident_id}/takeover")
    assert takeover.status_code == 200
    later = incident.created_at + datetime.timedelta(seconds=600)
    assert await rig.executor.run_due(later) <= 1  # only the operator step may remain/run
    remaining_contact_calls = [c for c in rig.twilio.created if c["To"] != SERVICE_USER_NUMBER]
    assert remaining_contact_calls == []  # no trusted-contact call after takeover

    # 15-16: manual resolution by the human, with the full audit trail.
    resolved = await operator.post(
        f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"}
    )
    assert resolved.status_code == 200
    types = await event_types(container.session_factory, incident_id)
    for required in (
        "SOS_RECEIVED",
        "INCIDENT_CREATED",
        "ESCALATION_SCHEDULED",
        "AUTOMATED_CALL_STARTED",
        "AI_CALL_STARTED",
        "AI_CALL_COMPLETED",
        "CONTACT_ACKNOWLEDGED",
        "OPERATOR_TAKEOVER",
        "INCIDENT_RESOLVED",
    ):
        assert required in types, required
    actions = set(await audit_actions(container.session_factory, resource_id=incident_id))
    assert {"INCIDENT_CREATED", "INCIDENT_TAKEOVER", "INCIDENT_RESOLVED"} <= actions
    assert (
        metric("careos_calls_started_total", provider="twilio", target_type="service_user")
        - started_before
        == 1
    )
    assert metric("careos_calls_answered_total", provider="twilio") - answered_before == 1


async def test_trusted_contact_flow_with_structured_keypad_response(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    await use_e164_numbers(container, tenant)
    rig = VoiceRig(container)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await incident_row(container, incident_id)
    at = lambda s: incident.created_at + datetime.timedelta(seconds=s)  # noqa: E731

    # T+0 welfare call: nobody answers.
    run = asyncio.create_task(rig.executor.run_due(at(1)))
    await rig.wait_for_dial()
    first_call = await rig.call_for_action(incident_id, step_order=1)
    await rig.progress(first_call.id, "initiated", "ringing", "no-answer")
    assert await asyncio.wait_for(run, timeout=10) == 1

    # T+30 trusted contact #1: deterministic Gather flow, minimum necessary info.
    run = asyncio.create_task(rig.executor.run_due(at(31)))
    await rig.wait_for_dial()
    created = rig.twilio.created[-1]
    assert created["To"] == CONTACT_1_NUMBER
    assert "<Gather" in created["Twiml"] and "Margaret Wilson" in created["Twiml"]
    assert "<Stream" not in created["Twiml"]  # no AI session on trusted-contact calls
    contact_call = await rig.call_for_action(incident_id, step_order=2)
    await rig.progress(contact_call.id, "initiated", "ringing", "in-progress")
    applied = await rig.store.apply_gather_response(
        contact_call.id, provider_sid=rig.twilio.sid(), digits="3"
    )
    assert applied.accepted
    await rig.progress(contact_call.id, "completed", duration="18")
    assert await asyncio.wait_for(run, timeout=10) == 1

    call = await rig.call_for_action(incident_id, step_order=2)
    assert call.structured_response is StructuredCallResponse.REQUEST_OPERATOR
    assert call.acknowledged is False
    # The person asked for an operator: the operator step is due immediately.
    async with container.session_factory() as session:
        operator_due = await session.scalar(
            select(ScheduledAction.due_at).where(
                ScheduledAction.incident_id == uuid.UUID(incident_id),
                ScheduledAction.action_type == EscalationActionType.OPERATOR_ESCALATION,
                ScheduledAction.status == ScheduledActionStatus.PENDING,
            )
        )
    assert operator_due is not None and operator_due <= utcnow()
    incident = await incident_row(container, incident_id)
    assert incident.is_active and incident.resolved_at is None  # nothing auto-resolved


async def test_provider_idempotency_prevents_a_second_dial_for_the_same_attempt(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    """ADR-017 crash window: a competing claim of the same attempt never dials twice."""
    await use_e164_numbers(container, tenant)
    rig = VoiceRig(container)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await incident_row(container, incident_id)

    # Simulate the crashed worker's phase 1 having already created the call row for
    # (action, attempt 1): the reclaiming worker must skip, not dial.
    async with container.session_factory() as session:
        action = await session.scalar(
            select(ScheduledAction)
            .where(ScheduledAction.incident_id == uuid.UUID(incident_id))
            .order_by(ScheduledAction.step_order)
            .limit(1)
        )
        assert action is not None
        session.add(
            Call(
                id=uuid.uuid4(),
                organisation_id=tenant.organisation_id,
                incident_id=uuid.UUID(incident_id),
                scheduled_action_id=action.id,
                attempt=1,
                target_type=CallTargetType.SERVICE_USER,
                service_user_id=tenant.service_user_id,
                provider="twilio",
                status=CallStatus.QUEUED,
                started_at=utcnow(),
            )
        )
        await session.commit()
    conflicts_before = metric(
        "careos_provider_idempotency_conflicts_total", kind="voice_call_attempt"
    )
    processed = await rig.executor.run_due(incident.created_at + datetime.timedelta(seconds=1))
    assert processed == 1  # claimed, then skipped as a duplicate attempt
    assert rig.twilio.created == []  # ZERO real dials for the duplicated attempt
    assert (
        metric("careos_provider_idempotency_conflicts_total", kind="voice_call_attempt")
        - conflicts_before
        == 1
    )
    assert (
        await count(
            container.session_factory,
            Call,
            Call.incident_id == uuid.UUID(incident_id),
        )
        == 1
    )


async def test_worker_retry_after_failed_dial_uses_a_new_attempt(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    await use_e164_numbers(container, tenant)
    rig = VoiceRig(container)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await incident_row(container, incident_id)
    at = lambda s: incident.created_at + datetime.timedelta(seconds=s)  # noqa: E731

    run = asyncio.create_task(rig.executor.run_due(at(1)))
    await rig.wait_for_dial()
    first = await rig.call_for_action(incident_id, step_order=1)
    await rig.progress(first.id, "initiated", "failed")
    assert await asyncio.wait_for(run, timeout=10) == 1

    # Retry (attempt 2) is a distinct idempotency key: the dial goes ahead.
    run = asyncio.create_task(rig.executor.run_due(at(2)))
    await rig.wait_for_dial()
    second = await rig.call_for_action(incident_id, step_order=1)
    assert second.attempt == 2 and second.id != first.id
    await rig.progress(second.id, "initiated", "ringing", "busy")
    assert await asyncio.wait_for(run, timeout=10) == 1
    assert len(rig.twilio.created) == 2
    types = await event_types(container.session_factory, incident_id)
    assert "CALL_FAILED" in types  # the failed dial is on the record, never hidden


async def test_operator_stop_call_cancels_the_live_call_and_is_audited(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    await use_e164_numbers(container, tenant)
    rig = VoiceRig(container)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await incident_row(container, incident_id)
    run = asyncio.create_task(
        rig.executor.run_due(incident.created_at + datetime.timedelta(seconds=1))
    )
    await rig.wait_for_dial()
    call = await rig.call_for_action(incident_id, step_order=1)
    await rig.progress(call.id, "initiated", "ringing", "in-progress")

    operator = await client_factory(tenant.emails[Role.OPERATOR])
    stopped = await operator.post(f"/v1/incidents/{incident_id}/calls/{call.id}/stop")
    assert stopped.status_code == 204
    assert await asyncio.wait_for(run, timeout=10) == 1  # the waiting executor sees CANCELLED

    call = await rig.call_for_action(incident_id, step_order=1)
    assert call.status is CallStatus.CANCELLED
    assert call.failure_category == "operator_cancelled"
    types = await event_types(container.session_factory, incident_id)
    assert "AUTOMATED_CALL_CANCELLED" in types
    actions = await audit_actions(container.session_factory, resource_id=incident_id)
    assert "CALL_CANCELLED_BY_OPERATOR" in actions
    incident = await incident_row(container, incident_id)
    assert incident.is_active  # stopping a call never resolves anything

    # Idempotence/conflict: stopping an already-ended call is a 409, not a silent success.
    again = await operator.post(f"/v1/incidents/{incident_id}/calls/{call.id}/stop")
    assert again.status_code == 409


async def test_escalate_now_pulls_the_operator_alert_forward(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    response = await operator.post(f"/v1/incidents/{incident_id}/escalate-now")
    assert response.status_code == 202
    assert response.json()["accelerated_steps"] >= 1
    async with container.session_factory() as session:
        due_at = await session.scalar(
            select(ScheduledAction.due_at).where(
                ScheduledAction.incident_id == uuid.UUID(incident_id),
                ScheduledAction.action_type == EscalationActionType.OPERATOR_ESCALATION,
                ScheduledAction.status == ScheduledActionStatus.PENDING,
            )
        )
    assert due_at is not None and due_at <= utcnow()
    types = await event_types(container.session_factory, incident_id)
    assert "OPERATOR_ESCALATION_REQUESTED" in types
    actions = await audit_actions(container.session_factory, resource_id=incident_id)
    assert "ESCALATION_ACCELERATED" in actions
    # Resolving/closing controls that do not exist: the API offers no AI resolve/auto-close.
    for forbidden in ("ai-resolve", "auto-resolve", "mark-safe"):
        missing = await operator.post(f"/v1/incidents/{incident_id}/{forbidden}")
        assert missing.status_code in (404, 405)


async def test_telephony_disabled_and_unlisted_numbers_never_dial_but_still_escalate(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    await use_e164_numbers(container, tenant)
    disabled_settings = container.settings.model_copy(
        update={
            "voice_provider": "twilio",
            "real_telephony_enabled": False,
            "twilio_account_sid": "AC" + "0" * 32,
            "twilio_auth_token": container.settings.secret_key,  # any secret-shaped value
            "twilio_from_number": "+441134960000",
            "twilio_webhook_base_url": "https://careos.example",
            "voice_call_poll_interval_seconds": 0.02,
            "provider_timeout_seconds": 2,
        }
    )
    rig = VoiceRig(container, settings=disabled_settings)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await incident_row(container, incident_id)
    at = lambda s: incident.created_at + datetime.timedelta(seconds=s)  # noqa: E731
    for second in (1, 2, 3, 4):  # three refused attempts, then the fail-safe operator alert
        await rig.executor.run_due(at(second))
    assert rig.twilio.created == []  # the safety switch means Twilio is NEVER contacted
    types = await event_types(container.session_factory, incident_id)
    assert types.count("CALL_FAILED") == 3
    assert "OPERATORS_ALERTED" in types
    incident = await incident_row(container, incident_id)
    assert incident.status.value == "ESCALATED"  # humans brought in; nothing lost
