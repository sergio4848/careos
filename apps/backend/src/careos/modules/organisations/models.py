from __future__ import annotations

from enum import StrEnum

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from careos.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class OrganisationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class Organisation(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A tenant: a telecare provider, home-care company, ARC, etc."""

    __tablename__ = "organisations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[OrganisationStatus] = mapped_column(
        str_enum(OrganisationStatus, "organisation_status"), default=OrganisationStatus.ACTIVE
    )
    country_code: Mapped[str] = mapped_column(String(2), default="GB")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/London")
