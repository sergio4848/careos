from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import (
    Base,
    CreatedAtMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)
from careos.modules.incident_engine.state_machine import (
    ACTIVE_STATUSES,
    IncidentStatus,
    sql_status_list,
)


class IncidentPriority(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}[self.value]


class ResolutionCategory(StrEnum):
    USER_SAFE = "USER_SAFE"
    CAREGIVER_RESPONDED = "CAREGIVER_RESPONDED"
    FAMILY_RESPONDED = "FAMILY_RESPONDED"
    FALSE_ALARM = "FALSE_ALARM"
    EMERGENCY_SERVICES = "EMERGENCY_SERVICES"
    DEVICE_ERROR = "DEVICE_ERROR"
    OTHER = "OTHER"


class IncidentEventType(StrEnum):
    SOS_RECEIVED = "SOS_RECEIVED"
    FALL_RECEIVED = "FALL_RECEIVED"
    DEVICE_FAULT_RECEIVED = "DEVICE_FAULT_RECEIVED"
    ALARM_REPEATED = "ALARM_REPEATED"
    INCIDENT_CREATED = "INCIDENT_CREATED"
    VALIDATION_STARTED = "VALIDATION_STARTED"
    VALIDATION_WARNING = "VALIDATION_WARNING"
    INCIDENT_OPENED = "INCIDENT_OPENED"
    PRIORITY_CHANGED = "PRIORITY_CHANGED"
    ESCALATION_SCHEDULED = "ESCALATION_SCHEDULED"
    ESCALATION_STEP_SKIPPED = "ESCALATION_STEP_SKIPPED"
    ESCALATION_STEP_FAILED = "ESCALATION_STEP_FAILED"
    ESCALATION_HALTED = "ESCALATION_HALTED"
    AUTOMATED_CALL_STARTED = "AUTOMATED_CALL_STARTED"
    AUTOMATED_CALL_NO_ANSWER = "AUTOMATED_CALL_NO_ANSWER"
    AUTOMATED_CALL_ANSWERED = "AUTOMATED_CALL_ANSWERED"
    AI_CALL_STARTED = "AI_CALL_STARTED"
    AI_CALL_COMPLETED = "AI_CALL_COMPLETED"
    AI_CALL_FAILED = "AI_CALL_FAILED"
    TRUSTED_CONTACT_CALLED = "TRUSTED_CONTACT_CALLED"
    TRUSTED_CONTACT_NO_ANSWER = "TRUSTED_CONTACT_NO_ANSWER"
    TRUSTED_CONTACT_NOTIFIED = "TRUSTED_CONTACT_NOTIFIED"
    CONTACT_ACKNOWLEDGED = "CONTACT_ACKNOWLEDGED"
    CALL_FAILED = "CALL_FAILED"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"
    OPERATORS_ALERTED = "OPERATORS_ALERTED"
    OPERATOR_TAKEOVER = "OPERATOR_TAKEOVER"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    INCIDENT_CLOSED = "INCIDENT_CLOSED"


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"
    DEVICE = "DEVICE"
    USER = "USER"
    PROVIDER = "PROVIDER"
    AI = "AI"


class Incident(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("organisation_id", "id", name="uq_incidents_organisation_id_id"),
        UniqueConstraint("organisation_id", "reference", name="uq_incidents_org_reference"),
        ForeignKeyConstraint(
            ["organisation_id", "service_user_id"],
            ["service_users.organisation_id", "service_users.id"],
            name="fk_incidents_service_user_same_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "device_id"],
            ["devices.organisation_id", "devices.id"],
            name="fk_incidents_device_same_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "assigned_user_id"],
            ["users.organisation_id", "users.id"],
            name="fk_incidents_assigned_user_same_org",
            ondelete="RESTRICT",
        ),
        Index("ix_incidents_organisation_id_status", "organisation_id", "status"),
        Index("ix_incidents_organisation_id_created_at", "organisation_id", "created_at"),
        # Database-enforced: at most one active incident per device (repeat presses attach).
        Index(
            "uq_incidents_one_active_per_device",
            "device_id",
            unique=True,
            postgresql_where=text(
                f"device_id IS NOT NULL AND status IN ({sql_status_list(ACTIVE_STATUSES)})"
            ),
        ),
    )

    reference: Mapped[str] = mapped_column(String(32))
    service_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_event_receipts.id", ondelete="RESTRICT")
    )
    trigger_type: Mapped[str] = mapped_column(String(40))
    priority: Mapped[IncidentPriority] = mapped_column(
        str_enum(IncidentPriority, "incident_priority")
    )
    status: Mapped[IncidentStatus] = mapped_column(str_enum(IncidentStatus, "incident_status"))
    escalation_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("escalation_policies.id", ondelete="RESTRICT")
    )
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolution_category: Mapped[ResolutionCategory | None] = mapped_column(
        str_enum(ResolutionCategory, "resolution_category")
    )
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )

    #: Monotonic counter for IncidentEvent.sequence; only changed under a row lock.
    last_event_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: Optimistic concurrency guard in addition to row locks.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class IncidentEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """Append-only incident timeline entry.

    Immutable by design: no ``updated_at`` column, and a database trigger rejects UPDATE
    and DELETE (migration 0001). Corrections are made by appending a new event.
    """

    __tablename__ = "incident_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_incident_events_incident_same_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("incident_id", "sequence", name="uq_incident_events_incident_sequence"),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[IncidentEventType] = mapped_column(
        str_enum(IncidentEventType, "incident_event_type", length=48)
    )
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType, "actor_type", length=16))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    from_status: Mapped[IncidentStatus | None] = mapped_column(
        str_enum(IncidentStatus, "incident_event_from_status")
    )
    to_status: Mapped[IncidentStatus | None] = mapped_column(
        str_enum(IncidentStatus, "incident_event_to_status")
    )
    message: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IncidentAssignment(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Who owns an incident. A partial unique index guarantees a single active owner."""

    __tablename__ = "incident_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_incident_assignments_incident_same_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "user_id"],
            ["users.organisation_id", "users.id"],
            name="fk_incident_assignments_user_same_org",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_incident_assignments_one_active",
            "incident_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(40))
