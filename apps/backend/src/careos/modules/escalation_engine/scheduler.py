"""Durable escalation scheduling (DB-backed timers, see ADR-007).

All functions here run inside the caller's transaction so that scheduling commits
atomically with the incident change that caused it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from careos.core.time import utcnow
from careos.modules.escalation_engine.models import (
    FAILSAFE_STEP_ORDER,
    EscalationActionType,
    EscalationPolicy,
    ScheduledAction,
    ScheduledActionStatus,
)
from careos.modules.escalation_engine.policies import (
    FALLBACK_STEPS,
    StepSpec,
    resolve_policy,
    steps_of,
)
from careos.modules.incident_engine.models import Incident
from careos.modules.service_users.models import ServiceUser


async def schedule_for_incident(
    session: AsyncSession,
    incident: Incident,
    service_user: ServiceUser | None,
    *,
    max_attempts: int,
    use_fallback: bool = False,
) -> tuple[EscalationPolicy | None, list[StepSpec]]:
    policy = (
        None
        if use_fallback
        else await resolve_policy(session, incident.organisation_id, service_user)
    )
    specs = steps_of(policy) if policy is not None else list(FALLBACK_STEPS)
    for spec in specs:
        session.add(
            ScheduledAction(
                organisation_id=incident.organisation_id,
                incident_id=incident.id,
                escalation_step_id=spec.escalation_step_id,
                step_order=spec.step_order,
                action_type=spec.action_type,
                contact_priority=spec.contact_priority,
                delay_seconds=spec.delay_seconds,
                due_at=incident.created_at + timedelta(seconds=spec.delay_seconds),
                status=ScheduledActionStatus.PENDING,
                max_attempts=max_attempts,
            )
        )
    return policy, specs


async def cancel_pending_actions(
    session: AsyncSession,
    incident_id: uuid.UUID,
    *,
    reason: str,
    action_types: Iterable[EscalationActionType] | None = None,
) -> int:
    """Cancel not-yet-started steps.

    Steps already RUNNING re-check the incident state when they record their outcome.
    """
    stmt = update(ScheduledAction).where(
        ScheduledAction.incident_id == incident_id,
        ScheduledAction.status == ScheduledActionStatus.PENDING,
    )
    if action_types is not None:
        stmt = stmt.where(ScheduledAction.action_type.in_(list(action_types)))
    result = await session.execute(
        stmt.values(
            status=ScheduledActionStatus.CANCELLED,
            last_error=reason,
            completed_at=utcnow(),
            updated_at=utcnow(),
        ).returning(ScheduledAction.id)
    )
    return len(result.all())


async def ensure_operator_alert_now(session: AsyncSession, incident: Incident) -> None:
    """Fail-safe: bring a human in immediately when automation fails."""
    now = utcnow()
    pending = await session.scalar(
        select(ScheduledAction)
        .where(
            ScheduledAction.incident_id == incident.id,
            ScheduledAction.action_type == EscalationActionType.OPERATOR_ESCALATION,
            ScheduledAction.status == ScheduledActionStatus.PENDING,
        )
        .order_by(ScheduledAction.due_at)
        .limit(1)
        .with_for_update()
    )
    if pending is not None:
        pending.due_at = min(pending.due_at, now)
        return
    await session.execute(
        pg_insert(ScheduledAction)
        .values(
            id=uuid.uuid4(),
            organisation_id=incident.organisation_id,
            incident_id=incident.id,
            step_order=FAILSAFE_STEP_ORDER,
            action_type=EscalationActionType.OPERATOR_ESCALATION,
            delay_seconds=max(0, int((now - incident.created_at).total_seconds())),
            due_at=now,
            status=ScheduledActionStatus.PENDING,
            attempts=0,
            max_attempts=5,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_scheduled_actions_incident_step")
    )


def describe_action(
    action_type: EscalationActionType,
    contact_priority: int | None,
    *,
    contact_name: str | None = None,
    service_user_name: str | None = None,
) -> str:
    if action_type == EscalationActionType.AUTOMATED_USER_CONTACT:
        return f"Automated welfare call to {service_user_name or 'service user'}"
    if action_type == EscalationActionType.CALL_TRUSTED_CONTACT:
        suffix = f" · {contact_name}" if contact_name else ""
        return f"Call trusted contact #{contact_priority}{suffix}"
    if action_type == EscalationActionType.NOTIFY_TRUSTED_CONTACT:
        suffix = f" · {contact_name}" if contact_name else ""
        return f"Notify trusted contact #{contact_priority}{suffix}"
    return "Alert on-duty operators"
