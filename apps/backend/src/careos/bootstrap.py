"""Composition root shared by the API and the worker.

Everything with side effects (DB engine, Redis, providers) is constructed here from
settings and injected. Tests build the same container with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from careos.contracts.realtime import RealtimePublisher
from careos.core.config import Settings
from careos.core.rate_limit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from careos.db.session import create_engine, create_session_factory
from careos.db.uow import UnitOfWork
from careos.modules.ai_orchestrator.orchestrator import (
    AIOrchestrator,
    AIProvider,
    MockAIProvider,
    UnavailableAIProvider,
)
from careos.modules.device_gateway.adapters import AdapterRegistry, default_adapters
from careos.modules.identity.service import AuthService
from careos.modules.notification_engine.providers import (
    DisabledNotificationProvider,
    DisabledVoiceProvider,
    MockNotificationProvider,
    MockVoiceProvider,
    NotificationProvider,
    VoiceProvider,
)
from careos.modules.realtime.broker import LocalRealtimeBroker, RedisRealtimeBroker
from careos.modules.realtime.hub import ConnectionHub


@dataclass(slots=True)
class ProviderRegistry:
    voice: VoiceProvider
    notifications: NotificationProvider
    ai: AIOrchestrator


def build_providers(settings: Settings) -> ProviderRegistry:
    voice: VoiceProvider = (
        MockVoiceProvider(settings.mock_voice_outcome)
        if settings.voice_provider == "mock"
        else DisabledVoiceProvider()
    )
    notifications: NotificationProvider = (
        MockNotificationProvider()
        if settings.notification_provider == "mock"
        else DisabledNotificationProvider()
    )
    ai_provider: AIProvider | None = None
    if settings.ai_provider == "mock":
        ai_provider = MockAIProvider()
    elif settings.ai_provider == "mock_unavailable":
        ai_provider = UnavailableAIProvider()
    return ProviderRegistry(
        voice=voice,
        notifications=notifications,
        ai=AIOrchestrator(ai_provider, timeout_seconds=settings.provider_timeout_seconds),
    )


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis | None
    rate_limiter: RateLimiter
    hub: ConnectionHub | None
    realtime: RealtimePublisher
    providers: ProviderRegistry
    adapters: AdapterRegistry
    auth: AuthService
    #: Transport the SOS simulator uses to reach the gateway (overridden in tests).
    simulator_transport: httpx.AsyncBaseTransport | None = None

    def uow(self, session: AsyncSession) -> UnitOfWork:
        return UnitOfWork(session, self.realtime)

    async def aclose(self) -> None:
        await self.engine.dispose()
        if self.redis is not None:
            await self.redis.aclose()


def build_container(
    settings: Settings,
    *,
    with_hub: bool,
    providers: ProviderRegistry | None = None,
    redis: Redis | None = None,
    engine: AsyncEngine | None = None,
) -> Container:
    engine = engine or create_engine(settings)
    session_factory = create_session_factory(engine)
    if redis is None and settings.redis_url:
        redis = Redis.from_url(
            settings.redis_url,
            socket_timeout=2,
            socket_connect_timeout=2,
            health_check_interval=30,
        )
    hub = ConnectionHub() if with_hub else None
    realtime: RealtimePublisher
    rate_limiter: RateLimiter
    if redis is not None:
        realtime = RedisRealtimeBroker(redis, hub)
        rate_limiter = RedisRateLimiter(redis)
    else:
        realtime = LocalRealtimeBroker(hub)
        rate_limiter = InMemoryRateLimiter()
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        rate_limiter=rate_limiter,
        hub=hub,
        realtime=realtime,
        providers=providers or build_providers(settings),
        adapters=default_adapters(),
        auth=AuthService(settings, rate_limiter, session_factory),
    )
