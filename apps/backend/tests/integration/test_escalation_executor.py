from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select, update

from careos.bootstrap import Container, ProviderRegistry
from careos.core.time import utcnow
from careos.modules.ai_orchestrator.orchestrator import (
    AIOrchestrator,
    CheckInAssist,
    CheckInContext,
    UnavailableAIProvider,
)
from careos.modules.devices.models import ConnectionStatus, DeviceConnection
from careos.modules.devices.service import mark_stale_devices_offline
from careos.modules.escalation_engine.models import ScheduledAction, ScheduledActionStatus
from careos.modules.identity.rbac import Role
from careos.modules.incident_engine.models import IncidentEvent
from careos.modules.notification_engine.models import Call, CallStatus
from careos.modules.notification_engine.providers import MockVoiceProvider
from tests.factories import ClientFactory, Tenant, sos_event
from tests.integration.helpers import (
    at,
    count,
    event_types,
    load_incident,
    make_executor,
    raise_sos,
    send_event,
)


async def test_full_escalation_ladder(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    executor = make_executor(container, providers)

    assert await executor.run_due(at(incident, 1)) == 1
    assert (await load_incident(container, incident_id)).status.value == "CONTACTING"
    assert await executor.run_due(at(incident, 2)) == 0  # nothing else due yet
    assert await executor.run_due(at(incident, 31)) == 1
    assert await executor.run_due(at(incident, 61)) == 1
    assert (await load_incident(container, incident_id)).status.value == "CONTACTING"
    assert await executor.run_due(at(incident, 91)) == 1
    assert (await load_incident(container, incident_id)).status.value == "ESCALATED"

    voice = providers.voice
    assert isinstance(voice, MockVoiceProvider)
    assert [c.to_number for c in voice.calls] == [
        "+44 20 7946 0018",
        "+44 7700 900123",
        "+44 7700 900456",
    ]
    types = await event_types(container.session_factory, incident_id)
    # AI assistance runs alongside the call, so only the deterministic events have a fixed order.
    assert [t for t in types[5:] if not t.startswith("AI_")] == [
        "AUTOMATED_CALL_STARTED",
        "AUTOMATED_CALL_NO_ANSWER",
        "TRUSTED_CONTACT_CALLED",
        "TRUSTED_CONTACT_NO_ANSWER",
        "TRUSTED_CONTACT_CALLED",
        "TRUSTED_CONTACT_NO_ANSWER",
        "OPERATORS_ALERTED",
    ]
    assert {"AI_CALL_STARTED", "AI_CALL_COMPLETED"} <= set(types)
    assert await count(container.session_factory, Call, Call.status == CallStatus.NO_ANSWER) == 3


async def test_provider_failure_retries_then_alerts_operators(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    providers.voice = MockVoiceProvider("failure")
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    executor = make_executor(container, providers)

    for second in (1, 2, 3):  # attempts 1..3 (retry backoff is 0s in tests)
        assert await executor.run_due(at(incident, second)) == 1
    assert await executor.run_due(at(incident, 4)) == 1  # fail-safe operator alert, not T+90

    current = await load_incident(container, incident_id)
    assert current.status.value == "ESCALATED"
    types = await event_types(container.session_factory, incident_id)
    assert types.count("CALL_FAILED") == 3
    assert "ESCALATION_STEP_FAILED" in types
    assert types[-1] == "OPERATORS_ALERTED"
    assert await count(container.session_factory, Call, Call.status == CallStatus.FAILED) == 3


async def test_ai_outage_does_not_affect_incident_handling(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    providers.ai = AIOrchestrator(UnavailableAIProvider(), timeout_seconds=1)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    await make_executor(container, providers).run_due(at(incident, 1))

    types = await event_types(container.session_factory, incident_id)
    assert "AI_CALL_FAILED" in types
    assert "AUTOMATED_CALL_NO_ANSWER" in types  # the deterministic call still happened
    assert (await load_incident(container, incident_id)).status.value == "CONTACTING"


async def test_slow_ai_never_delays_the_deterministic_call(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    class HangingAIProvider:
        name = "hanging-ai"

        async def assist_check_in(self, context: CheckInContext) -> CheckInAssist:
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    providers.ai = AIOrchestrator(HangingAIProvider(), timeout_seconds=1)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    await make_executor(container, providers).run_due(at(incident, 1))

    async with container.session_factory() as session:
        rows = (
            await session.execute(
                select(IncidentEvent.event_type, IncidentEvent.sequence).where(
                    IncidentEvent.incident_id == uuid.UUID(incident_id)
                )
            )
        ).all()
    sequence = {row.event_type.value: row.sequence for row in rows}
    # The call outcome was recorded before the AI assistant gave up.
    assert sequence["AUTOMATED_CALL_NO_ANSWER"] < sequence["AI_CALL_FAILED"]


async def test_escalation_runs_with_ai_disabled(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    providers.ai = AIOrchestrator(None, timeout_seconds=1)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    executor = make_executor(container, providers)
    for second in (1, 31, 61, 91):
        assert await executor.run_due(at(incident, second)) == 1

    types = await event_types(container.session_factory, incident_id)
    assert not [t for t in types if t.startswith("AI_")]
    assert types[-1] == "OPERATORS_ALERTED"
    assert (await load_incident(container, incident_id)).status.value == "ESCALATED"


async def test_acknowledgement_stops_contact_steps_but_operators_still_verify(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    providers.voice = MockVoiceProvider("answered_acknowledged")
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    executor = make_executor(container, providers)

    await executor.run_due(at(incident, 1))
    current = await load_incident(container, incident_id)
    assert current.status.value == "ACKNOWLEDGED"
    assert current.acknowledged_at is not None
    assert await executor.run_due(at(incident, 61)) == 0  # contact calls were cancelled
    assert await executor.run_due(at(incident, 91)) == 1
    assert (await load_incident(container, incident_id)).status.value == "ACKNOWLEDGED"
    assert (await event_types(container.session_factory, incident_id))[-1] == "OPERATORS_ALERTED"


async def test_operator_takeover_stops_automation(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    assert (await operator.post(f"/v1/incidents/{incident_id}/takeover")).status_code == 200
    assert await make_executor(container, providers).run_due(at(incident, 120)) == 0


async def test_action_running_when_incident_resolved_is_skipped(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    executor = make_executor(container, providers)
    (claimed,) = await executor.claim(at(incident, 1))

    manager = await client_factory(tenant.emails[Role.CARE_MANAGER])
    await manager.post(
        f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE", "notes": "Resolved"}
    )
    await executor.execute(claimed)

    async with container.session_factory() as session:
        action = await session.get(ScheduledAction, claimed.id)
    assert action is not None and action.status is ScheduledActionStatus.SKIPPED
    assert isinstance(providers.voice, MockVoiceProvider) and providers.voice.calls == []


async def test_concurrent_workers_never_claim_the_same_action(
    client_factory: ClientFactory,
    tenant: Tenant,
    other_tenant: Tenant,
    container: Container,
    providers: ProviderRegistry,
) -> None:
    gateway = await client_factory()
    await raise_sos(gateway, tenant)
    await raise_sos(gateway, other_tenant)
    await send_event(gateway, tenant, sos_event(tenant.unassigned_device_external_id))
    later = utcnow() + timedelta(minutes=5)

    workers = [make_executor(container, providers, worker_batch_size=3) for _ in range(4)]
    batches = await asyncio.gather(*(w.claim(later) for w in workers))
    ids = [action.id for batch in batches for action in batch]
    # Per incident only the next step in policy order is claimable, plus operator alerts:
    # 2 x (first contact step + operator step) + 1 fail-safe step for the unassigned device.
    assert len(ids) == len(set(ids)) == 5


async def test_expired_lease_is_reclaimed_after_worker_crash(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    crashed = make_executor(container, providers)
    (claimed,) = await crashed.claim(at(incident, 1))  # claimed, then the worker "dies"

    survivor = make_executor(container, providers)
    assert await survivor.claim(at(incident, 2)) == []  # lease still valid
    assert await survivor.run_due(at(incident, 1 + 61)) >= 1  # lease expired -> reclaimed
    assert "AUTOMATED_CALL_NO_ANSWER" in await event_types(container.session_factory, incident_id)
    async with container.session_factory() as session:
        action = await session.get(ScheduledAction, claimed.id)
    assert action is not None
    assert action.status is ScheduledActionStatus.COMPLETED
    assert action.attempts == 2  # the crashed attempt is counted


async def test_device_sweep_marks_silent_devices_offline(
    tenant: Tenant, container: Container
) -> None:
    async with container.session_factory() as session:
        await session.execute(
            update(DeviceConnection)
            .where(DeviceConnection.device_id == tenant.device_id)
            .values(last_seen_at=utcnow() - timedelta(days=2))
        )
        await session.commit()
    async with container.session_factory() as session:
        assert await mark_stale_devices_offline(session, offline_after_seconds=86_400) == 1
    async with container.session_factory() as session:
        status = await session.scalar(
            select(DeviceConnection.status).where(DeviceConnection.device_id == tenant.device_id)
        )
    assert status is ConnectionStatus.OFFLINE
