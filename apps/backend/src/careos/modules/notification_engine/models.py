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
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class CallTargetType(StrEnum):
    SERVICE_USER = "SERVICE_USER"
    TRUSTED_CONTACT = "TRUSTED_CONTACT"


class CallStatus(StrEnum):
    """CareOS-internal call lifecycle. Provider states are mapped, never exposed."""

    QUEUED = "QUEUED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_CALL_STATUSES: frozenset[CallStatus] = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
        CallStatus.FAILED,
        CallStatus.CANCELLED,
        CallStatus.TIMED_OUT,
    }
)

#: Forward-only ordering: a status callback may never move a call backwards, and nothing
#: leaves a terminal state (late or duplicate provider callbacks are ignored).
CALL_STATUS_RANK: dict[CallStatus, int] = {
    CallStatus.QUEUED: 0,
    CallStatus.INITIATED: 1,
    CallStatus.RINGING: 2,
    CallStatus.ANSWERED: 3,
    CallStatus.IN_PROGRESS: 4,
    **dict.fromkeys(TERMINAL_CALL_STATUSES, 10),
}


class StructuredCallResponse(StrEnum):
    """Deterministic keypad response captured from a trusted-contact call."""

    CAN_RESPOND = "CAN_RESPOND"
    CANNOT_RESPOND = "CANNOT_RESPOND"
    REQUEST_OPERATOR = "REQUEST_OPERATOR"


class Call(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """An outbound voice call placed through a VoiceProvider. Phone numbers are not copied here."""

    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("organisation_id", "id", name="uq_calls_organisation_id_id"),
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_calls_incident_same_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "trusted_contact_id"],
            ["trusted_contacts.organisation_id", "trusted_contacts.id"],
            name="fk_calls_trusted_contact_same_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "service_user_id"],
            ["service_users.organisation_id", "service_users.id"],
            name="fk_calls_service_user_same_org",
            ondelete="RESTRICT",
        ),
        # Provider idempotency (ADR-017): one call row per (scheduled action, attempt), so a
        # duplicate worker claim cannot place a second real call for the same attempt.
        Index(
            "uq_calls_scheduled_action_attempt",
            "scheduled_action_id",
            "attempt",
            unique=True,
            postgresql_where=text("scheduled_action_id IS NOT NULL"),
        ),
        Index(
            "uq_calls_provider_call_sid",
            "provider_call_sid",
            unique=True,
            postgresql_where=text("provider_call_sid IS NOT NULL"),
        ),
        Index(
            "uq_calls_media_token_digest",
            "media_token_digest",
            unique=True,
            postgresql_where=text("media_token_digest IS NOT NULL"),
        ),
        CheckConstraint("direction IN ('OUTBOUND')", name="direction_valid"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="duration_non_negative"
        ),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    scheduled_action_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("scheduled_actions.id", ondelete="SET NULL")
    )
    target_type: Mapped[CallTargetType] = mapped_column(str_enum(CallTargetType, "call_target"))
    trusted_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    service_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(String(40))
    provider_call_sid: Mapped[str | None] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(8), default="OUTBOUND", server_default="OUTBOUND")
    status: Mapped[CallStatus] = mapped_column(str_enum(CallStatus, "call_status"))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    structured_response: Mapped[StructuredCallResponse | None] = mapped_column(
        str_enum(StructuredCallResponse, "call_structured_response", length=24)
    )
    attempt: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    #: Only masked digits are ever stored (for example +44*******123); never the full number.
    from_number_masked: Mapped[str | None] = mapped_column(String(32))
    to_number_masked: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    failure_category: Mapped[str | None] = mapped_column(String(40))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    #: SHA-256 digest of the single-use media-stream token (ADR-015). Raw token never stored.
    media_token_digest: Mapped[str | None] = mapped_column(String(64))
    media_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationChannel(StrEnum):
    SMS = "SMS"
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    IN_APP = "IN_APP"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """A message sent through a NotificationProvider; stores a template key, never a body."""

    __tablename__ = "notifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_notifications_incident_same_org",
            ondelete="RESTRICT",
        ),
    )

    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    channel: Mapped[NotificationChannel] = mapped_column(
        str_enum(NotificationChannel, "notification_channel")
    )
    recipient_type: Mapped[str] = mapped_column(String(32))
    recipient_ref: Mapped[str] = mapped_column(String(64))
    template_key: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[NotificationStatus] = mapped_column(
        str_enum(NotificationStatus, "notification_status")
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    last_error: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
