"""Redis fan-out between processes (worker/API replicas -> sockets on another API replica)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
from redis.asyncio import Redis

from careos.contracts.realtime import RealtimeMessage, RealtimeMessageType
from careos.modules.realtime.broker import RedisRealtimeBroker
from careos.modules.realtime.hub import ConnectionHub


class RecordingSocket:
    def __init__(self) -> None:
        self.received: list[str] = []
        self.event = asyncio.Event()

    async def send_text(self, data: str) -> None:
        self.received.append(data)
        self.event.set()


def message(organisation_id: uuid.UUID) -> RealtimeMessage:
    return RealtimeMessage(
        type=RealtimeMessageType.INCIDENT_UPDATED,
        organisation_id=organisation_id,
        incident_id=uuid.uuid4(),
        payload={"status": "ESCALATED", "priority": "CRITICAL", "reference": "INC-1"},
    )


async def test_message_published_by_worker_reaches_socket_on_api_instance() -> None:
    server = FakeServer()
    organisation_id = uuid.uuid4()
    api_hub = ConnectionHub()
    socket = RecordingSocket()
    await api_hub.register(organisation_id, socket)  # type: ignore[arg-type]

    api_broker = RedisRealtimeBroker(FakeRedis(server=server), api_hub)
    worker_broker = RedisRealtimeBroker(FakeRedis(server=server), hub=None)  # worker has no sockets

    subscriber = asyncio.create_task(api_broker.run_subscriber())
    try:
        await asyncio.sleep(0.1)  # let the subscription register
        sent = message(organisation_id)
        await worker_broker.publish(sent)
        await asyncio.wait_for(socket.event.wait(), timeout=3)
    finally:
        subscriber.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber

    assert RealtimeMessage.model_validate_json(socket.received[0]).id == sent.id


async def test_publish_falls_back_to_local_delivery_when_redis_is_unreachable() -> None:
    organisation_id = uuid.uuid4()
    hub = ConnectionHub()
    socket = RecordingSocket()
    await hub.register(organisation_id, socket)  # type: ignore[arg-type]
    unreachable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    try:
        await RedisRealtimeBroker(unreachable, hub).publish(message(organisation_id))
    finally:
        await unreachable.aclose()
    assert len(socket.received) == 1
