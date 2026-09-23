"""Escalation Executor (runs in the worker).

Each due ``ScheduledAction`` is executed in three phases so no database lock is held
while talking to an external provider:

1. **prepare** (short transaction, incident row locked): re-check the incident is still
   active, resolve the target, record ``Call``/events, move OPEN -> CONTACTING.
2. **provider call** (no transaction): bounded by ``provider_timeout_seconds``.
3. **record** (short transaction, incident row locked): store the outcome, apply the
   state machine (e.g. CONTACT_ACKNOWLEDGED -> ACKNOWLEDGED), complete the action.

Failures are retried with exponential backoff; when retries are exhausted a fail-safe
operator alert is scheduled immediately. Delivery is at-least-once: a worker crash
between phases 2 and 3 can repeat a call, which is preferable to missing one.

Ordering: steps of one incident run one at a time in ``step_order`` (a worker that was down
catches up in policy order instead of firing every overdue step at once). Operator alerts are
exempt: bringing in a human is never held back behind an automated contact attempt.

Provider failures are recorded (timeline, call row, metrics, logs) and escalate towards
humans. They never delete, roll back, resolve or close an incident.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from careos.contracts.realtime import RealtimeMessageType, RealtimePublisher
from careos.core.config import Settings
from careos.core.logging import get_logger
from careos.core.metrics import (
    ESCALATION_ACTIONS,
    ESCALATION_FAILURES,
    PROVIDER_CALLS,
    PROVIDER_FAILURES,
)
from careos.core.time import utcnow
from careos.db.uow import UnitOfWork
from careos.modules.ai_orchestrator.models import AISession, AISessionPurpose, AISessionStatus
from careos.modules.ai_orchestrator.orchestrator import AIOrchestrator, CheckInContext
from careos.modules.escalation_engine import scheduler
from careos.modules.escalation_engine.models import (
    AUTOMATED_CONTACT_ACTIONS,
    EscalationActionType,
    ScheduledAction,
    ScheduledActionStatus,
)
from careos.modules.incident_engine.models import ActorType, Incident, IncidentEventType
from careos.modules.incident_engine.service import IncidentEngine
from careos.modules.incident_engine.state_machine import IncidentStatus
from careos.modules.notification_engine.models import (
    Call,
    CallStatus,
    CallTargetType,
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from careos.modules.notification_engine.providers import (
    NotificationProvider,
    NotificationRequest,
    ProviderError,
    VoiceCallOutcome,
    VoiceCallRequest,
    VoiceCallResult,
    VoiceProvider,
)
from careos.modules.service_users.models import ServiceUser, TrustedContact

log = get_logger(__name__)

_ESCALATE_FROM = frozenset(
    {
        IncidentStatus.OPEN,
        IncidentStatus.CONTACTING,
        IncidentStatus.FAILED,
        IncidentStatus.DEVICE_ERROR,
    }
)
_ACKNOWLEDGE_FROM = frozenset(
    {IncidentStatus.OPEN, IncidentStatus.CONTACTING, IncidentStatus.ESCALATED}
)


@dataclass(frozen=True, slots=True)
class ClaimedAction:
    id: uuid.UUID
    organisation_id: uuid.UUID
    incident_id: uuid.UUID
    action_type: EscalationActionType
    contact_priority: int | None
    step_order: int
    attempts: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class _Target:
    kind: CallTargetType
    name: str
    phone: str
    service_user_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    relationship: str | None = None


def failure_category(exc: BaseException) -> str:
    """Low-cardinality failure category for metrics and logs (never the error message)."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ProviderError):
        return "provider_error"
    return "unexpected"


