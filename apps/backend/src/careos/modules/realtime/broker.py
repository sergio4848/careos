"""Realtime fan-out across processes.

API replicas and the worker publish to Redis pub/sub; every API replica subscribes and
delivers to its local sockets. Redis is *never* on the critical path of incident creation or
escalation: publishing happens after the database commit and every failure is contained here.

Degraded behaviour (ADR-008, ADR-012):
* publish fails -> the circuit opens, the message is delivered to this process's sockets
  only, ``careos_realtime_delivery_failures_total`` is incremented and the broker reports
  ``degraded`` in ``/ready``;
* while the circuit is open, publishes skip Redis immediately (no per-request timeouts);
* consoles reconcile from the REST API, so a lost notification delays but never hides data.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from careos.contracts.realtime import RealtimeMessage
from careos.core.circuit import CircuitBreaker
from careos.core.logging import get_logger
from careos.core.metrics import REALTIME_DELIVERY_FAILURES
from careos.modules.realtime.hub import ConnectionHub

log = get_logger(__name__)

CHANNEL_PREFIX = "careos:realtime:org:"
_POLL_SECONDS = 5.0

RealtimeState = Literal["ok", "degraded", "local_only"]


def channel_for(message: RealtimeMessage) -> str:
    return f"{CHANNEL_PREFIX}{message.organisation_id}"


class LocalRealtimeBroker:
    """Single-process broker (tests, local development without Redis)."""

    def __init__(self, hub: ConnectionHub | None) -> None:
        self._hub = hub

    @property
    def state(self) -> RealtimeState:
        return "local_only"

    async def publish(self, message: RealtimeMessage) -> None:
        if self._hub is not None:
            await self._hub.deliver(message)


class RedisRealtimeBroker:
    def __init__(
        self, redis: Redis, hub: ConnectionHub | None, circuit: CircuitBreaker | None = None
    ) -> None:
        self._redis = redis
        self._hub = hub
        self.circuit = circuit or CircuitBreaker("redis")

    @property
    def state(self) -> RealtimeState:
        return "degraded" if self.circuit.degraded else "ok"

    async def publish(self, message: RealtimeMessage) -> None:
        if not self.circuit.allow():
            REALTIME_DELIVERY_FAILURES.labels(stage="broker_unavailable").inc()
            await self._deliver_locally(message)
            return
        try:
            await self._redis.publish(channel_for(message), message.model_dump_json())
        except (RedisError, OSError) as exc:
            self.circuit.record_failure()
            REALTIME_DELIVERY_FAILURES.labels(stage="broker_publish").inc()
            log.warning(
                "realtime.publish_failed",
                failure_category=type(exc).__name__,
                message_type=message.type.value,
                organisation_id=str(message.organisation_id),
                incident_id=str(message.incident_id) if message.incident_id else None,
                fallback="local_sockets_only",
            )
            await self._deliver_locally(message)
            return
        self.circuit.record_success()

    async def _deliver_locally(self, message: RealtimeMessage) -> None:
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
                log.warning("realtime.subscriber_disconnected", failure_category=type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
