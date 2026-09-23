"""AI voice sessions — assistive, optional and never safety-critical (ADR-016).

The bridge streams telephone audio to an :class:`AIVoiceProvider` session and back. The
only durable output is a small structured advisory (:class:`VoiceResult`). There is no
API through which a voice session can change incident status, priority, assignment,
resolution or the escalation schedule; automated tests and database constraints enforce
this (ADR-013, ADR-016).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from careos.core.logging import get_logger
from careos.modules.ai_orchestrator.models import UrgencySignal

log = get_logger(__name__)

#: Hard behavioural boundary given to every voice model. Mirrors ADR-006/ADR-016.
VOICE_SAFETY_INSTRUCTIONS = (
    "You are the CareOS automated safety assistant on a telephone call with a person who "
    "raised a personal safety alert. You MUST begin by disclosing that you are an automated "
    "assistant, not a human operator. You may: greet the person by their preferred name, "
    "check how they are, ask whether they want human assistance, ask whether someone should "
    "be contacted, and summarise the conversation. You must speak clearly and briefly. "
    "You must NEVER: give a medical diagnosis, give treatment or medication advice, declare "
    "the person safe or well, tell them emergency help is unnecessary, or claim the alert "
    "has been resolved, closed or cancelled. You cannot change the alert in any way; a human "
    "care team always reviews it. If the person describes urgent danger, reassure them that "
    "their care team has already been alerted and is being escalated to. When the "
    "conversation concludes (or the person stops responding), call the report_outcome tool "
    "exactly once with your structured advisory summary."
)


@dataclass(frozen=True, slots=True)
class VoiceSessionContext:
    """Minimal, purpose-limited context (ADR-018). Never the full service-user record."""

    organisation_id: uuid.UUID
    incident_id: uuid.UUID
    call_id: uuid.UUID
    preferred_name: str
    language: str
    trigger_type: str
    greeting: str


@dataclass(frozen=True, slots=True)
class VoiceResult:
    """Structured advisory outcome of a voice conversation. Metadata only."""

    contact_established: bool
    requested_human_help: bool | None
    urgency_signal: UrgencySignal
    language: str
    summary: str | None


AIVoiceEventType = Literal["audio", "speech_started", "result", "error", "closed"]


@dataclass(frozen=True, slots=True)
class AIVoiceEvent:
    type: AIVoiceEventType
    audio: bytes = b""
    result: VoiceResult | None = None
    error: str | None = None


class AIVoiceSession(Protocol):
    async def send_audio(self, payload: bytes) -> None: ...

    async def notify_dtmf(self, digit: str) -> None: ...

    def events(self) -> AsyncIterator[AIVoiceEvent]: ...

    async def close(self) -> None: ...


class AIVoiceProvider(Protocol):
    name: str

    async def connect(self, context: VoiceSessionContext) -> AIVoiceSession: ...


class VoiceAIOrchestrator:
    """Bounded, never-raising wrapper around the configured AI voice provider."""

    def __init__(self, provider: AIVoiceProvider | None, *, connect_timeout_seconds: float) -> None:
        self._provider = provider
        self._connect_timeout = connect_timeout_seconds

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    @property
    def provider_name(self) -> str:
        return self._provider.name if self._provider else "disabled"

    async def connect(self, context: VoiceSessionContext) -> AIVoiceSession | None:
        """Open a session, or return None. A failed AI connection never fails the call."""
        if self._provider is None:
            return None
        try:
            async with asyncio.timeout(self._connect_timeout):
                return await self._provider.connect(context)
        except Exception as exc:
            log.warning(
                "ai_voice.connect_failed",
                provider=self._provider.name,
                failure_category="timeout" if isinstance(exc, TimeoutError) else "connect",
                incident_id=str(context.incident_id),
                organisation_id=str(context.organisation_id),
                call_id=str(context.call_id),
            )
            return None


# --------------------------------------------------------------------------- mock

MockVoiceAIBehaviour = Literal[
    "assistance_requested",
    "emergency",
    "no_response",
    "hangs",
    "fails_connect",
    "errors_mid_session",
]

_SILENCE = b"\xff" * 160  # 20 ms of G.711 μ-law silence


@dataclass
class MockAIVoiceSession:
    behaviour: MockVoiceAIBehaviour
    context: VoiceSessionContext
    received_audio: list[bytes] = field(default_factory=list)
    dtmf: list[str] = field(default_factory=list)
    closed: bool = False
    _woke: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_audio(self, payload: bytes) -> None:
        self.received_audio.append(payload)
        if len(self.received_audio) >= 2:
            self._woke.set()

    async def notify_dtmf(self, digit: str) -> None:
        self.dtmf.append(digit)

    async def events(self) -> AsyncIterator[AIVoiceEvent]:
        if self.behaviour == "hangs":
            await asyncio.Event().wait()
        yield AIVoiceEvent(type="audio", audio=_SILENCE * 5)  # spoken greeting
        if self.behaviour == "errors_mid_session":
            yield AIVoiceEvent(type="error", error="provider_error")
            return
        # Wait briefly for the caller to speak, then barge-in fires and a result follows.
        try:
            async with asyncio.timeout(2):
                await self._woke.wait()
            yield AIVoiceEvent(type="speech_started")
        except TimeoutError:
            pass
        if self.behaviour == "no_response":
            yield AIVoiceEvent(
                type="result",
                result=VoiceResult(
                    contact_established=False,
                    requested_human_help=None,
                    urgency_signal=UrgencySignal.NONE,
                    language=self.context.language,
                    summary="No verbal response was captured.",
                ),
            )
            return
        urgency = (
            UrgencySignal.POTENTIAL_EMERGENCY
            if self.behaviour == "emergency"
            else UrgencySignal.ASSISTANCE_REQUESTED
        )
        yield AIVoiceEvent(
            type="result",
            result=VoiceResult(
                contact_established=True,
                requested_human_help=True,
                urgency_signal=urgency,
                language=self.context.language,
                summary=f"{self.context.preferred_name} answered and requested assistance.",
            ),
        )

    async def close(self) -> None:
        self.closed = True
        self._woke.set()


class MockAIVoiceProvider:
    """Deterministic scripted provider for tests and local development. No network."""

    name = "mock-ai-voice"

    def __init__(self, behaviour: MockVoiceAIBehaviour = "assistance_requested") -> None:
        self.behaviour = behaviour
        self.sessions: list[MockAIVoiceSession] = []

    async def connect(self, context: VoiceSessionContext) -> MockAIVoiceSession:
        if self.behaviour == "fails_connect":
            raise ConnectionError("mock AI voice provider configured to fail")
        session = MockAIVoiceSession(behaviour=self.behaviour, context=context)
        self.sessions.append(session)
        return session
