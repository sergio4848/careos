"""FastAPI application factory (HTTP + WebSocket delivery layer)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from careos.api.exception_handlers import register_exception_handlers
from careos.api.health import router as health_router
from careos.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from careos.bootstrap import Container, build_container
from careos.core.config import Settings, get_settings
from careos.core.logging import configure_logging, get_logger
from careos.modules.audit.router import router as audit_router
from careos.modules.dashboard.router import router as dashboard_router
from careos.modules.device_gateway.router import router as gateway_router
from careos.modules.devices.router import router as devices_router
from careos.modules.escalation_engine.router import router as escalation_router
from careos.modules.identity.router import auth_router, users_router
from careos.modules.incident_engine.router import router as incidents_router
from careos.modules.organisations.router import router as organisations_router
from careos.modules.realtime.broker import RedisRealtimeBroker
from careos.modules.realtime.router import router as realtime_router
from careos.modules.service_users.router import router as service_users_router
from careos.modules.simulator.router import router as simulator_router
from careos.worker.runner import run_loops

log = get_logger(__name__)

MAX_BODY_BYTES = 256 * 1024


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())
    configure_logging(settings.log_level, settings.log_json)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or build_container(settings, with_hub=True)
        background: list[asyncio.Task[None]] = []
        realtime = app.state.container.realtime
        if isinstance(realtime, RedisRealtimeBroker):
            background.append(
                asyncio.create_task(realtime.run_subscriber(), name="realtime-subscriber")
            )
        stop_worker = asyncio.Event()
        if settings.embedded_worker:
            log.warning("api.embedded_worker_enabled", note="development only")
            background.append(
                asyncio.create_task(
                    run_loops(app.state.container, stop_worker, embedded=True),
                    name="embedded-worker",
                )
            )
        log.info("api.started", environment=settings.environment.value)
        try:
            yield
        finally:
            stop_worker.set()
            for task in background:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if owned:
                await app.state.container.aclose()

    docs_enabled = not settings.is_production_like
    app = FastAPI(
        title="CareOS API",
        version="0.1.0",
        summary="Telecare & safety incident orchestration platform",
        description=(
            "CareOS does not diagnose, predict illness or recommend treatment. "
            "AI features are assistive only; incident handling is deterministic."
        ),
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    if container is not None:
        app.state.container = container

    register_exception_handlers(app)

    for router in (
        health_router,
        auth_router,
        users_router,
        organisations_router,
        service_users_router,
        devices_router,
        gateway_router,
        incidents_router,
        escalation_router,
        dashboard_router,
        audit_router,
        realtime_router,
    ):
        app.include_router(router)
    if settings.simulator_enabled:
        app.include_router(simulator_router)

    # Order: outermost first at runtime = last added.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookie_secure)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After", "Date"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)
    return app
