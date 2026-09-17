"""Against a real Redis server (CI service container). Skipped when CAREOS_TEST_REDIS_URL is unset.

Unit tests cover Redis failure with fakes; this proves the healthy path on the real protocol:
cross-process pub/sub fan-out, shared rate limits and readiness.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest
from redis.asyncio import Redis

from careos.bootstrap import Container, ProviderRegistry, build_container
from careos.contracts.realtime import RealtimeMessage, RealtimeMessageType
from careos.core.rate_limit import RedisRateLimiter
from careos.modules.realtime.broker import RedisRealtimeBroker
from careos.modules.realtime.hub import ConnectionHub
from tests.factories import Tenant, sos_event
from tests.integration.helpers import asgi_client, send_event

REDIS_URL = os.environ.get("CAREOS_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(not REDIS_URL, reason="CAREOS_TEST_REDIS_URL not set")


def redis_client() -> Redis:
    assert REDIS_URL is not None
    return Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


class RecordingSocket:
    def __init__(self) -> None:
        self.received: list[str] = []
        self.arrived = asyncio.Event()

    async def send_text(self, data: str) -> None:
        self.received.append(data)
        self.arrived.set()


async def test_worker_publish_reaches_api_socket_through_real_redis() -> None:
    api_redis, worker_redis = redis_client(), redis_client()
    organisation_id = uuid.uuid4()
    hub = ConnectionHub()
    socket = RecordingSocket()
    await hub.register(organisation_id, socket)  # type: ignore[arg-type]
    api = RedisRealtimeBroker(api_redis, hub)
    worker = RedisRealtimeBroker(worker_redis, hub=None)
    subscriber = asyncio.create_task(api.run_subscriber())
    try:
        await asyncio.sleep(0.3)
        sent = RealtimeMessage(
            type=RealtimeMessageType.INCIDENT_UPDATED, organisation_id=organisation_id
        )
        await worker.publish(sent)
        await asyncio.wait_for(socket.arrived.wait(), timeout=5)
    finally:
        subscriber.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber
        await api_redis.aclose()
        await worker_redis.aclose()
    assert RealtimeMessage.model_validate_json(socket.received[0]).id == sent.id
    assert worker.state == "ok"


async def test_rate_limit_is_shared_between_instances() -> None:
    first, second = redis_client(), redis_client()
    key = f"test:{uuid.uuid4().hex}"
    try:
        limiters = (RedisRateLimiter(first), RedisRateLimiter(second))
        allowed = [
            (await limiters[i % 2].hit(key, limit=3, window_seconds=60)).allowed for i in range(4)
        ]
        await limiters[0].reset(key)
    finally:
        await first.aclose()
        await second.aclose()
    assert allowed == [True, True, True, False]


async def test_sos_and_readiness_with_real_redis(
    container: Container, providers: ProviderRegistry, tenant: Tenant
) -> None:
    redis = redis_client()
    healthy = build_container(
        container.settings, with_hub=True, providers=providers, engine=container.engine, redis=redis
    )
    try:
        async with asgi_client(healthy) as client:
            response = await send_event(client, tenant, sos_event(tenant.device_external_id))
            ready = (await client.get("/ready")).json()
    finally:
        await redis.aclose()
    assert response.status_code == 202
    assert ready["status"] == "ready" and ready["redis"] == "ok" and ready["realtime"] == "ok"
