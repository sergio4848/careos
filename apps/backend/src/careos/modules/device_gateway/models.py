from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class GatewayCredential(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Credential used by a device platform / alarm receiver to push events for ONE tenant."""

    __tablename__ = "gateway_credentials"

    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_digest: Mapped[str] = mapped_column(String(64))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReceiptOutcome(StrEnum):
    INCIDENT_CREATED = "INCIDENT_CREATED"
    ATTACHED_TO_INCIDENT = "ATTACHED_TO_INCIDENT"
    TELEMETRY_ONLY = "TELEMETRY_ONLY"


class DeviceEventReceipt(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Idempotency ledger + evidence of every accepted device event (ADR-004).

    ``(organisation_id, event_id)`` is unique: a redelivered event can never create a
    second incident. The normalised event is kept as evidence for incident review.
    """

    __tablename__ = "device_event_receipts"
    __table_args__ = (
        UniqueConstraint("organisation_id", "event_id", name="uq_device_event_receipts_org_event"),
        UniqueConstraint(
            "organisation_id", "id", name="uq_device_event_receipts_organisation_id_id"
        ),
        ForeignKeyConstraint(
            ["organisation_id", "device_id"],
            ["devices.organisation_id", "devices.id"],
            name="fk_device_event_receipts_device_same_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_device_event_receipts_incident_same_org",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        # An accepted receipt names its device; incident outcomes name their incident.
        CheckConstraint("(outcome IS NULL) = (device_id IS NULL)", name="outcome_has_device"),
        CheckConstraint(
            "CASE WHEN outcome IN ('INCIDENT_CREATED', 'ATTACHED_TO_INCIDENT') "
            "THEN incident_id IS NOT NULL ELSE incident_id IS NULL END",
            name="incident_outcome_linked",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(40))
    event_type: Mapped[str] = mapped_column(String(40))
    device_external_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    outcome: Mapped[ReceiptOutcome | None] = mapped_column(
        str_enum(ReceiptOutcome, "receipt_outcome")
    )
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
