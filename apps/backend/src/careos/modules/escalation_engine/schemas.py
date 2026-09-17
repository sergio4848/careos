from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from careos.modules.escalation_engine.models import EscalationActionType, ScheduledActionStatus


class EscalationStepWrite(BaseModel):
    delay_seconds: int = Field(ge=0, le=24 * 3600)
    action_type: EscalationActionType
    contact_priority: int | None = Field(default=None, ge=1, le=9)


class EscalationPolicyWrite(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    is_default: bool = False
    is_active: bool = True
    steps: list[EscalationStepWrite] = Field(min_length=1, max_length=20)


class EscalationStepView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_order: int
    delay_seconds: int
    action_type: EscalationActionType
    contact_priority: int | None


class EscalationPolicyView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    is_default: bool
    is_active: bool
    revision: int
    steps: list[EscalationStepView]
    updated_at: datetime


class ScheduledActionView(BaseModel):
    id: uuid.UUID
    step_order: int
    action_type: EscalationActionType
    label: str
    delay_seconds: int
    due_at: datetime
    status: ScheduledActionStatus
    attempts: int
    completed_at: datetime | None
