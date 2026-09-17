"""Realtime message contract pushed to operator consoles over WebSocket.

Messages are *notifications*, not state: they carry identifiers and coarse status only
(no personal data). Clients refetch authoritative state through the REST API, which
enforces authorisation. If a message is lost, nothing is lost: the database is the truth.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from careos.core.time import utcnow


class RealtimeMessageType(StrEnum):
    INCIDENT_CREATED = "incident.created"
    INCIDENT_UPDATED = "incident.updated"
    DEVICE_UPDATED = "device.updated"


class RealtimeMessage(BaseModel):
    model_config = ConfigDict(frozen=True, title="CareOS Realtime Message v1")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    type: RealtimeMessageType
    organisation_id: uuid.UUID
    occurred_at: datetime = Field(default_factory=utcnow)
    incident_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RealtimePublisher(Protocol):
    async def publish(self, message: RealtimeMessage) -> None: ...


def incident_message(
    message_type: RealtimeMessageType,
    *,
    organisation_id: uuid.UUID,
    incident_id: uuid.UUID,
    status: str,
    priority: str,
    reference: str,
    event_type: str | None = None,
) -> RealtimeMessage:
    return RealtimeMessage(
        type=message_type,
        organisation_id=organisation_id,
        incident_id=incident_id,
        payload={
            "status": status,
            "priority": priority,
            "reference": reference,
            "event_type": event_type,
        },
    )
