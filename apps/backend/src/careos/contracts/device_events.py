"""CareOS Standard Device Event Contract (v1).

Every manufacturer adapter normalises its vendor payload into :class:`CareOSEvent`.
The Incident Engine only ever sees this contract, never a vendor format.

This Pydantic model is the single source of truth; the JSON Schema in
``packages/contracts/schemas/careos-device-event.v1.schema.json`` is generated from it
(``python -m careos.cli export-contracts``) and checked for drift in CI.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

MAX_CLOCK_SKEW = timedelta(minutes=5)

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_\-:.]*$"


class DeviceEventType(StrEnum):
    SOS_BUTTON = "SOS_BUTTON"
    FALL_DETECTED = "FALL_DETECTED"
    DEVICE_FAULT = "DEVICE_FAULT"
    LOW_BATTERY = "LOW_BATTERY"
    HEARTBEAT = "HEARTBEAT"


class GeoLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100_000)


class DeviceTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    battery: int | None = Field(default=None, ge=0, le=100, description="Battery level, percent")
    signal: int | None = Field(default=None, ge=0, le=100, description="Signal quality, percent")


MetadataValue = str | int | float | bool | None


class CareOSEvent(BaseModel):
    """A normalised event emitted by a care device or connected system."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        title="CareOS Device Event v1",
        json_schema_extra={
            "$id": "https://schemas.careos.dev/careos-device-event.v1.schema.json",
            "examples": [
                {
                    "schema_version": "1.0",
                    "event_id": "evt_01J9Z3Q4W7K8M2N5P6R7S8T9V0",
                    "event_type": "SOS_BUTTON",
                    "device_id": "DEV-0001",
                    "service_user_id": None,
                    "timestamp": "2026-09-16T09:30:00Z",
                    "location": {"latitude": 51.5074, "longitude": -0.1278},
                    "device": {"battery": 74, "signal": 82},
                    "metadata": {},
                }
            ],
        },
    )

    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
        description="Globally unique, producer-generated ID. Used for idempotency.",
    )
    event_type: DeviceEventType
    device_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=_IDENTIFIER_PATTERN,
        description="Device identifier as registered in CareOS (e.g. manufacturer serial).",
    )
    service_user_id: uuid.UUID | None = Field(
        default=None,
        description="Optional hint. The CareOS device registry is authoritative.",
    )
    timestamp: AwareDatetime = Field(description="When the event occurred on the device (ISO-8601)")
    location: GeoLocation | None = None
    device: DeviceTelemetry = Field(default_factory=DeviceTelemetry)
    metadata: dict[str, MetadataValue] = Field(
        default_factory=dict,
        max_length=32,
        description="Flat vendor extras. Must not contain personal or health data.",
    )

    @field_validator("timestamp")
    @classmethod
    def _normalise_timestamp(cls, value: datetime) -> datetime:
        value = value.astimezone(UTC)
        if value - datetime.now(UTC) > MAX_CLOCK_SKEW:
            raise ValueError("timestamp is too far in the future")
        return value

    @field_validator("metadata")
    @classmethod
    def _bound_metadata(cls, value: dict[str, MetadataValue]) -> dict[str, MetadataValue]:
        for key, item in value.items():
            if len(key) > 64:
                raise ValueError("metadata keys must be at most 64 characters")
            if isinstance(item, str) and len(item) > 256:
                raise ValueError("metadata string values must be at most 256 characters")
        return value

    @property
    def raises_incident(self) -> bool:
        return self.event_type in INCIDENT_TRIGGER_TYPES


INCIDENT_TRIGGER_TYPES: frozenset[DeviceEventType] = frozenset(
    {DeviceEventType.SOS_BUTTON, DeviceEventType.FALL_DETECTED, DeviceEventType.DEVICE_FAULT}
)
