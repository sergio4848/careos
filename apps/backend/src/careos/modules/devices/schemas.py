from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from careos.modules.devices.models import ConnectionStatus, DeviceStatus, DeviceType


class ServiceUserRef(BaseModel):
    id: uuid.UUID
    display_name: str
    city: str | None


class ConnectionView(BaseModel):
    status: ConnectionStatus
    last_seen_at: datetime | None
    battery_level: int | None
    signal_strength: int | None


class DeviceView(BaseModel):
    id: uuid.UUID
    external_id: str
    device_type: DeviceType
    manufacturer: str
    model: str | None
    adapter: str
    status: DeviceStatus
    service_user: ServiceUserRef | None
    connection: ConnectionView | None
    low_battery: bool
    created_at: datetime


class CreateDeviceRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-:.]*$")
    device_type: DeviceType
    manufacturer: str = Field(min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    adapter: str = Field(default="careos", pattern=r"^[a-z0-9_\-]{1,40}$")
    service_user_id: uuid.UUID | None = None
