from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from careos.modules.organisations.models import OrganisationStatus


class OrganisationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: OrganisationStatus
    country_code: str
    timezone: str


class CreateOrganisationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    country_code: str = Field(default="GB", pattern=r"^[A-Z]{2}$")
    timezone: str = Field(default="Europe/London", max_length=64)
    admin_email: EmailStr
    admin_full_name: str = Field(min_length=1, max_length=200)
    admin_password: str = Field(min_length=12, max_length=256)
