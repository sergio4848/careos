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
from careos.core.circuit import CircuitBreaker
from careos.core.config import Settings
from careos.core.rate_limit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from careos.db.session import create_engine, create_session_factory
from careos.db.uow import UnitOfWork
from careos.modules.ai_orchestrator.openai_realtime import OpenAIRealtimeProvider
from careos.modules.ai_orchestrator.orchestrator import (
    AIOrchestrator,
    AIProvider,
    MockAIProvider,
    UnavailableAIProvider,
)
from careos.modules.ai_orchestrator.voice import (
    AIVoiceProvider,
    MockAIVoiceProvider,
    VoiceAIOrchestrator,
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
from careos.modules.telephony.provider import TwilioVoiceProvider
from careos.modules.telephony.store import CallControlStore


@dataclass(slots=True)
class ProviderRegistry:
    voice: VoiceProvider
    notifications: NotificationProvider
    ai: AIOrchestrator
    #: In-call AI voice sessions (media bridge). None keeps older test wiring working.
    voice_ai: VoiceAIOrchestrator | None = None


def build_voice_ai(settings: Settings) -> VoiceAIOrchestrator:
    provider: AIVoiceProvider | None = None
    if settings.ai_voice_provider == "openai_realtime":
        if settings.openai_api_key is None:
            raise ValueError("CAREOS_AI_VOICE_PROVIDER=openai_realtime requires OPENAI_API_KEY")
        provider = OpenAIRealtimeProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_realtime_model,
        )
    elif settings.ai_voice_provider == "mock":
        provider = MockAIVoiceProvider()
    return VoiceAIOrchestrator(
        provider, connect_timeout_seconds=settings.ai_connect_timeout_seconds
    )


def build_providers(
    settings: Settings, *, call_store: CallControlStore | None = None
) -> ProviderRegistry:
    voice: VoiceProvider
    if settings.voice_provider == "twilio":
        if call_store is None:
            raise ValueError("the twilio voice provider needs database and realtime wiring")
        voice = TwilioVoiceProvider(settings, call_store)
    elif settings.voice_provider == "mock":
        voice = MockVoiceProvider(settings.mock_voice_outcome)
    else:
        voice = DisabledVoiceProvider()
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
        voice_ai=build_voice_ai(settings),
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
    #: Shared by everything that uses Redis, so one failure makes all of them skip it at once.
    redis_circuit: CircuitBreaker | None = None
    #: Transport the SOS simulator uses to reach the gateway (overridden in tests).
    simulator_transport: httpx.AsyncBaseTransport | None = None

    def uow(self, session: AsyncSession) -> UnitOfWork:
        return UnitOfWork(
            session,
            self.realtime,
            publish_timeout_seconds=self.settings.realtime_publish_timeout_seconds,
        )

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
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            health_check_interval=30,
        )
    hub = ConnectionHub() if with_hub else None
    realtime: RealtimePublisher
    rate_limiter: RateLimiter
    redis_circuit: CircuitBreaker | None = None
    if redis is not None:
        redis_circuit = CircuitBreaker(
            "redis", reset_after_seconds=settings.redis_circuit_reset_seconds
        )
        realtime = RedisRealtimeBroker(redis, hub, redis_circuit)
        rate_limiter = RedisRateLimiter(redis, circuit=redis_circuit)
    else:
        realtime = LocalRealtimeBroker(hub)
        rate_limiter = InMemoryRateLimiter()
    if providers is None:
        call_store = CallControlStore(
            session_factory,
            realtime,
            publish_timeout_seconds=settings.realtime_publish_timeout_seconds,
        )
        providers = build_providers(settings, call_store=call_store)
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        rate_limiter=rate_limiter,
        hub=hub,
        realtime=realtime,
        providers=providers,
        adapters=default_adapters(),
        auth=AuthService(settings, rate_limiter, session_factory),
        redis_circuit=redis_circuit,
    )
