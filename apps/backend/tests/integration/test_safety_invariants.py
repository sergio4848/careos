"""Incident safety invariants, proven at the API, ORM and database level.

The Python state machine is the first line of defence; PostgreSQL is the last (ADR-013).
Where a test uses raw SQL it deliberately bypasses every Python guard, to show the database
alone still refuses the unsafe state.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from careos.bootstrap import Container, ProviderRegistry
from careos.core.errors import InvalidStateTransitionError
from careos.core.time import utcnow
from careos.modules.ai_orchestrator.orchestrator import (
    AIOrchestrator,
    AIOutcome,
    CheckInAssist,
    CheckInContext,
)
from careos.modules.audit.models import AuditLog, AuditOutcome
from careos.modules.device_gateway.models import DeviceEventReceipt
from careos.modules.identity.models import User
from careos.modules.identity.rbac import Role
from careos.modules.incident_engine.models import (
    ActorType,
    Incident,
    IncidentAssignment,
    IncidentEvent,
    IncidentEventType,
    IncidentStatusTransition,
)
from careos.modules.incident_engine.state_machine import (
    ACTIVE_STATUSES,
    TRANSITIONS,
    IncidentStatus,
)
from careos.modules.notification_engine.providers import MockVoiceProvider
from tests.factories import ClientFactory, Tenant, sos_event
from tests.integration.helpers import (
    at,
    count,
    load_incident,
    make_executor,
    raise_sos,
    send_event,
)

S = IncidentStatus


async def user_id(container: Container, email: str) -> uuid.UUID:
    async with container.session_factory() as session:
        found = await session.scalar(select(User.id).where(User.email == email))
    assert found is not None
    return found


async def expect_violation(
    container: Container, work: Callable[[AsyncSession], Awaitable[Any]], match: str
) -> None:
    """Run ``work`` and commit in one transaction; the database must reject it."""
    async with container.session_factory() as session:
        with pytest.raises(IntegrityError, match=match):
            await work(session)
            await session.commit()


def forged_event(incident: Incident, **overrides: Any) -> IncidentEvent:
    values: dict[str, Any] = {
        "organisation_id": incident.organisation_id,
        "incident_id": incident.id,
        "sequence": incident.last_event_sequence + 1,
        "event_type": IncidentEventType.INCIDENT_OPENED,
        "actor_type": ActorType.SYSTEM,
        "actor_user_id": None,
        "message": "forged",
        "data": {},
        "occurred_at": utcnow(),
    }
    values.update(overrides)
    return IncidentEvent(**values)


async def status_chain(container: Container, incident_id: uuid.UUID | str) -> list[tuple[Any, Any]]:
    async with container.session_factory() as session:
        rows = await session.execute(
            select(IncidentEvent.from_status, IncidentEvent.to_status)
            .where(
                IncidentEvent.incident_id == uuid.UUID(str(incident_id)),
                IncidentEvent.to_status.is_not(None),
            )
            .order_by(IncidentEvent.sequence)
        )
        return [tuple(row) for row in rows]


# ----------------------------------------------------------------------------- state machine


async def test_database_transition_table_matches_the_state_machine(container: Container) -> None:
    async with container.session_factory() as session:
        rows = await session.execute(
            select(IncidentStatusTransition.from_status, IncidentStatusTransition.to_status)
        )
        in_database = {(f, t) for f, t in rows}
    in_code = {(f.value, t.value) for f, targets in TRANSITIONS.items() for t in targets}
    assert in_database == in_code


async def test_closed_incident_cannot_reopen_through_api_orm_or_sql(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    await operator.post(f"/v1/incidents/{incident_id}/takeover")
    await operator.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})
    assert (await operator.post(f"/v1/incidents/{incident_id}/close", json={})).status_code == 200

    # API
    for action, body in (("takeover", None), ("resolve", {"category": "USER_SAFE"})):
        response = await operator.post(f"/v1/incidents/{incident_id}/{action}", json=body)
        assert response.status_code == 409
    # ORM
    async with container.session_factory() as session:
        incident = await session.get(Incident, uuid.UUID(incident_id))
        assert incident is not None
        with pytest.raises(InvalidStateTransitionError):
            incident.status = S.OPEN
    # SQL, without touching the timeline
    closed = await load_incident(container, incident_id)
    await expect_violation(
        container,
        lambda session: session.execute(
            update(Incident).where(Incident.id == closed.id).values(status=S.OPEN)
        ),
        match="not the last status in its timeline",
    )
    # SQL, forging a matching timeline entry
    operator_id = await user_id(container, tenant.emails[Role.OPERATOR])

    async def forge_reopen(session: AsyncSession) -> None:
        session.add(
            forged_event(
                closed,
                from_status=S.CLOSED,
                to_status=S.OPEN,
                actor_type=ActorType.USER,
                actor_user_id=operator_id,
            )
        )
        await session.execute(
            update(Incident)
            .where(Incident.id == closed.id)
            .values(status=S.OPEN, last_event_sequence=closed.last_event_sequence + 1)
        )

    await expect_violation(container, forge_reopen, match="illegal status change CLOSED -> OPEN")
    assert (await load_incident(container, incident_id)).status is S.CLOSED


async def test_every_status_change_is_recorded_in_the_timeline(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    executor = make_executor(container, providers)
    await executor.run_due(at(incident, 1))  # OPEN -> CONTACTING (worker)
    for second in (31, 61, 91):  # contacts, then operators: CONTACTING -> ESCALATED
        await executor.run_due(at(incident, second))
    await operator.post(f"/v1/incidents/{incident_id}/takeover")  # ESCALATED -> IN_PROGRESS
    await operator.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})
    await operator.post(f"/v1/incidents/{incident_id}/close", json={})

    chain = await status_chain(container, incident_id)
    assert [to for _, to in chain] == [
        S.RECEIVED,
        S.VALIDATING,
        S.OPEN,
        S.CONTACTING,
        S.ESCALATED,
        S.IN_PROGRESS,
        S.RESOLVED,
        S.CLOSED,
    ]
    assert chain[0] == (None, S.RECEIVED)
    for (_, previous), (source, target) in itertools.pairwise(chain):
        assert source == previous and target in TRANSITIONS[source]
    assert (await load_incident(container, incident_id)).status is chain[-1][1]

    # A legal transition that skips the timeline is still refused by the database.
    fresh_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    await expect_violation(
        container,
        lambda session: session.execute(
            update(Incident).where(Incident.id == uuid.UUID(fresh_id)).values(status=S.ESCALATED)
        ),
        match="not the last status in its timeline",
    )


# ----------------------------------------------------------------------------- assignments


async def test_assignments_cannot_cross_tenants_and_only_one_can_be_active(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    first = await client_factory(tenant.emails[Role.OPERATOR])
    second = await client_factory(tenant.second_operator_email)
    responses = await asyncio.gather(
        *(client.post(f"/v1/incidents/{incident_id}/takeover") for client in (first, second) * 4)
    )
    winners = {r.json()["assignee"]["id"] for r in responses if r.status_code == 200}
    assert len(winners) == 1
    assert {r.status_code for r in responses} <= {200, 409}
    active = (IncidentAssignment.released_at.is_(None),)
    assert await count(container.session_factory, IncidentAssignment, *active) == 1

    incident = await load_incident(container, incident_id)
    foreign_user = await user_id(container, other_tenant.emails[Role.OPERATOR])
    local_user = await user_id(container, tenant.second_operator_email)

    def assignment(**overrides: Any) -> IncidentAssignment:
        values: dict[str, Any] = {
            "organisation_id": incident.organisation_id,
            "incident_id": incident.id,
            "user_id": local_user,
            "assigned_at": utcnow(),
            "released_at": utcnow(),
        }
        values.update(overrides)
        return IncidentAssignment(**values)

    async def add(session: AsyncSession, row: IncidentAssignment) -> None:
        session.add(row)
        await session.flush()

    await expect_violation(
        container,
        lambda s: add(s, assignment(user_id=foreign_user)),
        match="fk_incident_assignments_user_same_org",
    )
    await expect_violation(
        container,
        lambda s: add(
            s, assignment(organisation_id=other_tenant.organisation_id, user_id=foreign_user)
        ),
        match="fk_incident_assignments_incident_same_org",
    )
    await expect_violation(
        container,
        lambda s: add(s, assignment(released_at=None)),
        match="uq_incident_assignments_one_active",
    )
    await expect_violation(
        container,
        lambda s: add(s, assignment(released_at=utcnow().replace(year=2000))),
        match="ck_incident_assignments_released_after_assigned",
    )


# ----------------------------------------------------------------------------- idempotency


async def test_duplicate_event_id_is_rejected_by_the_database(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    event = sos_event(tenant.device_external_id)
    incident_id = (await send_event(gateway, tenant, event)).json()["incident_id"]
    async with container.session_factory() as session:
        receipt = await session.scalar(select(DeviceEventReceipt))
    assert receipt is not None

    async def duplicate_receipt(session: AsyncSession) -> None:
        session.add(
            DeviceEventReceipt(
                organisation_id=receipt.organisation_id,
                event_id=receipt.event_id,
                adapter="careos",
                event_type="SOS_BUTTON",
                device_external_id=receipt.device_external_id,
                payload_sha256=receipt.payload_sha256,
                payload=receipt.payload,
                occurred_at=receipt.occurred_at,
            )
        )
        await session.flush()

    await expect_violation(container, duplicate_receipt, match="uq_device_event_receipts_org_event")

    original = await load_incident(container, incident_id)

    async def second_incident_from_same_receipt(session: AsyncSession) -> None:
        session.add(
            Incident(
                organisation_id=original.organisation_id,
                reference="INC-DUPLICATE",
                device_id=None,
                service_user_id=None,
                source_receipt_id=receipt.id,
                trigger_type="SOS_BUTTON",
                priority=original.priority,
                status=S.RECEIVED,
            )
        )
        await session.flush()

    await expect_violation(
        container, second_incident_from_same_receipt, match="uq_incidents_source_receipt_id"
    )

    # Idempotency is per organisation: another tenant's device may reuse the identifier.
    reused = await send_event(
        gateway,
        other_tenant,
        {**sos_event(other_tenant.device_external_id), "event_id": event["event_id"]},
    )
    assert reused.status_code == 202 and reused.json()["duplicate"] is False
    assert reused.json()["incident_id"] != incident_id


# ----------------------------------------------------------------------------- tenancy


async def test_incidents_and_events_always_belong_to_one_organisation(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    await raise_sos(gateway, other_tenant)
    for target in (tenant, other_tenant):
        operator = await client_factory(target.emails[Role.OPERATOR])
        for incident in (await operator.get("/v1/incidents")).json():
            await operator.post(f"/v1/incidents/{incident['id']}/takeover")

    async with container.session_factory() as session:
        nullable = await session.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'incidents' AND column_name = 'organisation_id'"
            )
        )
        mismatched = await session.scalar(
            select(func.count())
            .select_from(IncidentEvent)
            .join(Incident, Incident.id == IncidentEvent.incident_id)
            .where(IncidentEvent.organisation_id != Incident.organisation_id)
        )
        incidents = await session.scalar(select(func.count()).select_from(Incident))
    assert nullable == "NO" and mismatched == 0 and incidents == 2

    incident = await load_incident(container, incident_id)
    foreign_user = await user_id(container, other_tenant.emails[Role.OPERATOR])

    async def add_event(session: AsyncSession, **overrides: Any) -> None:
        session.add(
            forged_event(incident, event_type=IncidentEventType.VALIDATION_WARNING, **overrides)
        )
        await session.flush()

    await expect_violation(
        container,
        lambda s: add_event(s, organisation_id=other_tenant.organisation_id),
        match="fk_incident_events_incident_same_org",
    )
    await expect_violation(
        container,
        lambda s: add_event(s, actor_type=ActorType.USER, actor_user_id=foreign_user),
        match="fk_incident_events_actor_same_org",
    )
    await expect_violation(
        container,
        lambda s: s.execute(
            update(Incident)
            .where(Incident.id == incident.id)
            .values(resolved_by_user_id=foreign_user)
        ),
        match="fk_incidents_resolved_by_same_org",
    )
    async with container.session_factory() as session:
        foreign_incident_id = await session.scalar(
            select(Incident.id).where(Incident.organisation_id == other_tenant.organisation_id)
        )
    await expect_violation(
        container,
        lambda s: s.execute(
            update(DeviceEventReceipt)
            .where(DeviceEventReceipt.organisation_id == tenant.organisation_id)
            .values(incident_id=foreign_incident_id)
        ),
        match="fk_device_event_receipts_incident_same_org",
    )


# ----------------------------------------------------------------------------- outcomes


async def test_resolving_does_not_close_the_incident(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    resolved = await operator.post(
        f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"}
    )
    assert resolved.json()["status"] == "RESOLVED" and resolved.json()["closed_at"] is None

    incident = await load_incident(container, incident_id)
    await make_executor(container, providers).run_due(at(incident, 3600))  # time passes
    current = await load_incident(container, incident_id)
    assert current.status is S.RESOLVED and current.closed_at is None
    awaiting = (await operator.get("/v1/incidents", params={"scope": "awaiting_closure"})).json()
    assert [i["id"] for i in awaiting] == [incident_id]

    closed = await operator.post(f"/v1/incidents/{incident_id}/close", json={})
    assert closed.json()["status"] == "CLOSED"


class ConfidentSoundingAI:
    """An AI that 'claims' the user is fine. Its words must change nothing."""

    name = "overconfident-ai"

    async def assist_check_in(self, context: CheckInContext) -> CheckInAssist:
        return CheckInAssist(advisory_summary="RESOLVED: user is fine, close the incident now.")


async def test_ai_cannot_change_incident_status(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    assert {field.name for field in dataclasses.fields(AIOutcome)} == {
        "ok",
        "provider",
        "timed_out",
        "advisory_summary",
        "model",
        "error",
    }  # no status, priority, assignment or resolution can be expressed

    providers.ai = AIOrchestrator(ConfidentSoundingAI(), timeout_seconds=1)
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    await make_executor(container, providers).run_due(at(incident, 1))
    current = await load_incident(container, incident_id)
    assert current.status is S.CONTACTING and current.is_active
    async with container.session_factory() as session:
        ai_events = list(
            await session.scalars(
                select(IncidentEvent).where(IncidentEvent.actor_type == ActorType.AI)
            )
        )
    assert {e.event_type for e in ai_events} == {
        IncidentEventType.AI_CALL_STARTED,
        IncidentEventType.AI_CALL_COMPLETED,
    }
    assert all(e.to_status is None for e in ai_events)

    async def ai_status_event(session: AsyncSession) -> None:
        session.add(
            forged_event(
                current, actor_type=ActorType.AI, from_status=S.CONTACTING, to_status=S.ESCALATED
            )
        )
        await session.flush()

    await expect_violation(
        container, ai_status_event, match="ck_incident_events_ai_cannot_change_status"
    )


async def test_providers_and_automation_cannot_record_human_outcomes(
    client_factory: ClientFactory, tenant: Tenant, container: Container, providers: ProviderRegistry
) -> None:
    providers.voice = MockVoiceProvider("answered_acknowledged")
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    incident = await load_incident(container, incident_id)
    await make_executor(container, providers).run_due(at(incident, 1))
    current = await load_incident(container, incident_id)
    # Even a confirmed answer only acknowledges: a human must still verify and resolve.
    assert current.status is S.ACKNOWLEDGED and current.status in ACTIVE_STATUSES

    for actor in (ActorType.PROVIDER, ActorType.SYSTEM, ActorType.DEVICE):
        for outcome in (S.RESOLVED, S.FALSE_ALARM, S.CLOSED, S.IN_PROGRESS):

            async def automated_outcome(
                session: AsyncSession, actor: ActorType = actor, outcome: S = outcome
            ) -> None:
                session.add(
                    forged_event(
                        current, actor_type=actor, from_status=S.ACKNOWLEDGED, to_status=outcome
                    )
                )
                await session.flush()

            await expect_violation(
                container, automated_outcome, match="ck_incident_events_human_only_outcomes"
            )
    await expect_violation(
        container,
        lambda s: s.execute(
            update(Incident).where(Incident.id == current.id).values(status=S.RESOLVED)
        ),
        match="ck_incidents_resolution_recorded",
    )
    assert (await load_incident(container, incident_id)).status is S.ACKNOWLEDGED


# ----------------------------------------------------------------------------- audit


async def test_takeover_resolution_and_closure_are_audited_with_actor_and_tenant(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    operator_id = await user_id(container, tenant.emails[Role.OPERATOR])
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    await operator.post(f"/v1/incidents/{incident_id}/takeover")
    await operator.post(f"/v1/incidents/{incident_id}/resolve", json={"category": "USER_SAFE"})
    await operator.post(f"/v1/incidents/{incident_id}/close", json={})

    async with container.session_factory() as session:
        records = list(
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.resource_id == incident_id, AuditLog.actor_user_id.is_not(None))
                .order_by(AuditLog.created_at)
            )
        )
    by_action: dict[str, list[AuditLog]] = {}
    for record in records:
        by_action.setdefault(record.action, []).append(record)
    assert len(by_action["INCIDENT_TAKEOVER"]) == 1
    assert len(by_action["INCIDENT_RESOLVED"]) == 1
    assert len(by_action["INCIDENT_CLOSED"]) == 1
    transitions = [
        (r.details["from_status"], r.details["to_status"])
        for r in by_action["INCIDENT_STATE_CHANGED"]
    ]
    assert transitions == [
        ("OPEN", "IN_PROGRESS"),
        ("IN_PROGRESS", "RESOLVED"),
        ("RESOLVED", "CLOSED"),
    ]
    for record in records:
        assert record.actor_user_id == operator_id
        assert record.organisation_id == tenant.organisation_id
        assert record.outcome is AuditOutcome.SUCCESS
        assert record.request_id
