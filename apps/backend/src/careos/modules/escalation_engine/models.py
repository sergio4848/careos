from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from careos.db.base import (
    Base,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)


class EscalationActionType(StrEnum):
    AUTOMATED_USER_CONTACT = "AUTOMATED_USER_CONTACT"
    CALL_TRUSTED_CONTACT = "CALL_TRUSTED_CONTACT"
    NOTIFY_TRUSTED_CONTACT = "NOTIFY_TRUSTED_CONTACT"
    OPERATOR_ESCALATION = "OPERATOR_ESCALATION"


CONTACT_ACTIONS = frozenset(
    {EscalationActionType.CALL_TRUSTED_CONTACT, EscalationActionType.NOTIFY_TRUSTED_CONTACT}
)
AUTOMATED_CONTACT_ACTIONS = CONTACT_ACTIONS | {EscalationActionType.AUTOMATED_USER_CONTACT}


class EscalationPolicy(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    """Organisation-configurable escalation ladder (e.g. T+0 user, T+30 contact #1, ...)."""

    __tablename__ = "escalation_policies"
    __table_args__ = (
        UniqueConstraint("organisation_id", "id", name="uq_escalation_policies_organisation_id_id"),
        Index(
            "uq_escalation_policies_one_default",
            "organisation_id",
            unique=True,
            postgresql_where=text("is_default AND deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    steps: Mapped[list[EscalationStep]] = relationship(
        order_by="EscalationStep.step_order",
        cascade="all, delete-orphan",
        lazy="raise",
        foreign_keys="EscalationStep.policy_id",
        primaryjoin="EscalationPolicy.id == EscalationStep.policy_id",
    )


class EscalationStep(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "escalation_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "policy_id"],
            ["escalation_policies.organisation_id", "escalation_policies.id"],
            name="fk_escalation_steps_policy_same_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint("policy_id", "step_order", name="uq_escalation_steps_policy_order"),
        CheckConstraint("delay_seconds >= 0", name="delay_non_negative"),
        CheckConstraint(
            "action_type NOT IN ('CALL_TRUSTED_CONTACT', 'NOTIFY_TRUSTED_CONTACT') "
            "OR contact_priority IS NOT NULL",
            name="contact_step_has_priority",
        ),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    step_order: Mapped[int] = mapped_column(SmallInteger)
    delay_seconds: Mapped[int] = mapped_column(Integer)
    action_type: Mapped[EscalationActionType] = mapped_column(
        str_enum(EscalationActionType, "escalation_action_type")
    )
    contact_priority: Mapped[int | None] = mapped_column(SmallInteger)


class ScheduledActionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


OPEN_ACTION_STATUSES = frozenset({ScheduledActionStatus.PENDING, ScheduledActionStatus.RUNNING})

#: step_order reserved for the fail-safe operator alert inserted when automation fails.
FAILSAFE_STEP_ORDER = 1000


class ScheduledAction(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Durable escalation timer (ADR-007).

    Created in the same transaction as the incident, from a *snapshot* of the policy, so a
    crash, a Redis outage or a later policy edit can never lose or alter a running escalation.
    Claimed by workers with ``FOR UPDATE SKIP LOCKED`` and a lease.
    """

    __tablename__ = "scheduled_actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_scheduled_actions_incident_same_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("incident_id", "step_order", name="uq_scheduled_actions_incident_step"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
        CheckConstraint(
            "status <> 'RUNNING' OR (lease_expires_at IS NOT NULL AND locked_by IS NOT NULL)",
            name="running_has_lease",
        ),
        Index(
            "ix_scheduled_actions_due",
            "due_at",
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    escalation_step_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("escalation_steps.id", ondelete="SET NULL")
    )
    step_order: Mapped[int] = mapped_column(SmallInteger)
    action_type: Mapped[EscalationActionType] = mapped_column(
        str_enum(EscalationActionType, "scheduled_action_type")
    )
    contact_priority: Mapped[int | None] = mapped_column(SmallInteger)
    delay_seconds: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ScheduledActionStatus] = mapped_column(
        str_enum(ScheduledActionStatus, "scheduled_action_status"),
        default=ScheduledActionStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=3, server_default="3")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
