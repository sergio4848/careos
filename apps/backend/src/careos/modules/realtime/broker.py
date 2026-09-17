"""Realtime fan-out across processes.

API replicas and the worker publish to Redis pub/sub; every API replica subscribes and
delivers to its local sockets. If Redis is unavailable the API delivers locally so a
single-node deployment keeps working; consoles additionally poll while disconnected.
Redis is *never* on the critical path of incident creation or escalation.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from careos.contracts.realtime import RealtimeMessage
from careos.core.logging import get_logger
from careos.core.metrics import REALTIME_PUBLISH_FAILURES
from careos.modules.realtime.hub import ConnectionHub

log = get_logger(__name__)

CHANNEL_PREFIX = "careos:realtime:org:"
_POLL_SECONDS = 5.0


def channel_for(message: RealtimeMessage) -> str:
    return f"{CHANNEL_PREFIX}{message.organisation_id}"


class LocalRealtimeBroker:
    """Single-process broker (tests, local development without Redis)."""

    def __init__(self, hub: ConnectionHub | None) -> None:
        self._hub = hub

    async def publish(self, message: RealtimeMessage) -> None:
        if self._hub is not None:
            await self._hub.deliver(message)


class RedisRealtimeBroker:
    def __init__(self, redis: Redis, hub: ConnectionHub | None) -> None:
        self._redis = redis
        self._hub = hub

    async def publish(self, message: RealtimeMessage) -> None:
        try:
            await self._redis.publish(channel_for(message), message.model_dump_json())
        except (RedisError, OSError) as exc:
            REALTIME_PUBLISH_FAILURES.inc()
            log.warning("realtime.publish_failed", error=type(exc).__name__)
            if self._hub is not None:
                await self._hub.deliver(message)

    async def run_subscriber(self) -> None:
        """Deliver messages from Redis to local sockets until cancelled. Reconnects with backoff."""
        if self._hub is None:
            return
        backoff = 1.0
        while True:
            try:
                async with self._redis.pubsub(ignore_subscribe_messages=True) as pubsub:
                    await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
                    log.info("realtime.subscriber_connected")
                    backoff = 1.0
                    while True:
                        # Poll with a timeout instead of an indefinite blocking read, so the
                        # client's periodic health check (PING) runs and a silently dropped
                        # connection (e.g. Redis failover) is detected and re-established.
                        raw = await pubsub.get_message(timeout=_POLL_SECONDS)
                        if raw is None or raw.get("type") != "pmessage":
                            continue
                        try:
                            message = RealtimeMessage.model_validate_json(raw["data"])
                        except ValidationError:
                            log.warning("realtime.invalid_message_dropped")
                            continue
                        await self._hub.deliver(message)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                log.warning("realtime.subscriber_disconnected", error=type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
