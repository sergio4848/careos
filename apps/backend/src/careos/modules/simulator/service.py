"""SOS Simulator — a virtual manufacturer platform for development and demos.

It does NOT create incidents. It builds a payload in the fictional "CareOS Sim Pendant"
vendor format and sends a real HTTP request to the Device Event Gateway, authenticated
with a gateway credential, exactly as a device platform would. Disabled in production.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careos.bootstrap import Container
from careos.core.context import get_request_id
from careos.core.errors import CareOSError, FeatureDisabledError, NotFoundError
from careos.core.logging import get_logger
from careos.core.time import utcnow
from careos.modules.audit import service as audit
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.device_gateway.service import GATEWAY_KEY_HEADER
from careos.modules.devices.models import Device
from careos.modules.identity.principal import Principal
from careos.modules.service_users.models import ServiceUser

log = get_logger(__name__)

SimulatedEventType = Literal[
    "SOS_BUTTON", "FALL_DETECTED", "DEVICE_FAULT", "LOW_BATTERY", "HEARTBEAT"
]

_VENDOR_KIND: dict[str, str] = {
    "SOS_BUTTON": "SOS",
    "FALL_DETECTED": "FALL",
    "DEVICE_FAULT": "FAULT",
    "LOW_BATTERY": "LOW_BATT",
    "HEARTBEAT": "HEARTBEAT",
}


class SimulatorEventRequest(BaseModel):
    event_type: SimulatedEventType = "SOS_BUTTON"
    battery: int = Field(default=84, ge=0, le=100)
    signal: int = Field(default=92, ge=0, le=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    event_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-:.]*$",
        description="Reuse a previous event_id to demonstrate gateway idempotency.",
    )
    timestamp_ms: int | None = Field(
        default=None,
        ge=0,
        description="Device timestamp. Send the original value with event_id for an exact replay.",
    )


class SimulatorEventResult(BaseModel):
    event_id: str
    vendor_payload: dict[str, Any]
    gateway_status_code: int
    gateway_response: dict[str, Any]


class GatewayUnreachableError(CareOSError):
    status_code = 502
    code = "gateway_unreachable"
    default_message = "The simulator could not reach the device gateway."


async def send_event(
    session: AsyncSession,
    container: Container,
    principal: Principal,
    device_id: uuid.UUID,
    request: SimulatorEventRequest,
    context: AuditContext,
) -> SimulatorEventResult:
    settings = container.settings
    if not settings.simulator_enabled:
        raise FeatureDisabledError()
    if settings.simulator_gateway_key is None:
        raise FeatureDisabledError("Simulator gateway key is not configured.")

    row = (
        await session.execute(
            select(Device, ServiceUser)
            .outerjoin(ServiceUser, ServiceUser.id == Device.service_user_id)
            .where(
                Device.id == device_id,
                Device.organisation_id == principal.tenant_id,
                Device.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError()
    device, service_user = row

    latitude = (
        request.latitude
        if request.latitude is not None
        else (service_user.home_latitude if service_user else None)
    )
    longitude = (
        request.longitude
        if request.longitude is not None
        else (service_user.home_longitude if service_user else None)
    )
    event_id = request.event_id or f"sim_{uuid.uuid4().hex}"
    vendor_payload: dict[str, Any] = {
        "msg_id": event_id,
        "imei": device.external_id,
        "kind": _VENDOR_KIND[request.event_type],
        "ts_ms": request.timestamp_ms or int(utcnow().timestamp() * 1000),
        "bat_pct": request.battery,
        "rssi_pct": request.signal,
        "fw": "sim-1.0",
    }
    if latitude is not None and longitude is not None:
        vendor_payload["gps"] = {"lat": latitude, "lon": longitude}

    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.SIMULATOR_EVENT_SENT,
            resource_type="device",
            resource_id=str(device.id),
            details={"event_type": request.event_type, "event_id": event_id},
        ),
        context,
    )
    await session.commit()  # release the DB connection before the outbound HTTP call

    headers = {GATEWAY_KEY_HEADER: settings.simulator_gateway_key.get_secret_value()}
    if request_id := get_request_id():
        headers["X-Request-ID"] = request_id
    try:
        async with httpx.AsyncClient(
            base_url=settings.gateway_internal_url,
            transport=container.simulator_transport,
            timeout=httpx.Timeout(10.0),
        ) as client:
            response = await client.post(
                "/v1/gateway/simulator/events", json=vendor_payload, headers=headers
            )
    except httpx.HTTPError as exc:
        log.warning("simulator.gateway_unreachable", error=type(exc).__name__)
        raise GatewayUnreachableError() from exc

    try:
        body = response.json()
    except ValueError:
        body = {"detail": "non-JSON response"}
    return SimulatorEventResult(
        event_id=event_id,
        vendor_payload=vendor_payload,
        gateway_status_code=response.status_code,
        gateway_response=body if isinstance(body, dict) else {"data": body},
    )
