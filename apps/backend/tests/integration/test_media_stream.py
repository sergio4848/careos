"""Twilio Media Streams bridge: protocol handling, authentication, AI exchange, barge-in.

Uses Starlette's sync TestClient (its own event loop) like the dashboard WebSocket tests,
with the scripted MockAIVoiceProvider — no Twilio, no OpenAI. Database assertions run on
a dedicated engine/loop (``run_db``) because the shared container's engine belongs to the
pytest-asyncio session loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from careos.api.app import create_app
from careos.bootstrap import Container, ProviderRegistry, build_container
from careos.core.config import Settings
from careos.core.time import utcnow
from careos.modules.ai_orchestrator.models import AISession, AISessionStatus, UrgencySignal
from careos.modules.ai_orchestrator.voice import MockAIVoiceProvider, VoiceAIOrchestrator
from careos.modules.escalation_engine.models import ScheduledAction, ScheduledActionStatus
from careos.modules.incident_engine.models import Incident, IncidentEvent
from careos.modules.notification_engine.models import Call, CallStatus, CallTargetType
from careos.modules.telephony.models import CallEvent, CallEventType
from careos.modules.telephony.security import media_token_digest, new_media_token
from tests.factories import ClientFactory, Tenant
from tests.integration.helpers import raise_sos

MEDIA_PATH = "/v1/providers/twilio/media"
FRAME = base64.b64encode(b"\x00" * 160).decode()


@pytest.fixture
def voice_ai() -> MockAIVoiceProvider:
    return MockAIVoiceProvider()


@pytest.fixture
def sync_client(
    settings: Settings,
    providers: ProviderRegistry,
    container: Container,
    voice_ai: MockAIVoiceProvider,
) -> Iterator[TestClient]:
    engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
    providers.voice_ai = VoiceAIOrchestrator(
        voice_ai, connect_timeout_seconds=settings.ai_connect_timeout_seconds
    )
    isolated = build_container(settings, with_hub=True, providers=providers, engine=engine)
    with TestClient(create_app(settings, isolated), base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def voice_call(
    container: Container, tenant: Tenant, client_factory: ClientFactory
) -> tuple[str, uuid.UUID, str]:
    """An active welfare call (with a fresh single-use media token) on a real incident."""
    incident_id = (await raise_sos(await client_factory(), tenant))["incident_id"]
    token = new_media_token()
    call_id = uuid.uuid4()
    async with container.session_factory() as session:
        session.add(
            Call(
                id=call_id,
                organisation_id=tenant.organisation_id,
                incident_id=uuid.UUID(incident_id),
                target_type=CallTargetType.SERVICE_USER,
                service_user_id=tenant.service_user_id,
                provider="twilio",
                provider_call_sid="CA" + "c" * 32,
                status=CallStatus.IN_PROGRESS,
                started_at=utcnow(),
                media_token_digest=media_token_digest(token),
            )
        )
        await session.commit()
    return incident_id, call_id, token


def run_db(settings: Settings, fn: Callable[[async_sessionmaker], Any]) -> Any:
    """Run a DB coroutine on its own loop and engine (safe from sync tests)."""

    async def runner() -> Any:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def get_ai_session(settings: Settings, call_id: uuid.UUID) -> AISession:
    """Fetch the finalised AI session; the bridge may still be finishing after a client-side
    disconnect, so poll briefly instead of racing it."""
    import time

    deadline = time.monotonic() + 5
    while True:

        async def fn(factory: async_sessionmaker) -> AISession | None:
            async with factory() as session:
                return await session.scalar(select(AISession).where(AISession.call_id == call_id))

        row = run_db(settings, fn)
        if row is not None and (
            row.status is not AISessionStatus.STARTED or time.monotonic() > deadline
        ):
            return row
        if time.monotonic() > deadline:
            assert row is not None, "AI session row never appeared"
            return row
        time.sleep(0.05)


def get_call(settings: Settings, call_id: uuid.UUID) -> Call:
    async def fn(factory: async_sessionmaker) -> Call:
        async with factory() as session:
            call = await session.get(Call, call_id)
            assert call is not None
            return call

    return run_db(settings, fn)


def get_call_events(settings: Settings, call_id: uuid.UUID) -> list[CallEventType]:
    async def fn(factory: async_sessionmaker) -> list[CallEventType]:
        async with factory() as session:
            return list(
                await session.scalars(
                    select(CallEvent.event_type)
                    .where(CallEvent.call_id == call_id)
                    .order_by(CallEvent.created_at)
                )
            )

    return run_db(settings, fn)


def get_incident(settings: Settings, incident_id: str) -> tuple[str, str, bool, list[str]]:
    async def fn(factory: async_sessionmaker) -> tuple[str, str, bool, list[str]]:
        async with factory() as session:
            incident = await session.get(Incident, uuid.UUID(incident_id))
            assert incident is not None
            timeline = [
                t.value
                for t in await session.scalars(
                    select(IncidentEvent.event_type).where(IncidentEvent.incident_id == incident.id)
                )
            ]
            return (
                incident.status.value,
                incident.priority.value,
                incident.resolved_at is not None,
                timeline,
            )

    return run_db(settings, fn)


def pending_steps(settings: Settings, incident_id: str) -> int:
    async def fn(factory: async_sessionmaker) -> int:
        async with factory() as session:
            rows = await session.scalars(
                select(ScheduledAction.id).where(
                    ScheduledAction.incident_id == uuid.UUID(incident_id),
                    ScheduledAction.status == ScheduledActionStatus.PENDING,
                )
            )
            return len(list(rows))

    return run_db(settings, fn)


def drive_handshake(socket, token: str, stream_sid: str = "MZstream1") -> None:
    socket.send_text(json.dumps({"event": "connected", "protocol": "Call"}))
    socket.send_text(
        json.dumps(
            {
                "event": "start",
                "streamSid": stream_sid,
                "start": {"streamSid": stream_sid, "customParameters": {"token": token}},
            }
        )
    )


def expect_disconnect(socket, attempts: int = 20) -> None:
    with pytest.raises(WebSocketDisconnect):
        for _ in range(attempts):
            socket.receive_text()


def test_full_media_session_with_barge_in_and_advisory(
    sync_client: TestClient,
    settings: Settings,
    voice_call: tuple[str, uuid.UUID, str],
    voice_ai: MockAIVoiceProvider,
) -> None:
    incident_id, call_id, token = voice_call
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        greeting = json.loads(socket.receive_text())
        assert greeting["event"] == "media" and greeting["streamSid"] == "MZstream1"
        assert base64.b64decode(greeting["media"]["payload"])  # the AI greets first
        assert json.loads(socket.receive_text())["event"] == "mark"
        # The person starts speaking: two inbound frames wake the mock; barge-in follows.
        for _ in range(2):
            socket.send_text(
                json.dumps({"event": "media", "media": {"payload": FRAME, "track": "inbound"}})
            )
        message = json.loads(socket.receive_text())
        assert message == {"event": "clear", "streamSid": "MZstream1"}  # queued AI audio dropped
        expect_disconnect(socket)  # result arrived; bridge finalises and ends the stream

    session_row = get_ai_session(settings, call_id)
    assert session_row.status is AISessionStatus.COMPLETED
    assert session_row.urgency_signal is UrgencySignal.ASSISTANCE_REQUESTED
    assert session_row.contact_established is True and session_row.requested_human_help is True
    assert session_row.advisory_summary and "requested assistance" in session_row.advisory_summary
    assert session_row.language == "en-GB"

    assert get_call(settings, call_id).acknowledged is True  # explicit request, not AI opinion
    events = get_call_events(settings, call_id)
    assert events[:2] == [CallEventType.MEDIA_STREAM_CONNECTED, CallEventType.AI_SESSION_STARTED]
    assert CallEventType.AI_SESSION_COMPLETED in events

    status, priority, resolved, timeline = get_incident(settings, incident_id)
    assert status == "OPEN" and priority == "CRITICAL" and resolved is False
    assert "AI_CALL_STARTED" in timeline and "AI_CALL_COMPLETED" in timeline

    mock_session = voice_ai.sessions[0]
    assert len(mock_session.received_audio) >= 2  # caller audio reached the AI
    assert "automated" in mock_session.context.greeting  # discloses it is not a human
    assert mock_session.closed is True


def test_invalid_reused_or_early_tokens_close_the_socket(
    sync_client: TestClient, settings: Settings, voice_call: tuple[str, uuid.UUID, str]
) -> None:
    _, _, token = voice_call
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, "not-a-real-token")
        with pytest.raises(WebSocketDisconnect) as denial:
            socket.receive_text()
        assert denial.value.code == 1008
    # A valid token authenticates once and is consumed...
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        assert json.loads(socket.receive_text())["event"] == "media"
    # ...so replaying it is refused.
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        with pytest.raises(WebSocketDisconnect) as denial:
            socket.receive_text()
        assert denial.value.code == 1008
    # Anything but connected/start before authentication is refused.
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        socket.send_text(json.dumps({"event": "media", "media": {"payload": FRAME}}))
        expect_disconnect(socket, attempts=3)


def test_malformed_media_message_ends_the_session_safely(
    sync_client: TestClient, settings: Settings, voice_call: tuple[str, uuid.UUID, str]
) -> None:
    incident_id, call_id, token = voice_call
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        socket.receive_text()  # greeting
        socket.receive_text()  # mark
        socket.send_text("this is not json {{{")
        expect_disconnect(socket)
    session_row = get_ai_session(settings, call_id)
    assert session_row.status is AISessionStatus.FAILED
    assert "malformed" in (session_row.failure_reason or "")
    status, _, resolved, _ = get_incident(settings, incident_id)
    assert status == "OPEN" and resolved is False  # a broken stream never marks anyone safe


def test_oversized_media_message_is_rejected(
    sync_client: TestClient, settings: Settings, voice_call: tuple[str, uuid.UUID, str]
) -> None:
    _, call_id, token = voice_call
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        socket.receive_text()
        socket.receive_text()
        socket.send_text("x" * 100_000)
        expect_disconnect(socket)
    assert get_ai_session(settings, call_id).status is AISessionStatus.FAILED


def test_unexpected_disconnect_mid_conversation_is_contained(
    sync_client: TestClient, settings: Settings, voice_call: tuple[str, uuid.UUID, str]
) -> None:
    incident_id, call_id, token = voice_call
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        socket.receive_text()  # greeting
        # The phone connection drops without a stop frame. Observe the finalisation while
        # the test session is still open (its private event loop dies at context exit).
        socket.close(code=1006)
        session_row = get_ai_session(settings, call_id)
    assert session_row.status is AISessionStatus.FAILED
    assert session_row.failure_reason == "call_ended"
    status, _, resolved, timeline = get_incident(settings, incident_id)
    assert status == "OPEN" and resolved is False
    assert "AI_CALL_FAILED" in timeline  # visible to operators, escalation continues


def test_dtmf_is_recorded_and_forwarded_but_never_resolves(
    sync_client: TestClient,
    settings: Settings,
    voice_call: tuple[str, uuid.UUID, str],
    voice_ai: MockAIVoiceProvider,
) -> None:
    incident_id, call_id, token = voice_call
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        socket.receive_text()  # greeting
        socket.receive_text()  # mark
        socket.send_text(json.dumps({"event": "dtmf", "dtmf": {"digit": "1"}}))
        socket.send_text(json.dumps({"event": "mark", "mark": {"name": "m1"}}))
        socket.send_text(json.dumps({"event": "stop", "stop": {}}))
        expect_disconnect(socket)
    assert get_call(settings, call_id).acknowledged is True  # a human pressed the key
    assert CallEventType.DTMF_RECEIVED in get_call_events(settings, call_id)
    assert voice_ai.sessions[0].dtmf == ["1"]
    status, _, resolved, _ = get_incident(settings, incident_id)
    assert status == "OPEN" and resolved is False


@pytest.mark.parametrize(
    ("behaviour", "expected_status", "failure_fragment"),
    [
        ("fails_connect", AISessionStatus.FAILED, "ai_unavailable"),
        ("hangs", AISessionStatus.FAILED, "ai_timeout"),
        ("errors_mid_session", AISessionStatus.FAILED, "provider_error"),
    ],
)
def test_ai_failures_are_contained_and_escalation_is_untouched(
    sync_client: TestClient,
    settings: Settings,
    voice_call: tuple[str, uuid.UUID, str],
    voice_ai: MockAIVoiceProvider,
    behaviour: str,
    expected_status: AISessionStatus,
    failure_fragment: str,
) -> None:
    voice_ai.behaviour = behaviour  # type: ignore[assignment]
    incident_id, call_id, token = voice_call
    steps_before = pending_steps(settings, incident_id)
    with sync_client.websocket_connect(MEDIA_PATH) as socket:
        drive_handshake(socket, token)
        expect_disconnect(socket)
    session_row = get_ai_session(settings, call_id)
    assert session_row.status is expected_status
    assert failure_fragment in (session_row.failure_reason or "")
    # AI outcomes never touch the deterministic schedule, status or priority.
    assert pending_steps(settings, incident_id) == steps_before
    status, priority, resolved, timeline = get_incident(settings, incident_id)
    assert status == "OPEN" and priority == "CRITICAL" and resolved is False
    assert "AI_CALL_FAILED" in timeline
