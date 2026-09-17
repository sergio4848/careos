from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
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


class DeviceType(StrEnum):
    SOS_PENDANT = "SOS_PENDANT"
    SMARTWATCH = "SMARTWATCH"
    FALL_SENSOR = "FALL_SENSOR"
    HOME_HUB = "HOME_HUB"
    OTHER = "OTHER"


class DeviceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    RETIRED = "RETIRED"


class ConnectionStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class Device(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    """A registered device. ``external_id`` is the identifier the manufacturer platform sends."""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("organisation_id", "id", name="uq_devices_organisation_id_id"),
        UniqueConstraint("organisation_id", "external_id", name="uq_devices_org_external_id"),
        ForeignKeyConstraint(
            ["organisation_id", "service_user_id"],
            ["service_users.organisation_id", "service_users.id"],
            name="fk_devices_service_user_same_org",
            ondelete="RESTRICT",
        ),
    )

    service_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    device_type: Mapped[DeviceType] = mapped_column(str_enum(DeviceType, "device_type"))
    manufacturer: Mapped[str] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    adapter: Mapped[str] = mapped_column(String(40))
    status: Mapped[DeviceStatus] = mapped_column(
        str_enum(DeviceStatus, "device_status"), default=DeviceStatus.ACTIVE
    )


class DeviceConnection(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Current connectivity/telemetry snapshot for a device.

    Kept apart from ``devices`` so high-frequency telemetry writes never contend with
    registry edits, and so a time-series store can replace it later.
    """

    __tablename__ = "device_connections"
    __table_args__ = (
        UniqueConstraint("device_id", name="uq_device_connections_device_id"),
        ForeignKeyConstraint(
            ["organisation_id", "device_id"],
            ["devices.organisation_id", "devices.id"],
            name="fk_device_connections_device_same_org",
            ondelete="CASCADE",
        ),
        CheckConstraint("battery_level BETWEEN 0 AND 100", name="battery_range"),
        CheckConstraint("signal_strength BETWEEN 0 AND 100", name="signal_range"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[ConnectionStatus] = mapped_column(
        str_enum(ConnectionStatus, "connection_status"), default=ConnectionStatus.UNKNOWN
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    battery_level: Mapped[int | None] = mapped_column(SmallInteger)
    signal_strength: Mapped[int | None] = mapped_column(SmallInteger)
    last_latitude: Mapped[float | None] = mapped_column(Float)
    last_longitude: Mapped[float | None] = mapped_column(Float)
