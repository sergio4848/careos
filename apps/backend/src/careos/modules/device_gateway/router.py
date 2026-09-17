from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from careos.api.deps import AuditContextDep, ContainerDep, UowDep
from careos.modules.device_gateway.models import ReceiptOutcome
from careos.modules.device_gateway.service import GATEWAY_KEY_HEADER, DeviceGatewayService
from careos.modules.incident_engine.service import IncidentEngine

router = APIRouter(prefix="/v1/gateway", tags=["device-gateway"])

gateway_key_scheme = APIKeyHeader(
    name=GATEWAY_KEY_HEADER, auto_error=False, scheme_name="GatewayKey"
)


class IngestResponse(BaseModel):
    receipt_id: uuid.UUID
    event_id: str
    duplicate: bool
    outcome: ReceiptOutcome | None
    incident_id: uuid.UUID | None
    incident_reference: str | None
    incident_status: str | None


def get_gateway_service(container: ContainerDep) -> DeviceGatewayService:
    return DeviceGatewayService(
        settings=container.settings,
        adapters=container.adapters,
        engine=IncidentEngine(escalation_max_attempts=container.settings.escalation_max_attempts),
        rate_limiter=container.rate_limiter,
        session_factory=container.session_factory,
    )


@router.post(
    "/{adapter}/events",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive a device event",
    description=(
        "Accepts a vendor payload for the named adapter. The `careos` adapter accepts the "
        "CareOS Device Event Contract directly (JSON Schema: packages/contracts/schemas/"
        "careos-device-event.v1.schema.json). Idempotent on `event_id`: redelivery returns "
        "the original receipt with `duplicate=true` and never creates a second incident."
    ),
)
async def ingest_event(
    adapter: Annotated[str, Path(pattern=r"^[a-z0-9_\-]{1,40}$")],
    payload: Annotated[dict[str, Any], Body()],
    uow: UowDep,
    service: Annotated[DeviceGatewayService, Depends(get_gateway_service)],
    context: AuditContextDep,
    gateway_key: Annotated[str | None, Security(gateway_key_scheme)],
) -> IngestResponse:
    credential = await service.authenticate(uow.session, gateway_key, context, adapter)
    result = await service.ingest(uow, credential, adapter, payload, context)
    return IngestResponse(**asdict(result))