class _SkipAction(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EscalationExecutor:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: RealtimePublisher,
        voice: VoiceProvider,
        notifications: NotificationProvider,
        ai: AIOrchestrator,
        worker_id: str,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._publisher = publisher
        self._voice = voice
        self._notifications = notifications
        self._ai = ai
        self._worker_id = worker_id
        self._engine = IncidentEngine(escalation_max_attempts=settings.escalation_max_attempts)

    # ------------------------------------------------------------------ claiming

    async def run_due(self, now: datetime | None = None) -> int:
        claimed = await self.claim(now or utcnow())
        if not claimed:
            return 0
        semaphore = asyncio.Semaphore(10)

        async def run(action: ClaimedAction) -> None:
            async with semaphore:
                await self.execute(action)

        await asyncio.gather(*(run(action) for action in claimed))
        return len(claimed)

    async def claim(self, now: datetime) -> list[ClaimedAction]:
        """Atomically lease due actions. ``SKIP LOCKED`` lets many workers run safely.

        A step is claimable only when no earlier step of the same incident is running or
        due (operator alerts excepted), so catch-up after downtime keeps the policy order.
        """
        earlier = aliased(ScheduledAction)
        earlier_step_open = exists().where(
            earlier.incident_id == ScheduledAction.incident_id,
            earlier.step_order < ScheduledAction.step_order,
            or_(
                earlier.status == ScheduledActionStatus.RUNNING,
                and_(
                    earlier.status == ScheduledActionStatus.PENDING,
                    earlier.due_at <= now,
                ),
            ),
        )
        due = (
            select(ScheduledAction.id)
            .where(
                or_(
                    and_(
                        ScheduledAction.status == ScheduledActionStatus.PENDING,
                        ScheduledAction.due_at <= now,
                    ),
                    and_(
                        ScheduledAction.status == ScheduledActionStatus.RUNNING,
                        ScheduledAction.lease_expires_at < now,
                    ),
                ),
                or_(
                    ScheduledAction.action_type == EscalationActionType.OPERATOR_ESCALATION,
                    ~earlier_step_open,
                ),
            )
            .order_by(ScheduledAction.due_at, ScheduledAction.step_order)
            .limit(self._settings.worker_batch_size)
            .with_for_update(skip_locked=True)
        )
        async with self._session_factory() as session:
            result = await session.execute(
                update(ScheduledAction)
                .where(ScheduledAction.id.in_(due.scalar_subquery()))
                .values(
                    status=ScheduledActionStatus.RUNNING,
                    attempts=ScheduledAction.attempts + 1,
                    lease_expires_at=now + timedelta(seconds=self._settings.worker_lease_seconds),
                    locked_by=self._worker_id,
                    updated_at=now,
                )
                .returning(
                    ScheduledAction.id,
                    ScheduledAction.organisation_id,
                    ScheduledAction.incident_id,
                    ScheduledAction.action_type,
                    ScheduledAction.contact_priority,
                    ScheduledAction.step_order,
                    ScheduledAction.attempts,
                    ScheduledAction.max_attempts,
                )
            )
            rows = result.all()
            await session.commit()
        return [ClaimedAction(*row) for row in rows]

    # ------------------------------------------------------------------ execution

    async def execute(self, action: ClaimedAction) -> None:
        try:
            if action.action_type == EscalationActionType.OPERATOR_ESCALATION:
                await self._alert_operators(action)
            elif action.action_type == EscalationActionType.NOTIFY_TRUSTED_CONTACT:
                await self._notify_contact(action)
            else:
                await self._call(action)
            ESCALATION_ACTIONS.labels(action.action_type.value, "completed").inc()
        except _SkipAction as skip:
            ESCALATION_ACTIONS.labels(action.action_type.value, "skipped").inc()
            log.info(
                "escalation.skipped",
                action_id=str(action.id),
                incident_id=str(action.incident_id),
                organisation_id=str(action.organisation_id),
                reason=skip.reason,
            )
        except Exception as exc:
            ESCALATION_ACTIONS.labels(action.action_type.value, "error").inc()
            log.exception(
                "escalation.action_error",
                action_id=str(action.id),
                incident_id=str(action.incident_id),
                organisation_id=str(action.organisation_id),
                failure_category="unexpected",
            )
            await self._record_failure_safely(action, error=type(exc).__name__, call_id=None)

    async def _record_failure_safely(
        self, action: ClaimedAction, *, error: str, call_id: uuid.UUID | None
    ) -> None:
        """Record a failed attempt. If even that fails (e.g. database outage) the action stays
        RUNNING and is reclaimed when its lease expires, so the step is never silently lost."""
        try:
            await self._handle_failure(action, error=error, call_id=call_id)
        except Exception:
            ESCALATION_FAILURES.labels(action.action_type.value, "record_failed").inc()
            log.exception(
                "escalation.failure_not_recorded",
                action_id=str(action.id),
                incident_id=str(action.incident_id),
                organisation_id=str(action.organisation_id),
                recovery="lease_expiry",
            )

    async def _begin(
        self, session: AsyncSession, action: ClaimedAction
    ) -> tuple[Incident, ScheduledAction]:
        """Lock incident + action; skip if the action is no longer ours or no longer needed."""
        incident = await IncidentEngine.lock(session, action.organisation_id, action.incident_id)
        scheduled = await session.scalar(
            select(ScheduledAction).where(ScheduledAction.id == action.id).with_for_update()
        )
        if (
            scheduled is None
            or scheduled.status != ScheduledActionStatus.RUNNING
            or scheduled.locked_by != self._worker_id
        ):
            raise _SkipAction("lease_lost")
        if action.attempts > action.max_attempts:
            # A worker crashed mid-action repeatedly: stop retrying and bring in a human.
            self._finish(scheduled, ScheduledActionStatus.FAILED, "attempts_exhausted")
            if (
                incident.is_active
                and action.action_type != EscalationActionType.OPERATOR_ESCALATION
            ):
                await scheduler.ensure_operator_alert_now(session, incident)
            await session.commit()
            raise _SkipAction("attempts_exhausted")
        reason: str | None = None
        if not incident.is_active:
            reason = "incident_no_longer_active"
        elif (
            action.action_type in AUTOMATED_CONTACT_ACTIONS
            and incident.status == IncidentStatus.IN_PROGRESS
        ):
            reason = "operator_in_control"
        if reason is not None:
            self._finish(scheduled, ScheduledActionStatus.SKIPPED, reason)
            await session.commit()
            raise _SkipAction(reason)
        return incident, scheduled

    @staticmethod
    def _finish(
        scheduled: ScheduledAction, status: ScheduledActionStatus, note: str | None = None
    ) -> None:
        now = utcnow()
        scheduled.status = status
        scheduled.completed_at = now
        scheduled.lease_expires_at = None
        scheduled.last_error = note
        scheduled.updated_at = now

    def _uow(self, session: AsyncSession) -> UnitOfWork:
        return UnitOfWork(
            session,
            self._publisher,
            publish_timeout_seconds=self._settings.realtime_publish_timeout_seconds,
        )

    def _provider_failed(
        self, action: ClaimedAction, kind: str, provider: str, category: str
    ) -> None:
        PROVIDER_CALLS.labels(kind, provider, "error").inc()
        PROVIDER_FAILURES.labels(kind, provider, category).inc()
        log.warning(
            "escalation.provider_failed",
            provider_kind=kind,
            provider=provider,
            failure_category=category,
            action_id=str(action.id),
            action_type=action.action_type.value,
            incident_id=str(action.incident_id),
            organisation_id=str(action.organisation_id),
            attempt=action.attempts,
        )

    # ------------------------------------------------------------------ OPERATOR_ESCALATION

    async def _alert_operators(self, action: ClaimedAction) -> None:
        async with self._session_factory() as session:
            uow = self._uow(session)
            incident, scheduled = await self._begin(session, action)
            if incident.status in _ESCALATE_FROM:
                self._engine.transition(
                    session,
                    incident,
                    IncidentStatus.ESCALATED,
                    IncidentEventType.OPERATORS_ALERTED,
                    "On-duty operators alerted: human response required",
                    data={"step_order": action.step_order},
                )
            else:
                self._engine.append_event(
                    session,
                    incident,
                    IncidentEventType.OPERATORS_ALERTED,
                    "On-duty operators alerted to verify the response",
                    data={"step_order": action.step_order},
                )
            now = utcnow()
            session.add(
                Notification(
                    organisation_id=incident.organisation_id,
                    incident_id=incident.id,
                    channel=NotificationChannel.IN_APP,
                    recipient_type="ROLE",
                    recipient_ref="OPERATOR",
                    template_key="incident.operators_alerted",
                    provider="in-app",
                    status=NotificationStatus.SENT,
                    sent_at=now,
                )
            )
            self._finish(scheduled, ScheduledActionStatus.COMPLETED)
            self._engine.notify(
                uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "OPERATORS_ALERTED"
            )
            await uow.commit()

    # ------------------------------------------------------------------ calls

    async def _resolve_target(
        self, session: AsyncSession, incident: Incident, action: ClaimedAction
    ) -> _Target | None:
        if incident.service_user_id is None:
            return None
        if action.action_type == EscalationActionType.AUTOMATED_USER_CONTACT:
            su = await session.get(ServiceUser, incident.service_user_id)
            if su is None or not su.phone_number:
                return None
            return _Target(
                kind=CallTargetType.SERVICE_USER,
                name=su.display_name,
                phone=su.phone_number,
                service_user_id=su.id,
            )
        contact = await session.scalar(
            select(TrustedContact).where(
                TrustedContact.service_user_id == incident.service_user_id,
                TrustedContact.organisation_id == incident.organisation_id,
                TrustedContact.priority == action.contact_priority,
                TrustedContact.is_active.is_(True),
                TrustedContact.deleted_at.is_(None),
            )
        )
        if contact is None:
            return None
        return _Target(
            kind=CallTargetType.TRUSTED_CONTACT,
            name=contact.full_name,
            phone=contact.phone_number,
            contact_id=contact.id,
            relationship=contact.relationship,
        )

    async def _skip_missing_target(
        self,
        session: AsyncSession,
        uow: UnitOfWork,
        incident: Incident,
        scheduled: ScheduledAction,
        action: ClaimedAction,
    ) -> None:
        what = (
            "service user phone number"
            if action.action_type == EscalationActionType.AUTOMATED_USER_CONTACT
            else f"active trusted contact #{action.contact_priority}"
        )
        self._engine.append_event(
            session,
            incident,
            IncidentEventType.ESCALATION_STEP_SKIPPED,
            f"Step {action.step_order} skipped: no {what}",
            data={"step_order": action.step_order, "action_type": action.action_type.value},
        )
        self._finish(scheduled, ScheduledActionStatus.SKIPPED, "target_missing")
        self._engine.notify(
            uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "ESCALATION_STEP_SKIPPED"
        )
        await uow.commit()
        raise _SkipAction("target_missing")

    async def _call(self, action: ClaimedAction) -> None:
        automated = action.action_type == EscalationActionType.AUTOMATED_USER_CONTACT
        # Phase 1: prepare
        async with self._session_factory() as session:
            uow = self._uow(session)
            incident, scheduled = await self._begin(session, action)
            target = await self._resolve_target(session, incident, action)
            if target is None:
                await self._skip_missing_target(session, uow, incident, scheduled, action)
                return
            call = Call(
                id=uuid.uuid4(),
                organisation_id=incident.organisation_id,
                incident_id=incident.id,
                scheduled_action_id=action.id,
                target_type=target.kind,
                trusted_contact_id=target.contact_id,
                service_user_id=target.service_user_id,
                provider=self._voice.name,
                status=CallStatus.INITIATED,
                attempt=action.attempts,
                started_at=utcnow(),
            )
            session.add(call)
            attempt_note = (
                f" (attempt {action.attempts}/{action.max_attempts})" if action.attempts > 1 else ""
            )
            if automated:
                event_type = IncidentEventType.AUTOMATED_CALL_STARTED
                message = f"Automated welfare call to {target.name}{attempt_note}"
            else:
                event_type = IncidentEventType.TRUSTED_CONTACT_CALLED
                message = (
                    f"Calling trusted contact #{action.contact_priority} · {target.name} "
                    f"({target.relationship}){attempt_note}"
                )
            data = {
                "call_id": str(call.id),
                "step_order": action.step_order,
                "attempt": action.attempts,
            }
            if incident.status == IncidentStatus.OPEN:
                self._engine.transition(
                    session, incident, IncidentStatus.CONTACTING, event_type, message, data=data
                )
            else:
                self._engine.append_event(session, incident, event_type, message, data=data)
            self._engine.notify(
                uow, incident, RealtimeMessageType.INCIDENT_UPDATED, event_type.value
            )
            trigger_type = incident.trigger_type
            await uow.commit()

        # Optional AI assistance runs alongside the call. It cannot delay, block or change the
        # deterministic flow: the call is placed and its outcome recorded independently of it.
        ai_task = (
            asyncio.create_task(self._run_ai_assist_isolated(action, trigger_type))
            if automated and self._ai.enabled
            else None
        )
        try:
            # Phase 2: provider call, outside any transaction
            try:
                async with asyncio.timeout(self._settings.provider_timeout_seconds):
                    result = await self._voice.place_call(
                        VoiceCallRequest(
                            organisation_id=action.organisation_id,
                            incident_id=action.incident_id,
                            call_id=call.id,
                            to_number=target.phone,
                            purpose=(
                                "AUTOMATED_WELFARE_CHECK" if automated else "TRUSTED_CONTACT_ALERT"
                            ),
                        )
                    )
            except Exception as exc:  # any provider failure is recorded, never propagated
                category = failure_category(exc)
                self._provider_failed(action, "voice", self._voice.name, category)
                await self._record_failure_safely(action, error=category, call_id=call.id)
                return
            PROVIDER_CALLS.labels("voice", self._voice.name, "ok").inc()

            # Phase 3: record outcome
            await self._record_call_result(action, call.id, target, result, automated)
        finally:
            if ai_task is not None:
                await ai_task

    async def _run_ai_assist_isolated(self, action: ClaimedAction, trigger_type: str) -> None:
        try:
            await self._run_ai_assist(action, trigger_type)
        except Exception:  # AI bookkeeping must never affect the deterministic escalation
            log.exception(
                "escalation.ai_assist_error",
                action_id=str(action.id),
                incident_id=str(action.incident_id),
                organisation_id=str(action.organisation_id),
                provider=self._ai.provider_name,
                failure_category="unexpected",
            )

    async def _record_call_result(
        self,
        action: ClaimedAction,
        call_id: uuid.UUID,
        target: _Target,
        result: VoiceCallResult,
        automated: bool,
    ) -> None:
        async with self._session_factory() as session:
            uow = self._uow(session)
            incident = await IncidentEngine.lock(
                session, action.organisation_id, action.incident_id
            )
            scheduled = await session.get(ScheduledAction, action.id, with_for_update=True)
            call = await session.get(Call, call_id)
            now = utcnow()
            if call is not None:
                call.status = CallStatus(result.outcome.value)
                call.acknowledged = result.acknowledged
                call.provider_call_id = result.provider_call_id
                call.ended_at = now
            data = {"call_id": str(call_id), "outcome": result.outcome.value}

            if result.outcome == VoiceCallOutcome.ANSWERED and result.acknowledged:
                self._engine.append_event(
                    session,
                    incident,
                    IncidentEventType.AUTOMATED_CALL_ANSWERED
                    if automated
                    else IncidentEventType.TRUSTED_CONTACT_CALLED,
                    f"{target.name} answered",
                    actor_type=ActorType.PROVIDER,
                    data=data,
                )
                ack_message = (
                    f"{target.name} confirmed via automated call"
                    if automated
                    else f"{target.name} acknowledged and is responding"
                )
                if incident.status in _ACKNOWLEDGE_FROM:
                    self._engine.transition(
                        session,
                        incident,
                        IncidentStatus.ACKNOWLEDGED,
                        IncidentEventType.CONTACT_ACKNOWLEDGED,
                        ack_message,
                        actor_type=ActorType.PROVIDER,
                        data=data,
                    )
                    incident.acknowledged_at = incident.acknowledged_at or now
                    cancelled = await scheduler.cancel_pending_actions(
                        session,
                        incident.id,
                        reason="contact_acknowledged",
                        action_types=AUTOMATED_CONTACT_ACTIONS,
                    )
                    if cancelled:
                        self._engine.append_event(
                            session,
                            incident,
                            IncidentEventType.ESCALATION_HALTED,
                            f"Further contact attempts cancelled ({cancelled}); "
                            "operator verification still required",
                            data={"reason": "contact_acknowledged", "cancelled_steps": cancelled},
                        )
                else:
                    self._engine.append_event(
                        session,
                        incident,
                        IncidentEventType.CONTACT_ACKNOWLEDGED,
                        ack_message,
                        actor_type=ActorType.PROVIDER,
                        data=data,
                    )
            elif result.outcome == VoiceCallOutcome.ANSWERED:
                self._engine.append_event(
                    session,
                    incident,
                    IncidentEventType.AUTOMATED_CALL_ANSWERED
                    if automated
                    else IncidentEventType.TRUSTED_CONTACT_NO_ANSWER,
                    f"{target.name} answered but did not confirm",
                    actor_type=ActorType.PROVIDER,
                    data=data,
                )
            else:
                self._engine.append_event(
                    session,
                    incident,
                    IncidentEventType.AUTOMATED_CALL_NO_ANSWER
                    if automated
                    else IncidentEventType.TRUSTED_CONTACT_NO_ANSWER,
                    f"No answer from {target.name}"
                    + (" (busy)" if result.outcome == VoiceCallOutcome.BUSY else ""),
                    actor_type=ActorType.PROVIDER,
                    data=data,
                )
            if scheduled is not None and scheduled.status == ScheduledActionStatus.RUNNING:
                self._finish(scheduled, ScheduledActionStatus.COMPLETED)
            self._engine.notify(uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "CALL_RESULT")
            await uow.commit()

    async def _run_ai_assist(self, action: ClaimedAction, trigger_type: str) -> None:
        async with self._session_factory() as session:
            uow = self._uow(session)
            incident = await IncidentEngine.lock(
                session, action.organisation_id, action.incident_id
            )
            ai_session = AISession(
                id=uuid.uuid4(),
                organisation_id=incident.organisation_id,
                incident_id=incident.id,
                purpose=AISessionPurpose.AUTOMATED_CHECK_IN,
                provider=self._ai.provider_name,
                status=AISessionStatus.STARTED,
                started_at=utcnow(),
            )
            session.add(ai_session)
            self._engine.append_event(
                session,
                incident,
                IncidentEventType.AI_CALL_STARTED,
                "AI check-in assistant started (advisory only)",
                actor_type=ActorType.AI,
                data={"ai_session_id": str(ai_session.id)},
            )
            self._engine.notify(
                uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "AI_CALL_STARTED"
            )
            await uow.commit()

        outcome = await self._ai.assist_check_in(
            CheckInContext(
                organisation_id=action.organisation_id,
                incident_id=action.incident_id,
                trigger_type=trigger_type,
            )
        )

        async with self._session_factory() as session:
            uow = self._uow(session)
            incident = await IncidentEngine.lock(
                session, action.organisation_id, action.incident_id
            )
            stored = await session.get(AISession, ai_session.id)
            if stored is not None:
                stored.ended_at = utcnow()
                stored.model = outcome.model
                if outcome.ok:
                    stored.status = AISessionStatus.COMPLETED
                    stored.advisory_summary = outcome.advisory_summary
                else:
                    stored.status = (
                        AISessionStatus.TIMED_OUT if outcome.timed_out else AISessionStatus.FAILED
                    )
                    stored.failure_reason = outcome.error
            if outcome.ok:
                self._engine.append_event(
                    session,
                    incident,
                    IncidentEventType.AI_CALL_COMPLETED,
                    "AI check-in assistant finished (advisory note attached)",
                    actor_type=ActorType.AI,
                    data={
                        "ai_session_id": str(ai_session.id),
                        "advisory": True,
                        "advisory_summary": outcome.advisory_summary,
                    },
                )
            else:
                self._engine.append_event(
                    session,
                    incident,
                    IncidentEventType.AI_CALL_FAILED,
                    "AI assistant unavailable; continuing with standard automated call",
                    actor_type=ActorType.AI,
                    data={"ai_session_id": str(ai_session.id), "error": outcome.error},
                )
            self._engine.notify(uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "AI_RESULT")
            await uow.commit()

    # ------------------------------------------------------------------ notifications

    async def _notify_contact(self, action: ClaimedAction) -> None:
        async with self._session_factory() as session:
            uow = self._uow(session)
            incident, scheduled = await self._begin(session, action)
            target = await self._resolve_target(session, incident, action)
            if target is None:
                await self._skip_missing_target(session, uow, incident, scheduled, action)
                return
            reference = incident.reference
            await session.commit()

        try:
            async with asyncio.timeout(self._settings.provider_timeout_seconds):
                result = await self._notifications.send(
                    NotificationRequest(
                        organisation_id=action.organisation_id,
                        incident_id=action.incident_id,
                        channel=NotificationChannel.SMS,
                        to=target.phone,
                        template_key="incident.trusted_contact_alert",
                        variables={"reference": reference},
                    )
                )
        except Exception as exc:  # any provider failure is recorded, never propagated
            category = failure_category(exc)
            self._provider_failed(action, "notification", self._notifications.name, category)
            await self._record_failure_safely(action, error=category, call_id=None)
            return
        PROVIDER_CALLS.labels("notification", self._notifications.name, "ok").inc()

        async with self._session_factory() as session:
            uow = self._uow(session)
            incident = await IncidentEngine.lock(
                session, action.organisation_id, action.incident_id
            )
            stored = await session.get(ScheduledAction, action.id, with_for_update=True)
            session.add(
                Notification(
                    organisation_id=incident.organisation_id,
                    incident_id=incident.id,
                    channel=NotificationChannel.SMS,
                    recipient_type="TRUSTED_CONTACT",
                    recipient_ref=str(target.contact_id),
                    template_key="incident.trusted_contact_alert",
                    provider=self._notifications.name,
                    provider_message_id=result.provider_message_id,
                    status=NotificationStatus.SENT,
                    attempts=action.attempts,
                    sent_at=utcnow(),
                )
            )
            if incident.status == IncidentStatus.OPEN:
                self._engine.transition(
                    session,
                    incident,
                    IncidentStatus.CONTACTING,
                    IncidentEventType.TRUSTED_CONTACT_NOTIFIED,
                    f"SMS alert sent to trusted contact #{action.contact_priority} · {target.name}",
                )
            else:
                self._engine.append_event(
                    session,
                    incident,
                    IncidentEventType.TRUSTED_CONTACT_NOTIFIED,
                    f"SMS alert sent to trusted contact #{action.contact_priority} · {target.name}",
                )
            if stored is not None and stored.status == ScheduledActionStatus.RUNNING:
                self._finish(stored, ScheduledActionStatus.COMPLETED)
            self._engine.notify(
                uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "TRUSTED_CONTACT_NOTIFIED"
            )
            await uow.commit()

    # ------------------------------------------------------------------ failure handling

    async def _handle_failure(
        self, action: ClaimedAction, *, error: str, call_id: uuid.UUID | None
    ) -> None:
        async with self._session_factory() as session:
            uow = self._uow(session)
            incident = await IncidentEngine.lock(
                session, action.organisation_id, action.incident_id
            )
            scheduled = await session.get(ScheduledAction, action.id, with_for_update=True)
            now = utcnow()
            if call_id is not None:
                call = await session.get(Call, call_id)
                if call is not None:
                    call.status = CallStatus.FAILED
                    call.failure_reason = error
                    call.ended_at = now
            if scheduled is None or scheduled.status != ScheduledActionStatus.RUNNING:
                await session.commit()
                return

            retrying = action.attempts < action.max_attempts
            ESCALATION_FAILURES.labels(
                action.action_type.value, "retrying" if retrying else "exhausted"
            ).inc()
            failure_event = (
                IncidentEventType.NOTIFICATION_FAILED
                if action.action_type == EscalationActionType.NOTIFY_TRUSTED_CONTACT
                else IncidentEventType.CALL_FAILED
            )
            self._engine.append_event(
                session,
                incident,
                failure_event,
                f"Step {action.step_order} failed: {error} "
                f"(attempt {action.attempts}/{action.max_attempts})"
                + ("; retrying" if retrying else ""),
                actor_type=ActorType.PROVIDER,
                data={"step_order": action.step_order, "error": error, "attempt": action.attempts},
            )
            if retrying:
                delay = self._settings.escalation_retry_base_seconds * (2 ** (action.attempts - 1))
                scheduled.status = ScheduledActionStatus.PENDING
                scheduled.due_at = now + timedelta(seconds=delay)
                scheduled.lease_expires_at = None
                scheduled.locked_by = None
                scheduled.last_error = error
            else:
                self._finish(scheduled, ScheduledActionStatus.FAILED, error)
                if action.action_type == EscalationActionType.OPERATOR_ESCALATION:
                    if incident.is_active and incident.status in (
                        IncidentStatus.OPEN,
                        IncidentStatus.CONTACTING,
                    ):
                        self._engine.transition(
                            session,
                            incident,
                            IncidentStatus.FAILED,
                            IncidentEventType.ESCALATION_STEP_FAILED,
                            "Automated escalation failed; manual handling required",
                        )
                else:
                    self._engine.append_event(
                        session,
                        incident,
                        IncidentEventType.ESCALATION_STEP_FAILED,
                        f"Step {action.step_order} failed after {action.attempts} attempts; "
                        "operators alerted immediately",
                        data={"step_order": action.step_order},
                    )
                    if incident.is_active:
                        await scheduler.ensure_operator_alert_now(session, incident)
            self._engine.notify(
                uow, incident, RealtimeMessageType.INCIDENT_UPDATED, failure_event.value
            )
            await uow.commit()
