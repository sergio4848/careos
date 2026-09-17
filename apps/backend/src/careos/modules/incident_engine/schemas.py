from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from careos.modules.escalation_engine.schemas import ScheduledActionView
from careos.modules.incident_engine.models import (
    ActorType,
    IncidentEventType,
    IncidentPriority,
    ResolutionCategory,
)
from careos.modules.incident_engine.state_machine import IncidentStatus


class PersonRef(BaseModel):
    id: uuid.UUID
    name: str


class ServiceUserBrief(BaseModel):
    id: uuid.UUID
    display_name: str
    city: str | None


class DeviceBrief(BaseModel):
    id: uuid.UUID
    external_id: str
    device_type: str
    battery_level: int | None
    signal_strength: int | None


class NextActionView(BaseModel):
    step_order: int
    action_type: str
    label: str
    due_at: datetime


class IncidentSummary(BaseModel):
    id: uuid.UUID
    reference: str
    status: IncidentStatus
    priority: IncidentPriority
    trigger_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    service_user: ServiceUserBrief | None
    device: DeviceBrief | None
    assignee: PersonRef | None
    next_action: NextActionView | None


class LocationView(BaseModel):
    latitude: float
    longitude: float


class IncidentDetail(IncidentSummary):
    location: LocationView | None
    resolution_category: ResolutionCategory | None
    resolution_notes: str | None
    resolved_by: PersonRef | None
    closed_by: PersonRef | None
    escalation: list[ScheduledActionView]
    allowed_actions: list[str] = Field(
        description="Actions the current user may take now: takeover, resolve, close."
    )


class IncidentEventView(BaseModel):
    id: uuid.UUID
    sequence: int
    event_type: IncidentEventType
    actor_type: ActorType
    actor: PersonRef | None
    from_status: IncidentStatus | None
    to_status: IncidentStatus | None
    message: str
    data: dict[str, Any]
    occurred_at: datetime
    created_at: datetime


class ResolveIncidentRequest(BaseModel):
    category: ResolutionCategory
    notes: str | None = Field(default=None, max_length=2000)


class CloseIncidentRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)
