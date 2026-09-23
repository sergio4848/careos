"""Shared fixtures.

Integration tests run against a real PostgreSQL database (``CAREOS_TEST_DATABASE_URL``):
tenant isolation, row locks, partial unique indexes and append-only triggers are
database behaviour and must not be faked. The schema is created by the real Alembic
migrations once per session; tables are truncated between tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from careos.api.app import create_app
from careos.bootstrap import Container, ProviderRegistry, build_container
from careos.core.config import Settings
from careos.db.registry import metadata
from careos.modules.ai_orchestrator.orchestrator import AIOrchestrator, MockAIProvider
from careos.modules.ai_orchestrator.voice import MockAIVoiceProvider, VoiceAIOrchestrator
from careos.modules.notification_engine.providers import (
    MockNotificationProvider,
    MockVoiceProvider,
)
from tests.factories import SIMULATOR_KEY, TEST_PASSWORD, ClientFactory, Tenant, TenantFactory

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "CAREOS_TEST_DATABASE_URL",
    "postgresql+asyncpg://careos:careos-dev-only@localhost:5432/careos_test",
)
TEST_SECRET = "test-secret-key-0123456789abcdef-0123456789abcdef"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "database_url": TEST_DATABASE_URL,
        "database_pool_size": 5,
        "redis_url": None,
        "secret_key": TEST_SECRET,
        "log_json": False,
        "log_level": "WARNING",
        "simulator_enabled": True,
        "simulator_gateway_key": SIMULATOR_KEY,
        "gateway_internal_url": "http://testserver",
        "cors_allowed_origins": ["http://localhost:3000"],
        "login_max_attempts": 5,
        "provider_timeout_seconds": 2,
        "escalation_retry_base_seconds": 0,
        "escalation_max_attempts": 3,
        # Telephony/AI voice test wiring: webhook URL building needs a base; short waits.
        "twilio_webhook_base_url": "http://testserver",
        "voice_call_poll_interval_seconds": 0.02,
        "voice_call_max_duration_seconds": 30,
        "ai_connect_timeout_seconds": 2,
        "ai_response_timeout_seconds": 2,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Reset the test schema and apply the real migrations (in a subprocess: no nested loops)."""
    reset = textwrap.dedent(
        f"""
        import asyncio, asyncpg
        async def main():
            conn = await asyncpg.connect({TEST_DATABASE_URL.replace("+asyncpg", "")!r})
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            await conn.close()
        asyncio.run(main())
        """
    )
    env = {**os.environ, "CAREOS_DATABASE_URL": TEST_DATABASE_URL, "CAREOS_SECRET_KEY": TEST_SECRET}
    subprocess.run([sys.executable, "-c", reset], check=True, env=env)  # noqa: S603
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
    )


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def providers(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(
        voice=MockVoiceProvider("no_answer"),
        notifications=MockNotificationProvider(),
        ai=AIOrchestrator(MockAIProvider(), timeout_seconds=settings.provider_timeout_seconds),
        voice_ai=VoiceAIOrchestrator(
            MockAIVoiceProvider(), connect_timeout_seconds=settings.ai_connect_timeout_seconds
        ),
    )


@pytest.fixture
async def container(
    migrated_database: None, settings: Settings, providers: ProviderRegistry
) -> AsyncIterator[Container]:
    built = build_container(settings, with_hub=True, providers=providers)
    async with built.engine.begin() as connection:
        tables = ", ".join(
            f'"{table.name}"'
            for table in reversed(metadata.sorted_tables)
            if not table.info.get("reference_data")  # seeded by migrations, never truncated
        )
        await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    yield built
    await built.aclose()


@pytest.fixture
def app(container: Container) -> FastAPI:
    application = create_app(container.settings, container)
    container.simulator_transport = httpx.ASGITransport(app=application)
    return application


@pytest.fixture
async def client_factory(app: FastAPI) -> AsyncIterator[ClientFactory]:
    clients: list[httpx.AsyncClient] = []

    async def make(email: str | None = None, password: str = TEST_PASSWORD) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.10", 5000)),
            base_url="http://testserver",
        )
        clients.append(client)
        if email is not None:
            response = await client.post(
                "/v1/auth/login", json={"email": email, "password": password}
            )
            assert response.status_code == 200, response.text
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client

    yield make
    for client in clients:
        await client.aclose()


@pytest.fixture
def factory(container: Container) -> TenantFactory:
    return TenantFactory(container.session_factory, container.settings)


@pytest.fixture
async def tenant(factory: TenantFactory) -> Tenant:
    return await factory.create_tenant(
        slug="demo-care-uk", name="Demo Care UK", key_prefix="demo0001"
    )


@pytest.fixture
async def other_tenant(factory: TenantFactory) -> Tenant:
    return await factory.create_tenant(
        slug="northshire-telecare", name="Northshire Telecare Ltd", key_prefix="north001"
    )
