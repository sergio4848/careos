"""Incident Engine — deterministic, safety-critical core.

Rules this module guarantees:
* every status change goes through the state machine (``_transition``);
* every meaningful change appends an immutable ``IncidentEvent`` in the same transaction;
* incident rows are mutated only while holding ``SELECT ... FOR UPDATE``;
* escalation timers are persisted in the same transaction as the incident;
* no external provider (voice, notification, AI) is ever called from here.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from careos.contracts.device_events import CareOSEvent, DeviceEventType
from careos.contracts.realtime import RealtimeMessageType, incident_message
from careos.core.errors import (
    IncidentAlreadyAssignedError,
    InvalidStateTransitionError,
    NotFoundError,
)
from careos.core.logging import get_logger
from careos.core.metrics import INCIDENT_RESOLUTIONS, INCIDENT_TAKEOVERS, INCIDENTS_CREATED
from careos.core.time import utcnow
from careos.db.uow import UnitOfWork
from careos.modules.audit import service as audit
from careos.modules.audit.models import AuditActorType
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.device_gateway.models import ReceiptOutcome
from careos.modules.devices.models import Device, DeviceStatus
from careos.modules.escalation_engine import scheduler
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission
from careos.modules.incident_engine.models import (
    ActorType,
    Incident,
    IncidentAssignment,
    IncidentEvent,
    IncidentEventType,
    IncidentPriority,
    ResolutionCategory,
)
from careos.modules.incident_engine.state_machine import (
    ACTIVE_STATUSES,
    AWAITING_CLOSURE_STATUSES,
    TAKEOVER_STATUSES,
    IncidentStatus,
    assert_transition,
)
from careos.modules.service_users.models import ServiceUser, ServiceUserStatus

log = get_logger(__name__)

_TRIGGER_PRIORITY: dict[DeviceEventType, IncidentPriority] = {
    DeviceEventType.SOS_BUTTON: IncidentPriority.CRITICAL,
    DeviceEventType.FALL_DETECTED: IncidentPriority.CRITICAL,
    DeviceEventType.DEVICE_FAULT: IncidentPriority.MEDIUM,
}

_TRIGGER_EVENT: dict[DeviceEventType, tuple[IncidentEventType, str]] = {
    DeviceEventType.SOS_BUTTON: (IncidentEventType.SOS_RECEIVED, "SOS button pressed"),
    DeviceEventType.FALL_DETECTED: (IncidentEventType.FALL_RECEIVED, "Fall detected by device"),
    DeviceEventType.DEVICE_FAULT: (
        IncidentEventType.DEVICE_FAULT_RECEIVED,
        "Device fault reported",
    ),
}


def priority_for(event_type: DeviceEventType) -> IncidentPriority:
    return _TRIGGER_PRIORITY[event_type]


def new_reference(now: datetime) -> str:
    return f"INC-{now:%y%m%d}-{secrets.token_hex(4).upper()}"


@dataclass(frozen=True, slots=True)
class DeviceEventOutcome:
    outcome: ReceiptOutcome
    incident: Incident | None = None


class IncidentEngine:
    def __init__(self, *, escalation_max_attempts: int) -> None:
        self._max_attempts = escalation_max_attempts

    # ------------------------------------------------------------------ timeline primitives

    @staticmethod
    def append_event(
        session: AsyncSession,
        incident: Incident,
        event_type: IncidentEventType,
        message: str,
        *,
        actor_type: ActorType = ActorType.SYSTEM,
        actor_user_id: uuid.UUID | None = None,
        from_status: IncidentStatus | None = None,
        to_status: IncidentStatus | None = None,
        data: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> IncidentEvent:
        """Append to the immutable timeline. Caller must hold the incident row lock."""
        incident.last_event_sequence += 1
        incident.updated_at = utcnow()
        event = IncidentEvent(
            organisation_id=incident.organisation_id,
            incident_id=incident.id,
            sequence=incident.last_event_sequence,
            event_type=event_type,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            from_status=from_status,
            to_status=to_status,
            message=message[:500],
            data=data or {},
            occurred_at=occurred_at or utcnow(),
        )
        session.add(event)
        return event

    @classmethod
    def transition(
        cls,
        session: AsyncSession,
        incident: Incident,
        target: IncidentStatus,
        event_type: IncidentEventType,
        message: str,
        **kwargs: Any,
    ) -> IncidentEvent:
        """The ONLY way incident status may change."""
        assert_transition(incident.status, target)
        previous = incident.status
        incident.status = target
        return cls.append_event(
            session, incident, event_type, message, from_status=previous, to_status=target, **kwargs
        )

    @staticmethod
    async def lock(
        session: AsyncSession, organisation_id: uuid.UUID, incident_id: uuid.UUID
    ) -> Incident:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id, Incident.organisation_id == organisation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if incident is None:
            raise NotFoundError()
        return incident

    @staticmethod
    def notify(
        uow: UnitOfWork, incident: Incident, kind: RealtimeMessageType, event_type: str
    ) -> None:
        uow.notify(
            incident_message(
                kind,
                organisation_id=incident.organisation_id,
                incident_id=incident.id,
                status=incident.status.value,
                priority=incident.priority.value,
                reference=incident.reference,
                event_type=event_type,
            )
        )

    # ------------------------------------------------------------------ device events

    async def handle_device_event(
        self,
        uow: UnitOfWork,
        *,
        device: Device,
        event: CareOSEvent,
        receipt_id: uuid.UUID,
    ) -> DeviceEventOutcome:
        """Open an incident for an alarm, or attach a repeat alarm to the active one.

        The caller holds a row lock on ``device``, serialising alarms per device.
        """
        if not event.raises_incident:
            return DeviceEventOutcome(ReceiptOutcome.TELEMETRY_ONLY)

        session = uow.session
        trigger_event_type, trigger_label = _TRIGGER_EVENT[event.event_type]
        telemetry = {
            "event_id": event.event_id,
            "device_external_id": event.device_id,
            "battery": event.device.battery,
            "signal": event.device.signal,
            "event_type": event.event_type.value,
        }

        active = await session.scalar(
            select(Incident)
            .where(Incident.device_id == device.id, Incident.status.in_(ACTIVE_STATUSES))
            .with_for_update()
        )
        if active is not None:
            self.append_event(
                session,
                active,
                IncidentEventType.ALARM_REPEATED,
                f"{trigger_label} again on {device.external_id}",
                actor_type=ActorType.DEVICE,
                data=telemetry,
                occurred_at=event.timestamp,
            )
            new_priority = priority_for(event.event_type)
            if new_priority.rank < active.priority.rank:
                self.append_event(
                    session,
                    active,
                    IncidentEventType.PRIORITY_CHANGED,
                    f"Priority raised from {active.priority.value} to {new_priority.value}",
                    data={"from": active.priority.value, "to": new_priority.value},
                )
                active.priority = new_priority
            self.notify(uow, active, RealtimeMessageType.INCIDENT_UPDATED, "ALARM_REPEATED")
            return DeviceEventOutcome(ReceiptOutcome.ATTACHED_TO_INCIDENT, active)

        service_user = await self._load_service_user(session, device)
        now = utcnow()
        incident = Incident(
            id=uuid.uuid4(),
            organisation_id=device.organisation_id,
            reference=new_reference(now),
            service_user_id=service_user.id if service_user else None,
            device_id=device.id,
            source_receipt_id=receipt_id,
            trigger_type=event.event_type.value,
            priority=priority_for(event.event_type),
            status=IncidentStatus.RECEIVED,
            latitude=event.location.latitude if event.location else None,
            longitude=event.location.longitude if event.location else None,
            created_at=now,
            updated_at=now,
        )
        session.add(incident)
        await session.flush()

        self.append_event(
            session,
            incident,
            trigger_event_type,
            f"{trigger_label} on {device.external_id}",
            actor_type=ActorType.DEVICE,
            data=telemetry,
            occurred_at=event.timestamp,
        )
        self.append_event(
            session,
            incident,
            IncidentEventType.INCIDENT_CREATED,
            f"Incident {incident.reference} created with priority {incident.priority.value}",
            to_status=IncidentStatus.RECEIVED,
            data={"priority": incident.priority.value},
        )
        self.transition(
            session,
            incident,
            IncidentStatus.VALIDATING,
            IncidentEventType.VALIDATION_STARTED,
            "Validating device, service user and organisation",
        )

        warnings = self._validation_warnings(device, service_user)
        for code, text in warnings:
            self.append_event(
                session, incident, IncidentEventType.VALIDATION_WARNING, text, data={"code": code}
            )

        if event.event_type == DeviceEventType.DEVICE_FAULT:
            self.transition(
                session,
                incident,
                IncidentStatus.DEVICE_ERROR,
                IncidentEventType.INCIDENT_OPENED,
                "Device fault needs operator attention",
            )
        else:
            self.transition(
                session,
                incident,
                IncidentStatus.OPEN,
                IncidentEventType.INCIDENT_OPENED,
                "Incident open, escalation workflow starting",
            )

        # Any validation doubt or a technical fault goes straight to humans (fail safe).
        use_fallback = bool(warnings) or event.event_type == DeviceEventType.DEVICE_FAULT
        policy, steps = await scheduler.schedule_for_incident(
            session,
            incident,
            service_user,
            max_attempts=self._max_attempts,
            use_fallback=use_fallback,
        )
        incident.escalation_policy_id = policy.id if policy else None
        policy_name = policy.name if policy else "Fail-safe: alert operators immediately"
        self.append_event(
            session,
            incident,
            IncidentEventType.ESCALATION_SCHEDULED,
            f"Escalation scheduled: {policy_name} ({len(steps)} steps)",
            data={
                "policy": policy_name,
                "policy_id": str(policy.id) if policy else None,
                "steps": [
                    {
                        "step_order": s.step_order,
                        "action_type": s.action_type.value,
                        "delay_seconds": s.delay_seconds,
                        "contact_priority": s.contact_priority,
                    }
                    for s in steps
                ],
            },
        )
        audit.record(
            session,
            AuditEntry(
                action=AuditAction.INCIDENT_CREATED,
                organisation_id=incident.organisation_id,
                actor_type=AuditActorType.GATEWAY,
                resource_type="incident",
                resource_id=str(incident.id),
                details={
                    "priority": incident.priority.value,
                    "trigger_type": incident.trigger_type,
                },
            ),
            None,
        )
        trigger_type, priority = incident.trigger_type, incident.priority.value
        created_log = {
            "incident_id": str(incident.id),
            "organisation_id": str(incident.organisation_id),
            "event_id": event.event_id,
            "trigger_type": trigger_type,
            "priority": priority,
            "escalation_steps": len(steps),
            "fail_safe": use_fallback,
        }

        def _created() -> None:
            INCIDENTS_CREATED.labels(trigger_type, priority).inc()
            log.info("incident.created", **created_log)

        uow.after_commit(_created)
        self.notify(uow, incident, RealtimeMessageType.INCIDENT_CREATED, "INCIDENT_CREATED")
        return DeviceEventOutcome(ReceiptOutcome.INCIDENT_CREATED, incident)

    @staticmethod
    async def _load_service_user(session: AsyncSession, device: Device) -> ServiceUser | None:
        if device.service_user_id is None:
            return None
        return await session.scalar(
            select(ServiceUser).where(
                ServiceUser.id == device.service_user_id,
                ServiceUser.organisation_id == device.organisation_id,
                ServiceUser.deleted_at.is_(None),
            )
        )

    @staticmethod
    def _validation_warnings(
        device: Device, service_user: ServiceUser | None
    ) -> list[tuple[str, str]]:
        warnings: list[tuple[str, str]] = []
        if service_user is None:
            warnings.append(
                (
                    "device_not_assigned",
                    "Device is not assigned to a service user; operators alerted",
                )
            )
        elif service_user.status != ServiceUserStatus.ACTIVE:
            warnings.append(("service_user_inactive", "Service user record is inactive"))
        if device.status != DeviceStatus.ACTIVE:
            warnings.append(("device_not_active", f"Device status is {device.status.value}"))
        return warnings

    # ------------------------------------------------------------------ operator actions

    async def take_over(
        self, uow: UnitOfWork, principal: Principal, incident_id: uuid.UUID, context: AuditContext
    ) -> Incident:
        """Assign the incident to the calling operator. Exactly one operator can win a race."""
        session = uow.session
        incident = await self.lock(session, principal.tenant_id, incident_id)
        if incident.status not in TAKEOVER_STATUSES:
            INCIDENT_TAKEOVERS.labels("invalid_state").inc()
            raise InvalidStateTransitionError(
                f"An incident in status {incident.status.value} cannot be taken over."
            )
        current = await session.scalar(
            select(IncidentAssignment).where(
                IncidentAssignment.incident_id == incident.id,
                IncidentAssignment.released_at.is_(None),
            )
        )
        if current is not None:
            if current.user_id == principal.user_id:
                INCIDENT_TAKEOVERS.labels("already_owner").inc()
                return incident  # idempotent retry by the owner
            INCIDENT_TAKEOVERS.labels("conflict").inc()
            log.info(
                "incident.takeover_conflict",
                incident_id=str(incident.id),
                organisation_id=str(incident.organisation_id),
            )
            raise IncidentAlreadyAssignedError(details={"assigned_user_id": str(current.user_id)})

        now = utcnow()
        session.add(
            IncidentAssignment(
                organisation_id=incident.organisation_id,
                incident_id=incident.id,
                user_id=principal.user_id,
                assigned_at=now,
            )
        )
        previous = incident.status
        incident.assigned_user_id = principal.user_id
        incident.acknowledged_at = incident.acknowledged_at or now
        message = f"{principal.full_name} took over the incident"
        if previous != IncidentStatus.IN_PROGRESS:
            self.transition(
                session,
                incident,
                IncidentStatus.IN_PROGRESS,
                IncidentEventType.OPERATOR_TAKEOVER,
                message,
                actor_type=ActorType.USER,
                actor_user_id=principal.user_id,
            )
        else:
            self.append_event(
                session,
                incident,
                IncidentEventType.OPERATOR_TAKEOVER,
                message,
                actor_type=ActorType.USER,
                actor_user_id=principal.user_id,
            )
        await self._halt_escalation(session, incident, reason="operator_takeover")
        self._audit_status(
            session, principal, incident, AuditAction.INCIDENT_TAKEOVER, previous, context
        )
        try:
            await session.flush()
        except IntegrityError as exc:  # partial unique index: belt and braces behind the row lock
            await uow.rollback()
            INCIDENT_TAKEOVERS.labels("conflict").inc()
            raise IncidentAlreadyAssignedError() from exc
        taken_log = {
            "incident_id": str(incident.id),
            "organisation_id": str(incident.organisation_id),
            "user_id": str(principal.user_id),
            "from_status": previous.value,
        }

        def _taken() -> None:
            INCIDENT_TAKEOVERS.labels("assigned").inc()
            log.info("incident.taken_over", **taken_log)

        uow.after_commit(_taken)
        self.notify(uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "OPERATOR_TAKEOVER")
        await uow.commit()
        return incident

    async def resolve(
        self,
        uow: UnitOfWork,
        principal: Principal,
        incident_id: uuid.UUID,
        *,
        category: ResolutionCategory,
        notes: str | None,
        context: AuditContext,
    ) -> Incident:
        session = uow.session
        incident = await self.lock(session, principal.tenant_id, incident_id)
        if incident.status not in ACTIVE_STATUSES:
            raise InvalidStateTransitionError(
                f"An incident in status {incident.status.value} cannot be resolved."
            )
        if (
            incident.assigned_user_id is not None
            and incident.assigned_user_id != principal.user_id
            and not principal.has(Permission.INCIDENTS_OVERRIDE_ASSIGNMENT)
        ):
            raise IncidentAlreadyAssignedError(
                "This incident is owned by another operator.",
                details={"assigned_user_id": str(incident.assigned_user_id)},
            )
        previous = incident.status
        target = (
            IncidentStatus.FALSE_ALARM
            if category == ResolutionCategory.FALSE_ALARM
            else IncidentStatus.RESOLVED
        )
        now = utcnow()
        self.transition(
            session,
            incident,
            target,
            IncidentEventType.INCIDENT_RESOLVED,
            f"Resolved by {principal.full_name}: {category.value.replace('_', ' ').lower()}",
            actor_type=ActorType.USER,
            actor_user_id=principal.user_id,
            data={"category": category.value},
        )
        incident.resolved_at = now
        incident.resolved_by_user_id = principal.user_id
        incident.resolution_category = category
        incident.resolution_notes = (notes or "").strip() or None
        await self._halt_escalation(session, incident, reason="incident_resolved")
        self._audit_status(
            session, principal, incident, AuditAction.INCIDENT_RESOLVED, previous, context
        )
        resolved_log = {
            "incident_id": str(incident.id),
            "organisation_id": str(incident.organisation_id),
            "user_id": str(principal.user_id),
            "category": category.value,
            "from_status": previous.value,
        }

        def _resolved() -> None:
            INCIDENT_RESOLUTIONS.labels(category.value).inc()
            log.info("incident.resolved", **resolved_log)

        uow.after_commit(_resolved)
        self.notify(uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "INCIDENT_RESOLVED")
        await uow.commit()
        return incident

    async def close(
        self,
        uow: UnitOfWork,
        principal: Principal,
        incident_id: uuid.UUID,
        *,
        notes: str | None,
        context: AuditContext,
    ) -> Incident:
        session = uow.session
        incident = await self.lock(session, principal.tenant_id, incident_id)
        if incident.status not in AWAITING_CLOSURE_STATUSES:
            raise InvalidStateTransitionError(
                "Only resolved, false-alarm or cancelled incidents can be closed."
            )
        previous = incident.status
        now = utcnow()
        self.transition(
            session,
            incident,
            IncidentStatus.CLOSED,
            IncidentEventType.INCIDENT_CLOSED,
            f"Closed by {principal.full_name}",
            actor_type=ActorType.USER,
            actor_user_id=principal.user_id,
            data={"has_notes": bool(notes)},
        )
        incident.closed_at = now
        incident.closed_by_user_id = principal.user_id
        if notes:
            incident.resolution_notes = (
                f"{incident.resolution_notes or ''}\n\n[Closure] {notes.strip()}".strip()
            )
        assignment = await session.scalar(
            select(IncidentAssignment).where(
                IncidentAssignment.incident_id == incident.id,
                IncidentAssignment.released_at.is_(None),
            )
        )
        if assignment is not None:
            assignment.released_at = now
            assignment.release_reason = "incident_closed"
        self._audit_status(
            session, principal, incident, AuditAction.INCIDENT_CLOSED, previous, context
        )
        closed_log = {
            "incident_id": str(incident.id),
            "organisation_id": str(incident.organisation_id),
            "user_id": str(principal.user_id),
        }
        uow.after_commit(lambda: log.info("incident.closed", **closed_log))
        self.notify(uow, incident, RealtimeMessageType.INCIDENT_UPDATED, "INCIDENT_CLOSED")
        await uow.commit()
        return incident

    # ------------------------------------------------------------------ helpers

    async def _halt_escalation(
        self, session: AsyncSession, incident: Incident, *, reason: str
    ) -> None:
        cancelled = await scheduler.cancel_pending_actions(session, incident.id, reason=reason)
        if cancelled:
            self.append_event(
                session,
                incident,
                IncidentEventType.ESCALATION_HALTED,
                f"Automated escalation stopped ({cancelled} pending step(s) cancelled)",
                data={"reason": reason, "cancelled_steps": cancelled},
            )

    @staticmethod
    def _audit_status(
        session: AsyncSession,
        principal: Principal,
        incident: Incident,
        action: AuditAction,
        previous: IncidentStatus,
        context: AuditContext,
    ) -> None:
        resource = {"resource_type": "incident", "resource_id": str(incident.id)}
        audit.record(
            session,
            AuditEntry.by(principal, action, details={"from_status": previous.value}, **resource),
            context,
        )
        if previous != incident.status:
            audit.record(
                session,
                AuditEntry.by(
                    principal,
                    AuditAction.INCIDENT_STATE_CHANGED,
                    details={"from_status": previous.value, "to_status": incident.status.value},
                    **resource,
                ),
                context,
            )
