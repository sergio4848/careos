"""TwilioVoiceProvider — the only place CareOS talks to Twilio's REST API.

Safety switches (ADR-014):
* ``CAREOS_REAL_TELEPHONY_ENABLED`` is false by default; the provider refuses every call.
* Outside production, the destination must be in ``CAREOS_TELEPHONY_ALLOWED_NUMBERS``.
* Destinations must be E.164; the From number comes from configuration.

``place_call`` keeps the executor's contract: it returns only when the call reached a
terminal state (or the bounded maximum duration passed). Progress arrives via signed
status webhooks and the media bridge, which persist to PostgreSQL; this provider simply
waits on that state. Region and edge are configurable — UK-first deployments use Twilio's
Ireland region (``ie1`` / ``dublin``) so call control stays in the EU; media-stream audio
still traverses the configured edge (documented limitation, ADR-014).
"""

from __future__ import annotations

import asyncio
import time

import httpx

from careos.core.config import Settings
from careos.core.logging import get_logger
from careos.modules.notification_engine.models import TERMINAL_CALL_STATUSES, CallStatus
from careos.modules.notification_engine.providers import (
    ProviderError,
    VoiceCallOutcome,
    VoiceCallRequest,
    VoiceCallResult,
)
from careos.modules.telephony.security import is_e164, mask_number
from careos.modules.telephony.store import CallControlStore, CallSnapshot
from careos.modules.telephony.twiml import (
    service_user_stream_twiml,
    status_callback_url,
    trusted_contact_gather_twiml,
)

log = get_logger(__name__)

_OUTCOME_BY_STATUS: dict[CallStatus, VoiceCallOutcome] = {
    CallStatus.NO_ANSWER: VoiceCallOutcome.NO_ANSWER,
    CallStatus.BUSY: VoiceCallOutcome.BUSY,
    CallStatus.CANCELLED: VoiceCallOutcome.CANCELLED,
    CallStatus.TIMED_OUT: VoiceCallOutcome.TIMED_OUT,
}


