"""Signed Twilio webhook endpoints (status callbacks and trusted-contact keypad input).

Every request must carry a valid ``X-Twilio-Signature`` computed over the *configured*
public URL and the form fields; unsigned, mis-signed or misdirected requests are rejected
and counted. Tenant safety: the caller supplies only an opaque call id — organisation and
incident always come from the call row it maps to, and the CallSid must match the one
bound to that call. Processing is idempotent (ADR-017) and never changes incident status.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, Query, Request, Response, status

from careos.api.deps import ContainerDep
from careos.core.logging import get_logger
from careos.core.metrics import TWILIO_WEBHOOK_REJECTIONS
from careos.modules.telephony.security import verify_twilio_signature
from careos.modules.telephony.store import CallControlStore
from careos.modules.telephony.twiml import gather_ack_twiml

log = get_logger(__name__)

router = APIRouter(prefix="/v1/providers/twilio", tags=["telephony (provider webhooks)"])


def _reject(reason: str, status_code: int = status.HTTP_403_FORBIDDEN) -> Response:
    TWILIO_WEBHOOK_REJECTIONS.labels(reason).inc()
    log.warning("telephony.webhook_rejected", failure_category=reason)
    return Response(status_code=status_code)


async def _verified_form(
    request: Request, container: ContainerDep, signature: str | None
) -> dict[str, str] | Response:
    settings = container.settings
    token = settings.twilio_auth_token
    base = settings.twilio_webhook_base_url
    if token is None or base is None:
        return _reject("not_configured", status.HTTP_503_SERVICE_UNAVAILABLE)
    # Twilio posts application/x-www-form-urlencoded; parse it directly (no multipart).
    body = (await request.body()).decode("utf-8", errors="replace")
    form = dict(parse_qsl(body, keep_blank_values=True))
    # Reconstruct the exact URL Twilio was given from configuration, never from Host.
    url = f"{base.rstrip('/')}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    if not verify_twilio_signature(token.get_secret_value(), url, form, signature):
        return _reject("bad_signature" if signature else "missing_signature")
    return form


def _store(container: ContainerDep) -> CallControlStore:
    return CallControlStore(
        container.session_factory,
        container.realtime,
        publish_timeout_seconds=container.settings.realtime_publish_timeout_seconds,
    )


@router.post("/voice/status", include_in_schema=False)
async def voice_status(
    request: Request,
    container: ContainerDep,
    call: Annotated[uuid.UUID, Query()],
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> Response:
    form = await _verified_form(request, container, x_twilio_signature)
    if isinstance(form, Response):
        return form
    sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    if not sid or not call_status:
        return _reject("malformed")
    applied = await _store(container).apply_provider_status(
        call,
        provider_sid=sid,
        twilio_status=call_status,
        sequence=form.get("SequenceNumber"),
        call_duration=form.get("CallDuration"),
        error_code=form.get("ErrorCode"),
    )
    if not applied.accepted:
        code = status.HTTP_404_NOT_FOUND if applied.reason == "unknown_call" else None
        return _reject(applied.reason, code or status.HTTP_403_FORBIDDEN)
    log.info(
        "telephony.status_callback",
        call_id=str(call),
        provider="twilio",
        provider_call_sid=sid,
        result=applied.reason,
        call_status=applied.status.value if applied.status else None,
        organisation_id=str(applied.organisation_id) if applied.organisation_id else None,
        incident_id=str(applied.incident_id) if applied.incident_id else None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/voice/gather", include_in_schema=False)
async def voice_gather(
    request: Request,
    container: ContainerDep,
    call: Annotated[uuid.UUID, Query()],
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> Response:
    form = await _verified_form(request, container, x_twilio_signature)
    if isinstance(form, Response):
        return form
    sid = form.get("CallSid", "")
    if not sid:
        return _reject("malformed")
    applied = await _store(container).apply_gather_response(
        call, provider_sid=sid, digits=form.get("Digits", "")
    )
    if not applied.accepted:
        code = status.HTTP_404_NOT_FOUND if applied.reason == "unknown_call" else None
        return _reject(applied.reason, code or status.HTTP_403_FORBIDDEN)
    log.info(
        "telephony.gather_response",
        call_id=str(call),
        provider="twilio",
        provider_call_sid=sid,
        result=applied.reason,
        organisation_id=str(applied.organisation_id) if applied.organisation_id else None,
    )
    return Response(content=gather_ack_twiml(), media_type="text/xml")
