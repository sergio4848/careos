"""Twilio bidirectional Media Streams endpoint and the telephony↔AI audio bridge.

Dedicated WebSocket, completely separate from the operator dashboard socket:

    Twilio <Connect><Stream>  ⇄  TelephonyMediaBridge  ⇄  AIVoiceProvider session

Authentication (ADR-015): Twilio presents the single-use opaque token CareOS put into the
TwiML as a <Parameter>. The token maps (server-side) to exactly one call, incident and
organisation — a client can never choose those. Unknown, reused or expired tokens close
the socket without explanation. Payload sizes and concurrent connections are bounded.

Audio frames are G.711 μ-law 8 kHz, base64 inside JSON, exactly as Twilio sends them; the
bridge relays them and NEVER stores them (ADR-018). Barge-in: when the AI provider signals
that the person started speaking, the bridge sends Twilio a ``clear`` so queued AI audio
stops immediately.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette import status as ws_status
from starlette.websockets import WebSocketDisconnect, WebSocketState

from careos.bootstrap import Container
from careos.core.config import Settings
from careos.core.logging import get_logger
from careos.core.metrics import (
    AI_VOICE_FAILURES,
    AI_VOICE_LATENCY,
    AI_VOICE_SESSIONS,
    MEDIA_SESSIONS,
)
from careos.modules.ai_orchestrator.voice import (
    AIVoiceSession,
    VoiceAIOrchestrator,
    VoiceResult,
    VoiceSessionContext,
)
from careos.modules.telephony.store import CallControlStore, MediaContext

log = get_logger(__name__)

router = APIRouter()

_HANDSHAKE_TIMEOUT_SECONDS = 10.0
CLOSE_POLICY = ws_status.WS_1008_POLICY_VIOLATION
CLOSE_TOO_BIG = 1009


class ConnectionGate:
    """Process-wide cap on concurrent media streams."""

    def __init__(self) -> None:
        self.active = 0

    def acquire(self, limit: int) -> bool:
        if self.active >= limit:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


connection_gate = ConnectionGate()


def format_greeting(settings: Settings, preferred_name: str) -> str:
    try:
        return settings.voice_greeting_template.format(preferred_name=preferred_name)
    except (KeyError, IndexError, ValueError):
        return (
            f"Hello {preferred_name}. This is the automated CareOS safety assistant "
            "responding to your alert. I am not a human operator."
        )


class TelephonyMediaBridge:
    def __init__(
        self,
        websocket: WebSocket,
        *,
        store: CallControlStore,
        orchestrator: VoiceAIOrchestrator,
        settings: Settings,
    ) -> None:
        self._ws = websocket
        self._store = store
        self._orchestrator = orchestrator
        self._settings = settings
        self._stream_sid: str | None = None
        self._result: VoiceResult | None = None
        self._failure: str | None = None
        self._first_audio_at: float | None = None

    # ------------------------------------------------------------------ handshake

    async def _read_message(self) -> dict[str, Any] | None:
        raw = await self._ws.receive_text()
        if len(raw) > self._settings.media_max_message_bytes:
            raise _MalformedMessage("oversized")
        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise _MalformedMessage("invalid_json") from exc
        if not isinstance(message, dict):
            raise _MalformedMessage("invalid_shape")
        return message

    async def _await_start(self) -> dict[str, Any]:
        async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SECONDS):
            while True:
                message = await self._read_message()
                if message is None:
                    continue
                event = message.get("event")
                if event == "connected":
                    continue
                if event == "start":
                    return message
                raise _MalformedMessage("unexpected_before_start")

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        await self._ws.accept()
        if not connection_gate.acquire(self._settings.media_max_connections):
            MEDIA_SESSIONS.labels("rejected_limit").inc()
            await self._ws.close(code=CLOSE_POLICY)
            return
        try:
            await self._run_gated()
        finally:
            connection_gate.release()

    async def _run_gated(self) -> None:
        try:
            start = await self._await_start()
        except (_MalformedMessage, TimeoutError, WebSocketDisconnect):
            MEDIA_SESSIONS.labels("rejected_auth").inc()
            await self._close(CLOSE_POLICY)
            return
        payload = start.get("start") or {}
        token = str((payload.get("customParameters") or {}).get("token") or "")
        self._stream_sid = str(payload.get("streamSid") or start.get("streamSid") or "")
        context = await self._store.attach_media_stream(token) if token else None
        if context is None or not self._stream_sid:
            MEDIA_SESSIONS.labels("rejected_auth").inc()
            await self._close(CLOSE_POLICY)
            return
        MEDIA_SESSIONS.labels("accepted").inc()
        await self._run_call(context)

    async def _run_call(self, context: MediaContext) -> None:
        self._context_ref = context
        ai_session_id = await self._store.start_ai_session(
            context, provider=self._orchestrator.provider_name
        )
        voice_context = VoiceSessionContext(
            organisation_id=context.organisation_id,
            incident_id=context.incident_id,
            call_id=context.call_id,
            preferred_name=context.preferred_name,
            language=context.language,
            trigger_type=context.trigger_type,
            greeting=format_greeting(self._settings, context.preferred_name),
        )
        session = (
            await self._orchestrator.connect(voice_context) if self._orchestrator.enabled else None
        )
        provider = self._orchestrator.provider_name
        if session is None:
            self._failure = "ai_unavailable" if self._orchestrator.enabled else "ai_disabled"
            AI_VOICE_FAILURES.labels(provider, "connect").inc()
        started = time.monotonic()
        try:
            if session is not None:
                async with asyncio.timeout(self._settings.voice_call_max_duration_seconds):
                    await self._pump(session, provider, started)
        except TimeoutError:
            self._failure = self._failure or "max_call_duration"
        except Exception as exc:
            self._failure = "unexpected"
            log.exception(
                "telephony.media_bridge_error",
                call_id=str(context.call_id),
                incident_id=str(context.incident_id),
                organisation_id=str(context.organisation_id),
                failure_category=type(exc).__name__,
            )
        finally:
            # An abrupt transport teardown cancels this endpoint task; shield the
            # finalisation so the advisory outcome is recorded exactly once regardless.
            finalise = asyncio.create_task(
                self._finalise(session, provider, ai_session_id, context)
            )
            try:
                await asyncio.shield(finalise)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await finalise
                raise

    async def _finalise(
        self,
        session: AIVoiceSession | None,
        provider: str,
        ai_session_id: uuid.UUID,
        context: MediaContext,
    ) -> None:
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        timed_out = self._failure == "max_call_duration"
        if self._result is not None:
            AI_VOICE_SESSIONS.labels(provider, "completed").inc()
        else:
            AI_VOICE_SESSIONS.labels(provider, "timed_out" if timed_out else "failed").inc()
            if self._failure not in (None, "ai_disabled"):
                AI_VOICE_FAILURES.labels(
                    provider, "timeout" if timed_out else self._failure or "unexpected"
                ).inc()
        await self._store.finish_ai_session(
            ai_session_id,
            context,
            result=self._result,
            failure_reason=self._failure,
            timed_out=timed_out,
        )
        await self._close()

    async def _pump(self, session: AIVoiceSession, provider: str, started: float) -> None:
        inbound = asyncio.create_task(self._twilio_loop(session))
        outbound = asyncio.create_task(self._ai_loop(session, provider, started))
        done, pending = await asyncio.wait({inbound, outbound}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            exception = task.exception()
            if exception is not None and not isinstance(exception, WebSocketDisconnect):
                raise exception

    async def _twilio_loop(self, session: AIVoiceSession) -> None:
        while True:
            try:
                message = await self._read_message()
            except WebSocketDisconnect:
                self._failure = self._failure or "call_ended"
                return
            except _MalformedMessage as exc:
                MEDIA_SESSIONS.labels("closed_malformed").inc()
                self._failure = self._failure or f"malformed_media:{exc.reason}"
                return
            if message is None:
                continue
            event = message.get("event")
            if event == "media":
                payload = (message.get("media") or {}).get("payload")
                if isinstance(payload, str):
                    with contextlib.suppress(ValueError):
                        await session.send_audio(base64.b64decode(payload))
            elif event == "dtmf":
                digit = str((message.get("dtmf") or {}).get("digit") or "")
                if digit:
                    await self._store.record_dtmf(self._context_ref, digit)
                    await session.notify_dtmf(digit)
            elif event == "stop":
                self._failure = self._failure or "call_ended"
                return
            elif event in ("mark", "connected"):
                continue
            # unknown events are ignored (Twilio adds new ones over time)

    async def _ai_loop(self, session: AIVoiceSession, provider: str, started: float) -> None:
        first_response_deadline = started + self._settings.ai_response_timeout_seconds
        events = session.events()
        while True:
            timeout = None
            if self._first_audio_at is None:
                timeout = max(0.1, first_response_deadline - time.monotonic())
            try:
                async with asyncio.timeout(timeout):
                    event = await anext(events)
            except TimeoutError:
                self._failure = "ai_timeout"
                return
            except StopAsyncIteration:
                self._failure = self._failure or "ai_closed"
                return
            if event.type == "audio" and event.audio:
                if self._first_audio_at is None:
                    self._first_audio_at = time.monotonic()
                    AI_VOICE_LATENCY.labels(provider).observe(self._first_audio_at - started)
                await self._send_media(event.audio)
            elif event.type == "speech_started":
                await self._send_clear()  # barge-in: drop queued AI audio immediately
            elif event.type == "result" and event.result is not None:
                self._result = event.result
                return
            elif event.type == "error":
                self._failure = event.error or "provider_error"
                return
            elif event.type == "closed":
                self._failure = self._failure or "ai_disconnected"
                return

    # ------------------------------------------------------------------ outbound frames

    async def _send_media(self, audio: bytes) -> None:
        if self._ws.application_state != WebSocketState.CONNECTED:
            return
        await self._ws.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": self._stream_sid,
                    "media": {"payload": base64.b64encode(audio).decode()},
                }
            )
        )
        await self._ws.send_text(
            json.dumps(
                {
                    "event": "mark",
                    "streamSid": self._stream_sid,
                    "mark": {"name": f"careos-{uuid.uuid4().hex[:8]}"},
                }
            )
        )

    async def _send_clear(self) -> None:
        if self._ws.application_state != WebSocketState.CONNECTED:
            return
        await self._ws.send_text(json.dumps({"event": "clear", "streamSid": self._stream_sid}))

    async def _close(self, code: int = ws_status.WS_1000_NORMAL_CLOSURE) -> None:
        with contextlib.suppress(Exception):
            if self._ws.application_state == WebSocketState.CONNECTED:
                await self._ws.close(code=code)

    # set in _run_call before the loops need it
    _context_ref: MediaContext


class _MalformedMessage(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@router.websocket("/v1/providers/twilio/media")
async def twilio_media(websocket: WebSocket) -> None:
    container: Container = websocket.app.state.container
    bridge = TelephonyMediaBridge(
        websocket,
        store=CallControlStore(
            container.session_factory,
            container.realtime,
            publish_timeout_seconds=container.settings.realtime_publish_timeout_seconds,
        ),
        orchestrator=container.providers.voice_ai
        or VoiceAIOrchestrator(None, connect_timeout_seconds=1.0),
        settings=container.settings,
    )
    await bridge.run()