class TwilioVoiceProvider:
    name = "twilio"
    #: The media bridge runs the AI conversation inside the call; the executor must not
    #: start a second, out-of-band AI check-in.
    carries_ai_session = True

    def __init__(
        self,
        settings: Settings,
        store: CallControlStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._transport = transport

    # Executor bound: API accept + ringing + the whole conversation, plus a small margin.
    @property
    def operation_deadline_seconds(self) -> float:
        s = self._settings
        return (
            s.twilio_api_timeout_seconds
            + s.twilio_ring_timeout_seconds
            + s.voice_call_max_duration_seconds
            + 15
        )

    def _api_base(self) -> str:
        host = "api"
        if self._settings.twilio_edge:
            host += f".{self._settings.twilio_edge}"
        if self._settings.twilio_region:
            host += f".{self._settings.twilio_region}"
        return f"https://{host}.twilio.com"

    def _guard(self, request: VoiceCallRequest) -> None:
        s = self._settings
        if not s.real_telephony_enabled:
            raise ProviderError("real telephony is disabled (CAREOS_REAL_TELEPHONY_ENABLED)")
        if not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number):
            raise ProviderError("Twilio credentials are not configured")
        if not s.twilio_webhook_base_url:
            raise ProviderError("CAREOS_TWILIO_WEBHOOK_BASE_URL is not configured")
        if not is_e164(request.to_number):
            raise ProviderError("destination is not a valid E.164 number")
        if not s.is_production_like and request.to_number not in s.telephony_allowed_numbers:
            raise ProviderError("destination is not in CAREOS_TELEPHONY_ALLOWED_NUMBERS")

    def _twiml(self, request: VoiceCallRequest) -> str:
        base = str(self._settings.twilio_webhook_base_url)
        if request.purpose == "AUTOMATED_WELFARE_CHECK":
            if not request.media_token:
                raise ProviderError("welfare call is missing its media token")
            return service_user_stream_twiml(base, request.media_token)
        return trusted_contact_gather_twiml(
            base,
            request.call_id,
            request.subject_name or "a person in your care circle",
            language=request.language,
        )

    def _credentials(self) -> tuple[str, str]:
        s = self._settings
        if s.twilio_account_sid is None or s.twilio_auth_token is None:
            raise ProviderError("Twilio credentials are not configured")
        return s.twilio_account_sid, s.twilio_auth_token.get_secret_value()

    async def _create_call(self, request: VoiceCallRequest) -> str:
        s = self._settings
        account_sid, auth_token = self._credentials()
        base = str(s.twilio_webhook_base_url)
        form = {
            "To": request.to_number,
            "From": str(s.twilio_from_number),
            "Twiml": self._twiml(request),
            "Timeout": str(s.twilio_ring_timeout_seconds),
            "TimeLimit": str(s.voice_call_max_duration_seconds),
            "StatusCallback": status_callback_url(base, request.call_id),
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": "initiated ringing answered completed",
        }
        url = f"{self._api_base()}/2010-04-01/Accounts/{account_sid}/Calls.json"
        try:
            async with httpx.AsyncClient(
                timeout=s.twilio_api_timeout_seconds,
                auth=(account_sid, auth_token),
                transport=self._transport,
            ) as client:
                response = await client.post(url, data=form)
        except httpx.TimeoutException as exc:
            raise TimeoutError("Twilio API timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Twilio API unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            code = (
                response.json().get("code")
                if "json" in response.headers.get("content-type", "")
                else None
            )
            raise ProviderError(f"Twilio rejected the call (HTTP {response.status_code}, {code})")
        sid = str(response.json().get("sid") or "")
        if not sid:
            raise ProviderError("Twilio response carried no call SID")
        return sid

    async def _hangup(self, sid: str) -> None:
        s = self._settings
        account_sid, auth_token = self._credentials()
        url = f"{self._api_base()}/2010-04-01/Accounts/{account_sid}/Calls/{sid}.json"
        try:
            async with httpx.AsyncClient(
                timeout=s.twilio_api_timeout_seconds,
                auth=(account_sid, auth_token),
                transport=self._transport,
            ) as client:
                await client.post(url, data={"Status": "completed"})
        except httpx.HTTPError as exc:  # best-effort: the TimeLimit still bounds the call
            log.warning("telephony.hangup_failed", failure_category=type(exc).__name__)

    async def cancel_call(self, provider_call_sid: str) -> None:
        await self._hangup(provider_call_sid)

    async def place_call(self, request: VoiceCallRequest) -> VoiceCallResult:
        self._guard(request)
        sid = await self._create_call(request)
        await self._store.bind_provider_sid(
            request.call_id,
            sid,
            from_number_masked=mask_number(str(self._settings.twilio_from_number)),
        )
        log.info(
            "telephony.call_requested",
            provider=self.name,
            call_id=str(request.call_id),
            incident_id=str(request.incident_id),
            organisation_id=str(request.organisation_id),
            provider_call_sid=sid,
        )
        deadline = time.monotonic() + (
            self._settings.twilio_ring_timeout_seconds
            + self._settings.voice_call_max_duration_seconds
            + 10
        )
        snapshot: CallSnapshot | None = None
        while time.monotonic() < deadline:
            snapshot = await self._store.snapshot(request.call_id)
            if snapshot is not None and snapshot.status in TERMINAL_CALL_STATUSES:
                break
            await asyncio.sleep(self._settings.voice_call_poll_interval_seconds)
        else:
            # Maximum duration exceeded without a terminal callback: end it deliberately.
            await self._hangup(sid)
            await self._store.mark_timed_out(request.call_id)
            snapshot = await self._store.snapshot(request.call_id)
        if snapshot is None:
            raise ProviderError("call record disappeared while waiting for completion")
        if snapshot.status is CallStatus.FAILED:
            raise ProviderError("Twilio reported the call as failed")
        if snapshot.status is CallStatus.COMPLETED:
            outcome = VoiceCallOutcome.ANSWERED if snapshot.answered else VoiceCallOutcome.NO_ANSWER
        else:
            outcome = _OUTCOME_BY_STATUS.get(snapshot.status, VoiceCallOutcome.NO_ANSWER)
        return VoiceCallResult(
            outcome=outcome,
            acknowledged=snapshot.acknowledged,
            provider_call_id=snapshot.provider_call_sid or sid,
        )
