from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    SmallInteger,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class CallTargetType(StrEnum):
    SERVICE_USER = "SERVICE_USER"
    TRUSTED_CONTACT = "TRUSTED_CONTACT"


class CallStatus(StrEnum):
    INITIATED = "INITIATED"
    ANSWERED = "ANSWERED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"


class Call(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """An outbound voice call placed through a VoiceProvider. Phone numbers are not copied here."""

    __tablename__ = "calls"
    __table_args__ = (
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
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    scheduled_action_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("scheduled_actions.id", ondelete="SET NULL")
    )
    target_type: Mapped[CallTargetType] = mapped_column(str_enum(CallTargetType, "call_target"))
    trusted_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    service_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(String(40))
    provider_call_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[CallStatus] = mapped_column(str_enum(CallStatus, "call_status"))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    attempt: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(255))


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
