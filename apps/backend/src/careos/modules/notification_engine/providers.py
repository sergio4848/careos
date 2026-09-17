"""Provider abstractions for outbound voice calls and notifications.

No critical workflow references a vendor. Real integrations (Twilio, Vonage, a UK
telecare IVR platform, GOV.UK Notify, ...) implement these protocols and are selected by
configuration. Providers are always called with a timeout by the escalation executor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

from careos.modules.notification_engine.models import NotificationChannel


class ProviderError(Exception):
    """A provider could not complete the request. Triggers retry and fail-safe escalation."""


class VoiceCallOutcome(StrEnum):
    ANSWERED = "ANSWERED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


CallPurpose = Literal["AUTOMATED_WELFARE_CHECK", "TRUSTED_CONTACT_ALERT"]


@dataclass(frozen=True, slots=True)
class VoiceCallRequest:
    organisation_id: uuid.UUID
    incident_id: uuid.UUID
    call_id: uuid.UUID
    to_number: str
    purpose: CallPurpose
    language: str = "en-GB"
    #: Single-use secret linking the Twilio media stream to this call (welfare calls only).
    media_token: str | None = None
    #: Spoken to a trusted contact: "an active safety alert for {subject_name}".
    subject_name: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceCallResult:
    outcome: VoiceCallOutcome
    #: The callee confirmed they are responding / are safe (e.g. pressed 1 on the IVR).
    acknowledged: bool
    provider_call_id: str | None = None


class VoiceProvider(Protocol):
    """``place_call`` returns only when the call is over (bounded by the executor).

    Optional attributes real providers may add:

    * ``carries_ai_session`` — the provider runs the AI conversation inside the call
      (media bridge); the executor must not start a second out-of-band check-in.
    * ``operation_deadline_seconds`` — how long the executor should allow for one call.
    * ``cancel_call(provider_call_sid)`` — best-effort hangup for operator STOP.
    """

    name: str

    async def place_call(self, request: VoiceCallRequest) -> VoiceCallResult: ...


@dataclass(frozen=True, slots=True)
class NotificationRequest:
    organisation_id: uuid.UUID
    incident_id: uuid.UUID | None
    channel: NotificationChannel
    to: str
    template_key: str
    variables: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NotificationResult:
    provider_message_id: str | None


class NotificationProvider(Protocol):
    name: str

    async def send(self, request: NotificationRequest) -> NotificationResult: ...


# --------------------------------------------------------------------------- mocks


MockVoiceBehaviour = Literal["no_answer", "answered_acknowledged", "failure"]


class MockVoiceProvider:
    """Deterministic development/test provider. Places no real calls."""

    name = "mock-voice"

    def __init__(self, behaviour: MockVoiceBehaviour = "no_answer") -> None:
        self.behaviour = behaviour
        self.calls: list[VoiceCallRequest] = []

    async def place_call(self, request: VoiceCallRequest) -> VoiceCallResult:
        self.calls.append(request)
        call_id = f"mock-call-{request.call_id.hex[:12]}"
        if self.behaviour == "failure":
            raise ProviderError("mock voice provider configured to fail")
        if self.behaviour == "answered_acknowledged":
            return VoiceCallResult(
                VoiceCallOutcome.ANSWERED, acknowledged=True, provider_call_id=call_id
            )
        return VoiceCallResult(
            VoiceCallOutcome.NO_ANSWER, acknowledged=False, provider_call_id=call_id
        )


class DisabledVoiceProvider:
    name = "disabled-voice"

    async def place_call(self, request: VoiceCallRequest) -> VoiceCallResult:
        raise ProviderError("voice provider is disabled")


class MockNotificationProvider:
    name = "mock-notify"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[NotificationRequest] = []

    async def send(self, request: NotificationRequest) -> NotificationResult:
        if self.fail:
            raise ProviderError("mock notification provider configured to fail")
        self.sent.append(request)
        return NotificationResult(provider_message_id=f"mock-msg-{uuid.uuid4().hex[:12]}")


class DisabledNotificationProvider:
    name = "disabled-notify"

    async def send(self, request: NotificationRequest) -> NotificationResult:
        raise ProviderError("notification provider is disabled")
