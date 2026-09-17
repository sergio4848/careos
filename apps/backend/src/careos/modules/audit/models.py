from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, str_enum


class AuditOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"


class AuditActorType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    GATEWAY = "GATEWAY"
    ANONYMOUS = "ANONYMOUS"


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable security/compliance audit record.

    No ``updated_at``: rows are never modified. A database trigger rejects UPDATE/DELETE,
    and no API exists to change them. ``details`` must not contain personal data.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_organisation_id_created_at", "organisation_id", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_actor_user_id_created_at", "actor_user_id", "created_at"),
    )

    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organisations.id", ondelete="RESTRICT")
    )
    actor_type: Mapped[AuditActorType] = mapped_column(
        str_enum(AuditActorType, "audit_actor_type", length=16)
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[AuditOutcome] = mapped_column(str_enum(AuditOutcome, "audit_outcome", 16))
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
