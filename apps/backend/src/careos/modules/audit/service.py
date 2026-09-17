"""Audit trail recording.

Audit rows are written in the *same transaction* as the action they describe, so an
action and its audit record commit or roll back together. Denied/failed attempts are
written with :func:`record_isolated`, which uses its own transaction so the record
survives the rollback of the request that failed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.core.logging import get_logger
from careos.core.time import utcnow
from careos.modules.audit.models import AuditActorType, AuditLog, AuditOutcome

if TYPE_CHECKING:
    from careos.modules.identity.principal import Principal

log = get_logger(__name__)


class AuditAction(StrEnum):
    AUTH_LOGIN_SUCCEEDED = "AUTH_LOGIN_SUCCEEDED"
    AUTH_LOGIN_FAILED = "AUTH_LOGIN_FAILED"
    AUTH_LOGOUT = "AUTH_LOGOUT"
    ACCESS_DENIED = "ACCESS_DENIED"
    ORGANISATION_CREATED = "ORGANISATION_CREATED"
    USER_CREATED = "USER_CREATED"
    USER_PERMISSIONS_CHANGED = "USER_PERMISSIONS_CHANGED"
    SERVICE_USER_CREATED = "SERVICE_USER_CREATED"
    SERVICE_USER_UPDATED = "SERVICE_USER_UPDATED"
    SERVICE_USER_VIEWED = "SERVICE_USER_VIEWED"
    TRUSTED_CONTACT_CREATED = "TRUSTED_CONTACT_CREATED"
    TRUSTED_CONTACT_UPDATED = "TRUSTED_CONTACT_UPDATED"
    DEVICE_ADDED = "DEVICE_ADDED"
    DEVICE_UPDATED = "DEVICE_UPDATED"
    ESCALATION_POLICY_CREATED = "ESCALATION_POLICY_CREATED"
    ESCALATION_POLICY_CHANGED = "ESCALATION_POLICY_CHANGED"
    GATEWAY_EVENT_ACCEPTED = "GATEWAY_EVENT_ACCEPTED"
    GATEWAY_EVENT_REJECTED = "GATEWAY_EVENT_REJECTED"
    GATEWAY_AUTH_FAILED = "GATEWAY_AUTH_FAILED"
    SIMULATOR_EVENT_SENT = "SIMULATOR_EVENT_SENT"
    INCIDENT_CREATED = "INCIDENT_CREATED"
    INCIDENT_VIEWED = "INCIDENT_VIEWED"
    INCIDENT_TAKEOVER = "INCIDENT_TAKEOVER"
    INCIDENT_STATE_CHANGED = "INCIDENT_STATE_CHANGED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    INCIDENT_CLOSED = "INCIDENT_CLOSED"


@dataclass(frozen=True, slots=True)
class AuditContext:
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None


@dataclass(slots=True)
class AuditEntry:
    action: AuditAction
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    organisation_id: uuid.UUID | None = None
    actor_type: AuditActorType = AuditActorType.SYSTEM
    actor_user_id: uuid.UUID | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def by(cls, principal: Principal, action: AuditAction, **kwargs: Any) -> AuditEntry:
        return cls(
            action=action,
            organisation_id=principal.organisation_id,
            actor_type=AuditActorType.USER,
            actor_user_id=principal.user_id,
            **kwargs,
        )


def _to_row(entry: AuditEntry, context: AuditContext | None) -> AuditLog:
    context = context or AuditContext()
    return AuditLog(
        organisation_id=entry.organisation_id,
        actor_type=entry.actor_type,
        actor_user_id=entry.actor_user_id,
        action=entry.action.value,
        resource_type=entry.resource_type,
        resource_id=entry.resource_id,
        outcome=entry.outcome,
        request_id=context.request_id,
        ip_address=context.ip_address,
        user_agent=(context.user_agent or "")[:255] or None,
        details=entry.details,
    )


def record(session: AsyncSession, entry: AuditEntry, context: AuditContext | None) -> None:
    """Stage an audit row in the caller's transaction."""
    session.add(_to_row(entry, context))


async def record_isolated(
    session_factory: async_sessionmaker[AsyncSession],
    entry: AuditEntry,
    context: AuditContext | None,
) -> None:
    """Persist an audit row in its own transaction (for failures and denials)."""
    try:
        async with session_factory() as session:
            session.add(_to_row(entry, context))
            await session.commit()
    except Exception:  # audit must never mask the original error
        log.exception("audit.isolated_write_failed", action=entry.action.value)


#: Read-access events are recorded at most once per actor/resource in this window, so live
#: consoles that refetch on every realtime update do not flood the audit trail.
READ_AUDIT_WINDOW = timedelta(minutes=10)


async def record_view(
    session: AsyncSession,
    principal: Principal,
    action: AuditAction,
    *,
    resource_type: str,
    resource_id: str,
    context: AuditContext | None,
) -> None:
    recent = await session.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.actor_user_id == principal.user_id,
            AuditLog.action == action.value,
            AuditLog.resource_id == resource_id,
            AuditLog.created_at > utcnow() - READ_AUDIT_WINDOW,
        )
        .limit(1)
    )
    if recent is None:
        record(
            session,
            AuditEntry.by(principal, action, resource_type=resource_type, resource_id=resource_id),
            context,
        )
