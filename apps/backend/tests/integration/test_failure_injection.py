"""Failure injection: dependencies fail, the safety core keeps its guarantees.

Each test breaks one dependency (Redis, the realtime broker, AI, voice/notification providers,
the worker, the database transaction) or races the gateway, and asserts the incident is still
persisted, escalated and recorded — never lost, rolled back, resolved or closed by the failure.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from collections import Counter
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update

from careos.bootstrap import Container, ProviderRegistry, build_container
from careos.contracts.realtime import RealtimeMessage
from careos.modules.ai_orchestrator.models import AISession, AISessionStatus
from careos.modules.ai_orchestrator.orchestrator import (
    AIOrchestrator,
    CheckInAssist,
    CheckInContext,
)
from careos.modules.device_gateway.models import DeviceEventReceipt
from careos.modules.escalation_engine.models import (
    EscalationActionType,
    ScheduledAction,
    ScheduledActionStatus,
)
from careos.modules.incident_engine.models import Incident, IncidentEvent
from careos.modules.incident_engine.service import IncidentEngine
from careos.modules.incident_engine.state_machine import ACTIVE_STATUSES, IncidentStatus
from careos.modules.notification_engine.models import Call, CallStatus
from careos.modules.notification_engine.providers import (
    MockVoiceProvider,
    NotificationRequest,
    NotificationResult,
    ProviderError,
    VoiceCallRequest,
    VoiceCallResult,
)
from tests.factories import ClientFactory, Tenant, sos_event
from tests.integration.helpers import (
    asgi_client,
    at,
    audit_actions,
    count,
    event_types,
    incident_count,
    load_incident,
    make_executor,
    metric,
    send_event,
)

HUMAN_OUTCOMES = {IncidentStatus.RESOLVED, IncidentStatus.FALSE_ALARM, IncidentStatus.CLOSED}


async def forever() -> None:
    await asyncio.Event().wait()


# ----------------------------------------------------------------------------- Redis / realtime


async def test_sos_is_persisted_and_escalated_when_redis_is_unavailable(
    container: Container, providers: ProviderRegistry, tenant: Tenant
) -> None:
    unreachable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    degraded = build_container(
        container.settings,
        with_hub=True,
        providers=providers,
        engine=container.engine,
        redis=unreachable,
    )
    failures_before = metric(
        "careos_realtime_delivery_failures_total", stage="broker_publish"
    ) + metric("careos_realtime_delivery_failures_total", stage="broker_unavailable")
    try:
        async with asgi_client(degraded) as client:
            first = await send_event(client, tenant, sos_event(tenant.device_external_id))
            repeat = await send_event(client, tenant, sos_event(tenant.device_external_id))
            ready = await client.get("/ready")
        executor = make_executor(degraded, providers)
        incident = await load_incident(container, first.json()["incident_id"])
        assert await executor.run_due(at(incident, 1)) == 1  # the worker does not need Redis
    finally:
        await unreachable.aclose()

    assert first.status_code == 202, first.text
    assert first.json()["outcome"] == "INCIDENT_CREATED"
    assert repeat.status_code == 202 and repeat.json()["outcome"] == "ATTACHED_TO_INCIDENT"
    incident_id = first.json()["incident_id"]
    assert await incident_count(container.session_factory, tenant.organisation_id) == 1
    types = await event_types(container.session_factory, incident_id)
    assert types[:2] == ["SOS_RECEIVED", "INCIDENT_CREATED"]
    assert "ESCALATION_SCHEDULED" in types and "AUTOMATED_CALL_STARTED" in types
    assert "INCIDENT_CREATED" in await audit_actions(
        container.session_factory, resource_id=incident_id
    )

    # Degradation is visible: circuit open, failures counted, readiness reports it but stays up.
    assert degraded.redis_circuit is not None and degraded.redis_circuit.degraded
    failures_after = metric(
        "careos_realtime_delivery_failures_total", stage="broker_publish"
    ) + metric("careos_realtime_delivery_failures_total", stage="broker_unavailable")
    assert failures_after - failures_before >= 4  # two messages per accepted SOS
    assert ready.status_code == 200
    assert ready.json()["redis"] == "unavailable"
    assert ready.json()["realtime"] == "degraded"


class ExplodingPublisher:
    async def publish(self, message: RealtimeMessage) -> None:
        raise RuntimeError("broker exploded")


class HangingPublisher:
    async def publish(self, message: RealtimeMessage) -> None:
        await forever()


@pytest.mark.parametrize(
    "publisher", [ExplodingPublisher(), HangingPublisher()], ids=["raises", "hangs"]
)
async def test_realtime_failure_never_fails_or_delays_a_committed_incident(
    container: Container, tenant: Tenant, publisher: Any
) -> None:
    settings = container.settings.model_copy(update={"realtime_publish_timeout_seconds": 0.2})
    broken = dataclasses.replace(container, settings=settings, realtime=publisher)
    before = metric("careos_realtime_delivery_failures_total", stage="after_commit")

    async with asgi_client(broken) as client:
        started = time.monotonic()
        response = await send_event(client, tenant, sos_event(tenant.device_external_id))
        elapsed = time.monotonic() - started

    assert response.status_code == 202, response.text
    assert elapsed < 2.0  # bounded: 2 notifications x 0.2 s, never the broker's own timeout
    assert await incident_count(container.session_factory, tenant.organisation_id) == 1
    assert metric("careos_realtime_delivery_failures_total", stage="after_commit") - before == 2


# ----------------------------------------------------------------------------- AI provider


class HangingAIProvider:
    name = "hanging-ai"

    async def assist_check_in(self, context: CheckInContext) -> CheckInAssist:
        await forever()
        raise AssertionError("unreachable")


class BrokenAIProvider:
    name = "broken-ai"

    async def assist_check_in(self, context: CheckInContext) -> CheckInAssist:
        raise KeyError("malformed model response")


@pytest.mark.parametrize(
    ("provider", "session_status"),
    [
        (HangingAIProvider(), AISessionStatus.TIMED_OUT),
        (BrokenAIProvider(), AISessionStatus.FAILED),
    ],
    ids=["hangs", "raises"],
)
async def test_ai_provider_failure_never_blocks_or_changes_the_incident(
    client_factory: ClientFactory,
    tenant: Tenant,
    container: Container,
    providers: ProviderRegistry,
    provider: Any,
    session_status: AISessionStatus,
) -> None:
    providers.ai = AIOrchestrator(provider, timeout_seconds=0.3)
    timeouts_before = metric("careos_ai_timeouts_total", provider="hanging-ai")
    response = await send_event(
        await client_factory(), tenant, sos_event(tenant.device_external_id)
    )
    incident = await load_incident(container, response.json()["incident_id"])

    started = time.monotonic()
    assert await make_executor(container, providers).run_due(at(incident, 1)) == 1
    assert time.monotonic() - started < 5

    types = await event_types(container.session_factory, incident.id)
    assert "AUTOMATED_CALL_NO_ANSWER" in types  # the deterministic call happened regardless
    assert "AI_CALL_FAILED" in types
    async with container.session_factory() as session:
        ai_session = await session.scalar(select(AISession))
        ai_status_changes = await session.scalar(
            select(func.count())
            .select_from(IncidentEvent)
            .where(IncidentEvent.actor_type == "AI", IncidentEvent.to_status.is_not(None))
        )
    assert ai_session is not None and ai_session.status is session_status
    assert ai_status_changes == 0
    assert (await load_incident(container, incident.id)).status is IncidentStatus.CONTACTING
    if isinstance(provider, HangingAIProvider):
        assert metric("careos_ai_timeouts_total", provider="hanging-ai") - timeouts_before == 1


# ------------------------------------------------------------------------ voice / notifications


class ScriptedVoiceProvider:
    name = "scripted-voice"

    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def place_call(self, request: VoiceCallRequest) -> VoiceCallResult:
        if self.mode == "provider_error":
            raise ProviderError("carrier rejected the call")
        if self.mode == "unexpected":
            raise RuntimeError("provider SDK bug")
        await forever()
        raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("mode", "category"),
    [("provider_error", "provider_error"), ("unexpected", "unexpected"), ("hangs", "timeout")],
)
async def test_voice_provider_failure_is_recorded_and_escalates_to_humans(
    client_factory: ClientFactory,
    tenant: Tenant,
    container: Container,
    providers: ProviderRegistry,
    mode: str,
    category: str,
) -> None:
    providers.voice = ScriptedVoiceProvider(mode)
    providers.ai = AIOrchestrator(None, timeout_seconds=1)
    failures_before = metric(
        "careos_provider_failures_total",
        provider_kind="voice",
        provider="scripted-voice",
        category=category,
    )
    exhausted_before = metric(
        "careos_escalation_failures_total",
        action_type="AUTOMATED_USER_CONTACT",
        category="exhausted",
    )
    response = await send_event(
        await client_factory(), tenant, sos_event(tenant.device_external_id)
    )
    incident = await load_incident(container, response.json()["incident_id"])
    executor = make_executor(container, providers, provider_timeout_seconds=0.2)

    for second in (1, 2, 3):  # three attempts, retry backoff is 0 s in tests
        assert await executor.run_due(at(incident, second)) == 1
        current = await load_incident(container, incident.id)
        assert current.status in ACTIVE_STATUSES and current.status not in HUMAN_OUTCOMES
    assert await executor.run_due(at(incident, 4)) == 1  # fail-safe operator alert

    current = await load_incident(container, incident.id)
    assert current.status is IncidentStatus.ESCALATED
    assert current.resolved_at is None and current.closed_at is None
    types = await event_types(container.session_factory, incident.id)
    assert types.count("CALL_FAILED") == 3
    assert types[-1] == "OPERATORS_ALERTED"
    assert types[:2] == ["SOS_RECEIVED", "INCIDENT_CREATED"]  # nothing was removed
    async with container.session_factory() as session:
        reasons = list(await session.scalars(select(Call.failure_reason)))
        statuses = set(await session.scalars(select(Call.status)))
    assert reasons == [category] * 3 and statuses == {CallStatus.FAILED}
    assert (
        metric(
            "careos_provider_failures_total",
            provider_kind="voice",
            provider="scripted-voice",
            category=category,
        )
        - failures_before
        == 3
    )
    assert (
        metric(
            "careos_escalation_failures_total",
            action_type="AUTOMATED_USER_CONTACT",
            category="exhausted",
        )
        - exhausted_before
        == 1
    )


class FailingNotificationProvider:
    name = "failing-notify"

    async def send(self, request: NotificationRequest) -> NotificationResult:
        raise ProviderError("SMS gateway unavailable")


async def test_notification_provider_failure_is_recorded_and_escalates_to_humans(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    providers.notifications = FailingNotificationProvider()
    response = await send_event(
        await client_factory(), tenant, sos_event(tenant.device_external_id)
    )
    incident = await load_incident(container, response.json()["incident_id"])
    async with container.session_factory() as session:
        # Isolate a single SMS step: cancel the default ladder, schedule a notification step.
        await session.execute(
            update(ScheduledAction)
            .where(ScheduledAction.incident_id == incident.id)
            .values(status=ScheduledActionStatus.CANCELLED)
        )
        session.add(
            ScheduledAction(
                organisation_id=incident.organisation_id,
                incident_id=incident.id,
                step_order=10,
                action_type=EscalationActionType.NOTIFY_TRUSTED_CONTACT,
                contact_priority=1,
                delay_seconds=0,
                due_at=incident.created_at,
                max_attempts=3,
            )
        )
        await session.commit()
    before = metric(
        "careos_provider_failures_total",
        provider_kind="notification",
        provider="failing-notify",
        category="provider_error",
    )
    executor = make_executor(container, providers)
    for second in (1, 2, 3):
        assert await executor.run_due(at(incident, second)) == 1
    assert await executor.run_due(at(incident, 4)) == 1  # fail-safe operator alert

    current = await load_incident(container, incident.id)
    assert current.status is IncidentStatus.ESCALATED
    types = await event_types(container.session_factory, incident.id)
    assert types.count("NOTIFICATION_FAILED") == 3 and types[-1] == "OPERATORS_ALERTED"
    after = metric(
        "careos_provider_failures_total",
        provider_kind="notification",
        provider="failing-notify",
        category="provider_error",
    )
    assert after - before == 3


# ----------------------------------------------------------------------------- worker outage


async def test_escalation_survives_worker_crash_and_downtime_in_policy_order(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    providers.ai = AIOrchestrator(None, timeout_seconds=1)
    response = await send_event(
        await client_factory(), tenant, sos_event(tenant.device_external_id)
    )
    incident = await load_incident(container, response.json()["incident_id"])

    crashed = make_executor(container, providers)
    (lost,) = await crashed.claim(at(incident, 1))  # claimed, then the process dies
    assert lost.step_order == 1

    # Ten minutes later a worker starts. Every step is overdue; nothing was lost.
    restarted = make_executor(container, providers)
    now = at(incident, 600)
    first_batch = await restarted.claim(now)
    # The abandoned step is reclaimed and humans are alerted at once; contact steps wait their turn.
    assert sorted(action.step_order for action in first_batch) == [1, 4]
    await asyncio.gather(*(restarted.execute(action) for action in first_batch))
    rounds = 0
    while await restarted.run_due(now):
        rounds += 1
        assert rounds < 10

    voice = providers.voice
    assert isinstance(voice, MockVoiceProvider)
    assert [call.to_number for call in voice.calls] == [
        "+44 20 7946 0018",  # service user
        "+44 7700 900123",  # trusted contact #1
        "+44 7700 900456",  # trusted contact #2
    ]
    async with container.session_factory() as session:
        actions = {
            a.step_order: a
            for a in await session.scalars(
                select(ScheduledAction).where(ScheduledAction.incident_id == incident.id)
            )
        }
    assert {a.status for a in actions.values()} == {ScheduledActionStatus.COMPLETED}
    assert actions[1].attempts == 2 and all(actions[o].attempts == 1 for o in (2, 3, 4))
    assert (await load_incident(container, incident.id)).status is IncidentStatus.ESCALATED


# ----------------------------------------------------------------------------- gateway races


async def test_same_event_id_delivered_concurrently_creates_one_receipt_and_one_incident(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    event = sos_event(tenant.device_external_id)
    responses = await asyncio.gather(*(send_event(client, tenant, event) for _ in range(8)))

    assert {r.status_code for r in responses} == {202}
    bodies = [r.json() for r in responses]
    assert len({b["incident_id"] for b in bodies}) == 1
    assert len({b["receipt_id"] for b in bodies}) == 1
    assert sum(not b["duplicate"] for b in bodies) == 1
    assert await count(container.session_factory, DeviceEventReceipt) == 1
    assert await incident_count(container.session_factory, tenant.organisation_id) == 1
    types = await event_types(container.session_factory, bodies[0]["incident_id"])
    assert types.count("SOS_RECEIVED") == 1 and "ALARM_REPEATED" not in types


async def test_distinct_sos_events_from_one_device_concurrently_yield_one_active_incident(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    events = [sos_event(tenant.device_external_id) for _ in range(8)]
    responses = await asyncio.gather(*(send_event(client, tenant, e) for e in events))

    assert {r.status_code for r in responses} == {202}
    bodies = [r.json() for r in responses]
    assert Counter(b["outcome"] for b in bodies) == {
        "INCIDENT_CREATED": 1,
        "ATTACHED_TO_INCIDENT": 7,
    }
    (incident_id,) = {b["incident_id"] for b in bodies}
    assert (
        await count(
            container.session_factory,
            Incident,
            Incident.device_id == tenant.device_id,
            Incident.status.in_(ACTIVE_STATUSES),
        )
        == 1
    )
    types = await event_types(container.session_factory, incident_id)
    assert types.count("SOS_RECEIVED") == 1 and types.count("ALARM_REPEATED") == 7
    async with container.session_factory() as session:
        sequences = list(
            await session.scalars(
                select(IncidentEvent.sequence)
                .where(IncidentEvent.incident_id == uuid.UUID(incident_id))
                .order_by(IncidentEvent.sequence)
            )
        )
    assert sequences == list(range(1, len(sequences) + 1))  # gap-free, strictly ordered


# ----------------------------------------------------------------------------- transaction rollback


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[RealtimeMessage] = []

    async def publish(self, message: RealtimeMessage) -> None:
        self.messages.append(message)


async def test_crash_before_commit_leaves_no_partial_incident_state(
    container: Container, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = RecordingPublisher()
    recording = dataclasses.replace(container, realtime=publisher)
    original = IncidentEngine.handle_device_event
    written_before_crash: dict[str, int] = {}

    async def crash_after_writing(self: IncidentEngine, uow: Any, **kwargs: Any) -> Any:
        await original(self, uow, **kwargs)
        await uow.session.flush()  # incident, timeline, audit and schedule reach the database
        for model in (Incident, IncidentEvent, ScheduledAction):
            written_before_crash[model.__tablename__] = int(
                await uow.session.scalar(select(func.count()).select_from(model)) or 0
            )
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(IncidentEngine, "handle_device_event", crash_after_writing)
    created_before = metric(
        "careos_incidents_created_total", trigger_type="SOS_BUTTON", priority="CRITICAL"
    )
    event = sos_event(tenant.device_external_id)
    async with asgi_client(recording) as client:
        failed = await send_event(client, tenant, event)
        assert failed.status_code == 500
        assert all(written_before_crash[name] > 0 for name in written_before_crash)

        for model in (Incident, IncidentEvent, ScheduledAction, DeviceEventReceipt):
            assert await count(container.session_factory, model) == 0, model.__tablename__
        assert "INCIDENT_CREATED" not in await audit_actions(container.session_factory)
        assert publisher.messages == []  # nothing announced for state that never existed
        assert (
            metric("careos_incidents_created_total", trigger_type="SOS_BUTTON", priority="CRITICAL")
            == created_before
        )

        # The idempotency ledger rolled back too, so the device's retry is processed normally.
        monkeypatch.undo()
        retry = await send_event(client, tenant, event)
    assert retry.status_code == 202
    assert retry.json()["outcome"] == "INCIDENT_CREATED" and retry.json()["duplicate"] is False
    assert await incident_count(container.session_factory, tenant.organisation_id) == 1
    assert {m.type.value for m in publisher.messages} == {"incident.created", "device.updated"}
