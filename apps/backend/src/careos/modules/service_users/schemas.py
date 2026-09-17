from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from careos.modules.devices.schemas import DeviceView
from careos.modules.incident_engine.schemas import IncidentSummary
from careos.modules.service_users.models import ServiceUserStatus

_PHONE = r"^\+?[0-9 ()\-]{6,24}$"


class TrustedContactView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    relationship: str
    phone_number: str
    email: str | None
    priority: int
    is_active: bool


class ServiceUserSummary(BaseModel):
    id: uuid.UUID
    display_name: str
    first_name: str
    last_name: str
    city: str | None
    status: ServiceUserStatus
    active_incident_count: int


class ServiceUserProfile(BaseModel):
    id: uuid.UUID
    display_name: str
    first_name: str
    last_name: str
    preferred_name: str | None
    phone_number: str | None
    address_line1: str | None
    city: str | None
    postcode: str | None
    external_reference: str | None
    status: ServiceUserStatus
    escalation_policy_id: uuid.UUID | None
    created_at: datetime
    contacts: list[TrustedContactView]
    devices: list[DeviceView]
    recent_incidents: list[IncidentSummary]


class ServiceUserWrite(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    preferred_name: str | None = Field(default=None, max_length=100)
    phone_number: str | None = Field(default=None, pattern=_PHONE)
    address_line1: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    postcode: str | None = Field(default=None, max_length=16)
    home_latitude: float | None = Field(default=None, ge=-90, le=90)
    home_longitude: float | None = Field(default=None, ge=-180, le=180)
    external_reference: str | None = Field(default=None, max_length=64)
    status: ServiceUserStatus = ServiceUserStatus.ACTIVE
    escalation_policy_id: uuid.UUID | None = None


class TrustedContactWrite(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=60)
    phone_number: str = Field(pattern=_PHONE)
    email: EmailStr | None = None
    priority: int = Field(ge=1, le=9)
    is_active: bool = True
