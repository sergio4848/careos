"""Operator-facing voice escalation actions and views.

Safe controls only (ADR-016): an operator can STOP an automated call, TAKE OVER the
incident (existing) and bring the operator alert forward (ESCALATE NOW). There is no
"AI marked safe", no auto-resolve and no AI-driven close — manual resolution remains
the only path to RESOLVED/CLOSED.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from careos.contracts.realtime import RealtimeMessageType
from careos.core.errors import ConflictError, NotFoundError
from careos.core.logging import get_logger
from careos.core.time import utcnow
from careos.db.uow import UnitOfWork
from careos.modules.ai_orchestrator.models import AISession, AISessionStatus, UrgencySignal
from careos.modules.audit import service as audit
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.escalation_engine import scheduler
from careos.modules.escalation_engine.models import (
    EscalationActionType,
    ScheduledAction,
    ScheduledActionStatus,
)
from careos.modules.identity.principal import Principal
from careos.modules.incident_engine.models import ActorType, IncidentEventType
from careos.modules.incident_engine.service import IncidentEngine
from careos.modules.notification_engine.models import (
    TERMINAL_CALL_STATUSES,
    Call,
    CallStatus,
    CallTargetType,
    StructuredCallResponse,
)
from careos.modules.notification_engine.providers import VoiceProvider
from careos.modules.service_users.models import TrustedContact
from careos.modules.telephony.models import CallEvent, CallEventType
from careos.modules.telephony.store import event_row

log = get_logger(__name__)

AI_ADVISORY_LABEL = "AI ADVISORY — HUMAN REVIEW REQUIRED"


class VoiceAdvisoryView(BaseModel):
    status: AISessionStatus
    urgency_signal: UrgencySignal | None
    contact_established: bool | None
    requested_human_help: bool | None
    language: str | None
    summary: str | None
    provider: str
    disclaimer: str = AI_ADVISORY_LABEL


class VoiceCallView(BaseModel):
    id: uuid.UUID
    target_type: CallTargetType
    target_label: str
    to_number_masked: str | None
    provider: str
    direction: str
    status: CallStatus
    acknowledged: bool
    structured_response: StructuredCallResponse | None
    attempt: int
    started_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    failure_category: str | None
    ai: VoiceAdvisoryView | None


async def list_incident_calls(
    session: AsyncSession, principal: Principal, incident_id: uuid.UUID
) -> list[VoiceCallView]:
    # Tenant scope first: an unknown or foreign incident is indistinguishable (404).
    from careos.modules.incident_engine.models import Incident

    exists = await session.scalar(
        select(Incident.id).where(
            Incident.id == incident_id, Incident.organisation_id == principal.tenant_id
        )
    )
    if exists is None:
        raise NotFoundError()
    rows = (
        await session.execute(
            select(Call, TrustedContact.full_name, TrustedContact.relationship)
            .outerjoin(TrustedContact, TrustedContact.id == Call.trusted_contact_id)
            .where(Call.organisation_id == principal.tenant_id, Call.incident_id == incident_id)
            .order_by(Call.started_at)
        )
    ).all()
    ai_sessions = {
        s.call_id: s
        for s in await session.scalars(
            select(AISession).where(
                AISession.organisation_id == principal.tenant_id,
                AISession.incident_id == incident_id,
                AISession.call_id.is_not(None),
            )
        )
    }
    views: list[VoiceCallView] = []
    for call, contact_name, relationship in rows:
        if call.target_type is CallTargetType.SERVICE_USER:
            label = "Service user"
        else:
            label = f"Trusted contact · {contact_name or 'unknown'}"
            if relationship:
                label += f" ({relationship})"
        ai = ai_sessions.get(call.id)
        views.append(
            VoiceCallView(
                id=call.id,
                target_type=call.target_type,
                target_label=label,
                to_number_masked=call.to_number_masked,
                provider=call.provider,
                direction=call.direction,
                status=call.status,
                acknowledged=call.acknowledged,
                structured_response=call.structured_response,
                attempt=call.attempt,
                started_at=call.started_at,
                answered_at=call.answered_at,
                ended_at=call.ended_at,
                duration_seconds=call.duration_seconds,
                failure_category=call.failure_category,
                ai=VoiceAdvisoryView(
                    status=ai.status,
                    urgency_signal=ai.urgency_signal,
                    contact_established=ai.contact_established,
                    requested_human_help=ai.requested_human_help,
                    language=ai.language,
                    summary=ai.advisory_summary,
                    provider=ai.provider,
                )
                if ai is not None
                else None,
            )
        )
    return views


async def stop_automated_call(
    uow: UnitOfWork,
    principal: Principal,
    incident_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    voice_provider: VoiceProvider,
    context: AuditContext,
) -> None:
    """Operator STOP: cancel one automated call. Never touches incident status."""
    session = uow.session
    incident = await IncidentEngine.lock(session, principal.tenant_id, incident_id)
    call = await session.scalar(
        select(Call)
        .where(
            Call.id == call_id,
            Call.organisation_id == principal.tenant_id,
            Call.incident_id == incident_id,
        )
        .with_for_update()
    )
    if call is None:
        raise NotFoundError()
    if call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError("This call has already ended.")
    call.status = CallStatus.CANCELLED
    call.ended_at = utcnow()
    call.failure_category = "operator_cancelled"
    await session.execute(
        pg_insert(CallEvent).values(
            event_row(
                organisation_id=call.organisation_id,
                incident_id=call.incident_id,
                call_id=call.id,
                event_type=CallEventType.CALL_CANCELLED,
                data={"by": "operator"},
            )
        )
    )
    IncidentEngine.append_event(
        session,
        incident,
        IncidentEventType.AUTOMATED_CALL_CANCELLED,
        f"{principal.full_name} stopped the automated call",
        actor_type=ActorType.USER,
        actor_user_id=principal.user_id,
        data={"call_id": str(call.id)},
    )
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.CALL_CANCELLED_BY_OPERATOR,
            resource_type="incident",
            resource_id=str(incident_id),
            details={"call_id": str(call.id)},
        ),
        context,
    )
    sid = call.provider_call_sid
    IncidentEngine.notify(
        uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "AUTOMATED_CALL_CANCELLED"
    )
    cancel = getattr(voice_provider, "cancel_call", None)
    if sid and cancel is not None:
        # Best-effort provider hangup after commit; the DB cancel already ends the
        # executor's wait, and Twilio's own TimeLimit bounds the call regardless.
        def _hangup() -> None:
            task = asyncio.ensure_future(_swallow(cancel(sid)))
            _HANGUP_TASKS.add(task)
            task.add_done_callback(_HANGUP_TASKS.discard)

        uow.after_commit(_hangup)
    await uow.commit()


async def accelerate_operator_escalation(
    uow: UnitOfWork, principal: Principal, incident_id: uuid.UUID, *, context: AuditContext
) -> int:
    """Operator ESCALATE NOW: pull the operator-alert step forward to run immediately."""
    session = uow.session
    incident = await IncidentEngine.lock(session, principal.tenant_id, incident_id)
    if not incident.is_active:
        raise ConflictError("Only active incidents can be escalated.")
    result = await session.execute(
        update(ScheduledAction)
        .where(
            ScheduledAction.incident_id == incident.id,
            ScheduledAction.action_type == EscalationActionType.OPERATOR_ESCALATION,
            ScheduledAction.status == ScheduledActionStatus.PENDING,
        )
        .values(due_at=utcnow(), updated_at=utcnow())
        .returning(ScheduledAction.id)
    )
    accelerated = len(result.all())
    if accelerated == 0:
        await scheduler.ensure_operator_alert_now(session, incident)
        accelerated = 1
    IncidentEngine.append_event(
        session,
        incident,
        IncidentEventType.OPERATOR_ESCALATION_REQUESTED,
        f"{principal.full_name} requested immediate operator escalation",
        actor_type=ActorType.USER,
        actor_user_id=principal.user_id,
    )
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.ESCALATION_ACCELERATED,
            resource_type="incident",
            resource_id=str(incident_id),
        ),
        context,
    )
    IncidentEngine.notify(
        uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "OPERATOR_ESCALATION_REQUESTED"
    )
    await uow.commit()
    return accelerated


_HANGUP_TASKS: set[asyncio.Task[None]] = set()


async def _swallow(operation: object) -> None:
    with contextlib.suppress(Exception):
        await operation  # type: ignore[misc]
