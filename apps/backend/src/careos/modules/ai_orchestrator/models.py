from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKeyConstraint, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class AISessionPurpose(StrEnum):
    AUTOMATED_CHECK_IN = "AUTOMATED_CHECK_IN"


class AISessionStatus(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class AISession(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Record of an assistive AI interaction.

    ``advisory_summary`` is decision *support* only. It never changes incident state and
    is always displayed as AI-generated (ADR-006).
    """

    __tablename__ = "ai_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_ai_sessions_incident_same_org",
            ondelete="RESTRICT",
        ),
    )

    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    purpose: Mapped[AISessionPurpose] = mapped_column(str_enum(AISessionPurpose, "ai_purpose"))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[AISessionStatus] = mapped_column(str_enum(AISessionStatus, "ai_status"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    advisory_summary: Mapped[str | None] = mapped_column(Text)
