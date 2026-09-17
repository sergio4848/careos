from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from careos.modules.identity.rbac import Role
from careos.modules.organisations.schemas import OrganisationView


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    organisation_id: uuid.UUID | None
    is_active: bool
    last_login_at: datetime | None


class SessionView(BaseModel):
    user: UserView
    organisation: OrganisationView | None
    permissions: list[str]
    csrf_token: str = Field(description="Send as X-CSRF-Token on every state-changing request.")


class CreateUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: Role
    password: str = Field(min_length=12, max_length=256)


class ChangeRoleRequest(BaseModel):
    role: Role
