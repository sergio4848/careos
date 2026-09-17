"""Circuit breaker, Redis fallbacks and Unit of Work after-commit semantics."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from careos.contracts.realtime import RealtimeMessage, RealtimeMessageType
from careos.core.circuit import CircuitBreaker
from careos.core.rate_limit import RedisRateLimiter
from careos.db.uow import UnitOfWork
from careos.modules.realtime.broker import RedisRealtimeBroker
from careos.modules.realtime.hub import ConnectionHub
from tests.integration.helpers import metric


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RecordingSocket:
    def __init__(self) -> None:
        self.received: list[str] = []

    async def send_text(self, data: str) -> None:
        self.received.append(data)


class DownRedis:
    """Every command fails like an unreachable server; counts how often it was tried."""

    def __init__(self) -> None:
        self.attempts = 0

    async def publish(self, *_: Any) -> int:
        self.attempts += 1
        raise RedisConnectionError("connection refused")

    def pipeline(self, **_: Any) -> Any:
        self.attempts += 1
        raise RedisConnectionError("connection refused")


def message(organisation_id: uuid.UUID) -> RealtimeMessage:
    return RealtimeMessage(
        type=RealtimeMessageType.INCIDENT_UPDATED, organisation_id=organisation_id
    )


def test_circuit_opens_on_failure_and_half_opens_after_cooldown() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker("redis", reset_after_seconds=5, clock=clock)
    assert circuit.state == "closed" and circuit.allow() and not circuit.degraded

    circuit.record_failure()
    assert circuit.state == "open" and not circuit.allow() and circuit.degraded
    clock.now += 4.9
    assert not circuit.allow()
    clock.now += 0.2
    assert circuit.state == "half_open" and circuit.allow()  # one probe allowed through
    circuit.record_failure()
    assert circuit.state == "open"
    clock.now += 5
    circuit.record_success()
    assert circuit.state == "closed" and not circuit.degraded


async def test_broker_skips_redis_while_circuit_is_open_and_still_delivers_locally() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker("redis", reset_after_seconds=5, clock=clock)
    redis = DownRedis()
    hub = ConnectionHub()
    socket = RecordingSocket()
    organisation_id = uuid.uuid4()
    await hub.register(organisation_id, socket)  # type: ignore[arg-type]
    broker = RedisRealtimeBroker(redis, hub, circuit)  # type: ignore[arg-type]
    unavailable_before = metric(
        "careos_realtime_delivery_failures_total", stage="broker_unavailable"
    )

    for _ in range(5):
        await broker.publish(message(organisation_id))

    assert redis.attempts == 1  # one failure opened the circuit; no timeout paid per message
    assert len(socket.received) == 5  # local consoles still notified
    assert broker.state == "degraded"
    assert (
        metric("careos_realtime_delivery_failures_total", stage="broker_unavailable")
        - unavailable_before
        == 4
    )
    clock.now += 5
    await broker.publish(message(organisation_id))
    assert redis.attempts == 2  # half-open probe


async def test_rate_limiter_uses_local_fallback_while_redis_is_down() -> None:
    circuit = CircuitBreaker("redis", reset_after_seconds=60)
    redis = DownRedis()
    limiter = RedisRateLimiter(redis, circuit=circuit)  # type: ignore[arg-type]
    decisions = [await limiter.hit("login:1.2.3.4", limit=3, window_seconds=60) for _ in range(5)]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]  # still enforced
    assert redis.attempts == 1


class FakeSession:
    def __init__(self, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        if self.fail_commit:
            raise ConnectionResetError("database went away during COMMIT")
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class Publisher:
    def __init__(self, behaviour: str = "ok") -> None:
        self.behaviour = behaviour
        self.published: list[RealtimeMessage] = []

    async def publish(self, message: RealtimeMessage) -> None:
        if self.behaviour == "raise":
            raise RuntimeError("broker bug")
        if self.behaviour == "hang":
            await asyncio.Event().wait()
        self.published.append(message)


async def test_commit_failure_releases_no_notifications_or_hooks() -> None:
    publisher = Publisher()
    uow = UnitOfWork(FakeSession(fail_commit=True), publisher)  # type: ignore[arg-type]
    hooks: list[str] = []
    uow.notify(message(uuid.uuid4()))
    uow.after_commit(lambda: hooks.append("counted"))
    with pytest.raises(ConnectionResetError):
        await uow.commit()
    assert publisher.published == [] and hooks == []
    # Nothing leaks into a later, successful commit on the same unit of work either.
    uow.session.fail_commit = False  # type: ignore[attr-defined]
    await uow.commit()
    assert publisher.published == [] and hooks == []


async def test_rollback_discards_pending_side_effects() -> None:
    publisher = Publisher()
    session = FakeSession()
    uow = UnitOfWork(session, publisher)  # type: ignore[arg-type]
    hooks: list[str] = []
    uow.notify(message(uuid.uuid4()))
    uow.after_commit(lambda: hooks.append("counted"))
    await uow.rollback()
    await uow.commit()
    assert session.rolled_back and publisher.published == [] and hooks == []


@pytest.mark.parametrize("behaviour", ["raise", "hang"])
async def test_after_commit_publish_failure_is_contained_and_bounded(behaviour: str) -> None:
    session = FakeSession()
    uow = UnitOfWork(
        session,  # type: ignore[arg-type]
        Publisher(behaviour),
        publish_timeout_seconds=0.1,
    )
    hooks: list[str] = []
    uow.after_commit(lambda: hooks.append("counted"))
    uow.notify(message(uuid.uuid4()))
    uow.notify(message(uuid.uuid4()))
    before = metric("careos_realtime_delivery_failures_total", stage="after_commit")
    started = time.monotonic()
    await uow.commit()  # must not raise
    assert time.monotonic() - started < 1.0
    assert session.committed and hooks == ["counted"]
    assert metric("careos_realtime_delivery_failures_total", stage="after_commit") - before == 2


async def test_failing_after_commit_hook_does_not_block_notifications() -> None:
    publisher = Publisher()
    uow = UnitOfWork(FakeSession(), publisher)  # type: ignore[arg-type]

    def broken_hook() -> None:
        raise ValueError("metrics bug")

    uow.after_commit(broken_hook)
    uow.notify(message(uuid.uuid4()))
    await uow.commit()
    assert len(publisher.published) == 1
