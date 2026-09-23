"""Fixed-window rate limiting.

Redis-backed so limits hold across API replicas. If Redis is unavailable we fall back
to a per-process in-memory limiter rather than failing open or rejecting everything.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from careos.core.circuit import CircuitBreaker
from careos.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision: ...

    async def reset(self, key: str) -> None: ...


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        now = time.monotonic()
        async with self._lock:
            count, window_start = self._buckets.get(key, (0, now))
            if now - window_start >= window_seconds:
                count, window_start = 0, now
            count += 1
            self._buckets[key] = (count, window_start)
            if len(self._buckets) > 50_000:  # bound memory under abuse
                self._buckets.clear()
        retry_after = max(1, int(window_seconds - (now - window_start)))
        return RateLimitDecision(allowed=count <= limit, retry_after_seconds=retry_after)

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._buckets.pop(key, None)


class RedisRateLimiter:
    def __init__(
        self,
        redis: Redis,
        fallback: InMemoryRateLimiter | None = None,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._redis = redis
        self._fallback = fallback or InMemoryRateLimiter()
        self._circuit = circuit or CircuitBreaker("redis")

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        if not self._circuit.allow():
            return await self._fallback.hit(key, limit, window_seconds)
        bucket = int(time.time() // window_seconds)
        redis_key = f"careos:ratelimit:{key}:{bucket}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(redis_key)
                pipe.expire(redis_key, window_seconds + 1)
                count, _ = await pipe.execute()
        except (RedisError, OSError) as exc:
            self._circuit.record_failure()
            log.warning("rate_limiter.redis_unavailable", failure_category=type(exc).__name__)
            return await self._fallback.hit(key, limit, window_seconds)
        self._circuit.record_success()
        retry_after = max(1, window_seconds - int(time.time()) % window_seconds)
        return RateLimitDecision(allowed=int(count) <= limit, retry_after_seconds=retry_after)

    async def reset(self, key: str) -> None:
        if not self._circuit.allow():
            await self._fallback.reset(key)
            return
        try:
            keys = [k async for k in self._redis.scan_iter(match=f"careos:ratelimit:{key}:*")]
            if keys:
                await self._redis.delete(*keys)
        except (RedisError, OSError):
            pass
        await self._fallback.reset(key)
