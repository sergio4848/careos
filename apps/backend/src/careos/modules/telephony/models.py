"""Append-only call event ledger.

One row per meaningful call lifecycle event: provider callbacks, media-stream milestones,
AI session boundaries and DTMF. Raw audio frames are NEVER stored here (ADR-018).
Deduplication of provider callbacks happens on ``provider_event_key`` (ADR-017).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import Base, CreatedAtMixin, TenantMixin, UUIDPrimaryKeyMixin, str_enum


class CallEventType(StrEnum):
    CALL_REQUESTED = "CALL_REQUESTED"
    CALL_INITIATED = "CALL_INITIATED"
    CALL_RINGING = "CALL_RINGING"
    CALL_ANSWERED = "CALL_ANSWERED"
    MEDIA_STREAM_CONNECTED = "MEDIA_STREAM_CONNECTED"
    AI_SESSION_STARTED = "AI_SESSION_STARTED"
    AI_SESSION_COMPLETED = "AI_SESSION_COMPLETED"
    AI_SESSION_FAILED = "AI_SESSION_FAILED"
    DTMF_RECEIVED = "DTMF_RECEIVED"
    USER_RESPONSE_RECEIVED = "USER_RESPONSE_RECEIVED"
    CALL_ENDED = "CALL_ENDED"
    CALL_FAILED = "CALL_FAILED"
    CALL_NO_ANSWER = "CALL_NO_ANSWER"
    CALL_BUSY = "CALL_BUSY"
    CALL_CANCELLED = "CALL_CANCELLED"
    CALL_TIMED_OUT = "CALL_TIMED_OUT"


class CallEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """Immutable by design: a database trigger rejects UPDATE and DELETE (migration 0003)."""

    __tablename__ = "call_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "call_id"],
            ["calls.organisation_id", "calls.id"],
            name="fk_call_events_call_same_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_call_events_incident_same_org",
            ondelete="RESTRICT",
        ),
        # Webhook idempotency: a redelivered provider callback maps onto the same row.
        Index(
            "uq_call_events_provider_key",
            "call_id",
            "provider_event_key",
            unique=True,
            postgresql_where=text("provider_event_key IS NOT NULL"),
        ),
        Index("ix_call_events_call_id_created_at", "call_id", "created_at"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_type: Mapped[CallEventType] = mapped_column(
        str_enum(CallEventType, "call_event_type", length=32)
    )
    #: Identity of the provider callback that produced this row (dedupe key), null for
    #: internally generated events.
    provider_event_key: Mapped[str | None] = mapped_column(String(160))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
