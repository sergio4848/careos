"""OpenAIRealtimeProvider against a scripted fake socket. No real OpenAI calls, ever."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid

from careos.modules.ai_orchestrator.models import UrgencySignal
from careos.modules.ai_orchestrator.openai_realtime import (
    OpenAIRealtimeProvider,
    parse_outcome_arguments,
)
from careos.modules.ai_orchestrator.voice import (
    VOICE_SAFETY_INSTRUCTIONS,
    AIVoiceEvent,
    VoiceAIOrchestrator,
    VoiceSessionContext,
)


class FakeSocket:
    def __init__(self, incoming: list[str | bytes]) -> None:
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue[str | bytes] = asyncio.Queue()
        for item in incoming:
            self._incoming.put_nowait(item)
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str | bytes:
        if self.closed and self._incoming.empty():
            raise ConnectionError("socket closed")
        try:
            return self._incoming.get_nowait()
        except asyncio.QueueEmpty as exc:
            raise ConnectionError("no more scripted frames") from exc

    async def close(self) -> None:
        self.closed = True


def context() -> VoiceSessionContext:
    return VoiceSessionContext(
        organisation_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        call_id=uuid.uuid4(),
        preferred_name="Margaret",
        language="en-GB",
        trigger_type="SOS_BUTTON",
        greeting="Hello Margaret. This is the automated CareOS safety assistant.",
    )


def make_provider(incoming: list[str | bytes], model: str = "model-from-config"):
    sockets: list[FakeSocket] = []

    async def connector(url: str, headers: dict[str, str]) -> FakeSocket:
        assert f"model={model}" in url  # the configured model, never a hard-coded name
        assert headers["Authorization"].startswith("Bearer ")
        socket = FakeSocket(incoming)
        sockets.append(socket)
        return socket

    provider = OpenAIRealtimeProvider(api_key="test-key", model=model, connector=connector)
    return provider, sockets


async def collect(session, limit: int = 10) -> list[AIVoiceEvent]:
    events: list[AIVoiceEvent] = []
    async for event in session.events():
        events.append(event)
        if len(events) >= limit:
            break
    return events


async def test_connect_configures_session_safety_and_greeting() -> None:
    provider, sockets = make_provider([])
    session = await provider.connect(context())
    socket = sockets[0]
    update = socket.sent[0]
    assert update["type"] == "session.update"
    configured = update["session"]
    assert configured["model"] == "model-from-config"
    assert configured["input_audio_format"] == "g711_ulaw"  # telephone audio, no transcoding
    assert configured["output_audio_format"] == "g711_ulaw"
    assert configured["turn_detection"] == {"type": "server_vad"}
    assert [tool["name"] for tool in configured["tools"]] == ["report_outcome"]
    instructions = configured["instructions"]
    assert VOICE_SAFETY_INSTRUCTIONS in instructions
    assert "automated" in instructions and "not a human operator" in instructions
    for forbidden in ("diagnosis", "medication", "resolved, closed or cancelled"):
        assert forbidden in VOICE_SAFETY_INSTRUCTIONS  # the boundary is spelled out
    assert "Hello Margaret." in instructions  # configurable greeting is passed through
    assert socket.sent[1] == {"type": "response.create"}  # speak the greeting immediately
    await session.close()
    assert socket.closed


async def test_audio_flows_both_ways() -> None:
    chunk = base64.b64encode(b"\xff" * 160).decode()
    provider, sockets = make_provider(
        [json.dumps({"type": "response.output_audio.delta", "delta": chunk})]
    )
    session = await provider.connect(context())
    await session.send_audio(b"\x00" * 160)
    appended = [m for m in sockets[0].sent if m["type"] == "input_audio_buffer.append"]
    assert base64.b64decode(appended[0]["audio"]) == b"\x00" * 160
    events = await collect(session, limit=1)
    assert events[0].type == "audio" and events[0].audio == b"\xff" * 160


async def test_speech_started_maps_to_barge_in_event() -> None:
    provider, _ = make_provider([json.dumps({"type": "input_audio_buffer.speech_started"})])
    session = await provider.connect(context())
    events = await collect(session, limit=1)
    assert events[0].type == "speech_started"


async def test_report_outcome_tool_call_becomes_structured_result() -> None:
    arguments = json.dumps(
        {
            "contact_established": True,
            "requested_human_help": True,
            "urgency_signal": "ASSISTANCE_REQUESTED",
            "language": "en-GB",
            "summary": "Margaret answered and requested assistance.",
        }
    )
    provider, _ = make_provider(
        [
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "report_outcome",
                    "arguments": arguments,
                }
            )
        ]
    )
    session = await provider.connect(context())
    events = await collect(session, limit=1)
    result = events[0].result
    assert events[0].type == "result" and result is not None
    assert result.contact_established is True and result.requested_human_help is True
    assert result.urgency_signal is UrgencySignal.ASSISTANCE_REQUESTED
    assert result.summary == "Margaret answered and requested assistance."


async def test_malformed_events_are_dropped_and_bad_outcomes_flagged() -> None:
    provider, _ = make_provider(
        [
            "not json at all {{{",
            json.dumps({"no_type": True}),
            json.dumps({"type": "response.output_audio.delta"}),  # missing delta
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "report_outcome",
                    "arguments": "{broken json",
                }
            ),
        ]
    )
    session = await provider.connect(context())
    events = await collect(session, limit=2)
    assert events[0].type == "error" and events[0].error == "malformed_outcome"
    assert events[1].type == "closed"  # scripted frames exhausted → disconnect


async def test_provider_error_event_and_disconnect_are_surfaced() -> None:
    provider, _ = make_provider([json.dumps({"type": "error", "error": {"code": "rate_limited"}})])
    session = await provider.connect(context())
    events = await collect(session, limit=2)
    assert events[0].type == "error" and events[0].error == "rate_limited"
    assert events[1].type == "closed"


def test_outcome_parsing_is_defensive() -> None:
    assert parse_outcome_arguments("{broken", "en-GB") is None
    assert parse_outcome_arguments(json.dumps({"urgency_signal": "NONE"}), "en-GB") is None
    parsed = parse_outcome_arguments(
        json.dumps(
            {
                "contact_established": True,
                "urgency_signal": "NOT_A_REAL_SIGNAL",
                "summary": "x",
            }
        ),
        "en-GB",
    )
    assert parsed is None  # unknown urgency values are rejected, never guessed
    minimal = parse_outcome_arguments(
        json.dumps({"contact_established": False, "urgency_signal": "NONE", "summary": ""}),
        "en-GB",
    )
    assert minimal is not None
    assert minimal.language == "en-GB" and minimal.summary is None


async def test_orchestrator_contains_connect_failures_and_timeouts() -> None:
    async def failing_connector(url: str, headers: dict[str, str]):
        raise ConnectionError("dns failure")

    failing = OpenAIRealtimeProvider(api_key="k", model="m", connector=failing_connector)
    orchestrator = VoiceAIOrchestrator(failing, connect_timeout_seconds=0.5)
    assert await orchestrator.connect(context()) is None  # never raises into the call

    async def hanging_connector(url: str, headers: dict[str, str]):
        await asyncio.Event().wait()

    hanging = OpenAIRealtimeProvider(api_key="k", model="m", connector=hanging_connector)  # type: ignore[arg-type]
    orchestrator = VoiceAIOrchestrator(hanging, connect_timeout_seconds=0.1)
    assert await orchestrator.connect(context()) is None

    disabled = VoiceAIOrchestrator(None, connect_timeout_seconds=1)
    assert disabled.enabled is False and disabled.provider_name == "disabled"
    assert await disabled.connect(context()) is None
