"""OpenAI Realtime voice provider (speech in, speech out over one WebSocket).

Telephone audio is G.711 μ-law at 8 kHz, which the Realtime API accepts natively, so no
transcoding happens in CareOS. The model is configurable (``OPENAI_REALTIME_MODEL``);
no model name appears in business code. The structured advisory is produced by the model
calling the single ``report_outcome`` tool — free text is never parsed for decisions.

Never used in CI or automated tests: those run :class:`MockAIVoiceProvider`.
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from careos.core.logging import get_logger
from careos.modules.ai_orchestrator.models import UrgencySignal
from careos.modules.ai_orchestrator.voice import (
    VOICE_SAFETY_INSTRUCTIONS,
    AIVoiceEvent,
    VoiceResult,
    VoiceSessionContext,
)

log = get_logger(__name__)

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

REPORT_OUTCOME_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "report_outcome",
    "description": (
        "Report the structured advisory outcome of this welfare conversation exactly once, "
        "when it concludes. This is advisory metadata for the human care team; it does not "
        "change the alert."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contact_established": {"type": "boolean"},
            "requested_human_help": {"type": ["boolean", "null"]},
            "urgency_signal": {
                "type": "string",
                "enum": [signal.value for signal in UrgencySignal],
            },
            "language": {"type": "string"},
            "summary": {"type": "string", "maxLength": 500},
        },
        "required": ["contact_established", "urgency_signal", "summary"],
    },
}


class RealtimeSocket(Protocol):
    """The slice of a websocket connection this provider uses (fake-able in tests)."""

    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


SocketConnector = Callable[[str, dict[str, str]], Awaitable[RealtimeSocket]]


async def _default_connector(url: str, headers: dict[str, str]) -> RealtimeSocket:
    import websockets  # provided by uvicorn[standard]; imported lazily (never in tests)

    # websockets' ClientConnection satisfies RealtimeSocket structurally.
    connection: RealtimeSocket = await websockets.connect(
        url, additional_headers=headers, max_size=2**20
    )
    return connection


def _session_update(context: VoiceSessionContext, model: str) -> dict[str, Any]:
    instructions = (
        f"{VOICE_SAFETY_INSTRUCTIONS}\n\n"
        f"The person's preferred name is {context.preferred_name}. Speak {context.language}. "
        f"The alert type is {context.trigger_type}. Open with exactly this greeting: "
        f'"{context.greeting}"'
    )
    return {
        "type": "session.update",
        "session": {
            "model": model,
            "instructions": instructions,
            "modalities": ["audio", "text"],
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "turn_detection": {"type": "server_vad"},
            "tools": [REPORT_OUTCOME_TOOL],
        },
    }


def parse_outcome_arguments(raw: str, fallback_language: str) -> VoiceResult | None:
    try:
        data = json.loads(raw)
        urgency = UrgencySignal(data.get("urgency_signal", "NONE"))
        return VoiceResult(
            contact_established=bool(data["contact_established"]),
            requested_human_help=data.get("requested_human_help"),
            urgency_signal=urgency,
            language=str(data.get("language") or fallback_language)[:16],
            summary=str(data.get("summary") or "")[:500] or None,
        )
    except (ValueError, KeyError, TypeError):
        return None


class OpenAIRealtimeSession:
    def __init__(self, socket: RealtimeSocket, context: VoiceSessionContext) -> None:
        self._socket = socket
        self._context = context
        self._closed = False

    async def send_audio(self, payload: bytes) -> None:
        if self._closed:
            return
        message = {"type": "input_audio_buffer.append", "audio": base64.b64encode(payload).decode()}
        await self._socket.send(json.dumps(message))

    async def notify_dtmf(self, digit: str) -> None:
        if self._closed:
            return
        await self._socket.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"[keypad digit {digit}]"}],
                    },
                }
            )
        )

    async def events(self) -> AsyncIterator[AIVoiceEvent]:
        while True:
            try:
                raw = await self._socket.recv()
            except Exception:
                yield AIVoiceEvent(type="closed")
                return
            try:
                event = json.loads(raw)
                event_type = event.get("type", "")
            except (ValueError, TypeError):
                log.warning("ai_voice.malformed_event_dropped", provider="openai-realtime")
                continue
            if event_type in ("response.output_audio.delta", "response.audio.delta"):
                try:
                    yield AIVoiceEvent(type="audio", audio=base64.b64decode(event["delta"]))
                except (KeyError, ValueError, TypeError):
                    log.warning("ai_voice.malformed_event_dropped", provider="openai-realtime")
                continue
            if event_type == "input_audio_buffer.speech_started":
                yield AIVoiceEvent(type="speech_started")
                continue
            if event_type == "response.function_call_arguments.done":
                if event.get("name") == "report_outcome":
                    result = parse_outcome_arguments(
                        event.get("arguments", ""), self._context.language
                    )
                    if result is None:
                        yield AIVoiceEvent(type="error", error="malformed_outcome")
                    else:
                        yield AIVoiceEvent(type="result", result=result)
                continue
            if event_type == "error":
                yield AIVoiceEvent(
                    type="error", error=str(event.get("error", {}).get("code", "provider_error"))
                )
                continue
            # Every other event type (transcripts are disabled) is intentionally ignored.

    async def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            await self._socket.close()


class OpenAIRealtimeProvider:
    name = "openai-realtime"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        connector: SocketConnector | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._connector = connector or _default_connector

    async def connect(self, context: VoiceSessionContext) -> OpenAIRealtimeSession:
        url = f"{OPENAI_REALTIME_URL}?model={self._model}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        socket = await self._connector(url, headers)
        await socket.send(json.dumps(_session_update(context, self._model)))
        # Ask the model to speak the configured greeting immediately.
        await socket.send(json.dumps({"type": "response.create"}))
        return OpenAIRealtimeSession(socket, context)
