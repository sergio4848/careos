from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import (
    Base,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)


class ServiceUserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ServiceUser(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    """A person supported by the organisation.

    Minimum necessary data: identity, how to reach them and where they live. No date of
    birth, diagnoses, medication or other health records are stored (see docs/compliance).
    """

    __tablename__ = "service_users"
    __table_args__ = (
        UniqueConstraint("organisation_id", "id", name="uq_service_users_organisation_id_id"),
        ForeignKeyConstraint(
            ["organisation_id", "escalation_policy_id"],
            ["escalation_policies.organisation_id", "escalation_policies.id"],
            name="fk_service_users_escalation_policy_same_org",
            use_alter=True,
        ),
        Index("ix_service_users_organisation_id_last_name", "organisation_id", "last_name"),
    )

    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    preferred_name: Mapped[str | None] = mapped_column(String(100))
    phone_number: Mapped[str | None] = mapped_column(String(32))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    postcode: Mapped[str | None] = mapped_column(String(16))
    home_latitude: Mapped[float | None] = mapped_column(Float)
    home_longitude: Mapped[float | None] = mapped_column(Float)
    external_reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ServiceUserStatus] = mapped_column(
        str_enum(ServiceUserStatus, "service_user_status"), default=ServiceUserStatus.ACTIVE
    )
    escalation_policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    @property
    def display_name(self) -> str:
        return f"{self.preferred_name or self.first_name} {self.last_name}"


class TrustedContact(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    """Family member, neighbour or key holder contacted during escalation, ordered by priority."""

    __tablename__ = "trusted_contacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "service_user_id"],
            ["service_users.organisation_id", "service_users.id"],
            name="fk_trusted_contacts_service_user_same_org",
            ondelete="RESTRICT",
        ),
        CheckConstraint("priority BETWEEN 1 AND 9", name="priority_range"),
        Index(
            "uq_trusted_contacts_active_priority",
            "service_user_id",
            "priority",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND is_active"),
        ),
    )

    service_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    relationship: Mapped[str] = mapped_column(String(60))
    phone_number: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    priority: Mapped[int] = mapped_column(SmallInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
