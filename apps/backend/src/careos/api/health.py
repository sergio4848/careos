"""Liveness, readiness and metrics endpoints.

* ``/health`` — liveness: the process runs and can serve HTTP. Never touches dependencies, so
  a database outage does not make an orchestrator restart healthy API processes in a loop.
* ``/ready`` — can this instance safely process requests? Requires PostgreSQL reachable and a
  compatible schema revision. Redis is optional (rate limiting falls back to per-process,
  realtime to local sockets + client REST reconciliation), so an unavailable Redis is reported
  as degraded but does not remove the instance from service. A lagging escalation worker is
  reported but does not fail API readiness either: restarting the API cannot fix the worker,
  and operators must still be able to handle incidents manually (see
  docs/architecture/reliability.md).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from careos.api.deps import ContainerDep
from careos.bootstrap import Container
from careos.core.logging import get_logger
from careos.core.time import utcnow
from careos.db.schema import EXPECTED_SCHEMA_REVISION, SchemaState, classify_revision, is_safe
from careos.modules.escalation_engine.models import ScheduledAction, ScheduledActionStatus

router = APIRouter(tags=["health"])
log = get_logger(__name__)

_DATABASE_TIMEOUT_SECONDS = 3.0


class HealthStatus(BaseModel):
    status: Literal["ok"]


class ReadinessStatus(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unavailable"]
    migrations: SchemaState
    expected_schema_revision: str
    redis: Literal["ok", "unavailable", "not_configured"]
    realtime: Literal["ok", "degraded", "local_only"]
    escalation_worker: Literal["ok", "lagging", "unknown"]
    #: Degradable capabilities: their outage never removes the SOS ingestion API from
    #: service (docs/architecture/reliability.md).
    telephony: Literal["mock", "disabled", "twilio_simulated", "twilio_live"]
    ai_voice: Literal["mock", "disabled", "openai_realtime"]


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Liveness: the process is running. Does not touch dependencies."""
    return HealthStatus(status="ok")


async def _schema_revision(connection: AsyncConnection) -> str | None:
    try:
        async with connection.begin_nested():
            return await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return None


async def _escalation_lagging(container: Container, connection: AsyncConnection) -> bool:
    cutoff = utcnow() - timedelta(seconds=container.settings.escalation_overdue_after_seconds)
    overdue = exists().where(
        or_(
            and_(
                ScheduledAction.status == ScheduledActionStatus.PENDING,
                ScheduledAction.due_at < cutoff,
            ),
            and_(
                ScheduledAction.status == ScheduledActionStatus.RUNNING,
                ScheduledAction.lease_expires_at < cutoff,
            ),
        )
    )
    return bool(await connection.scalar(select(overdue)))


async def _redis_state(container: Container) -> Literal["ok", "unavailable", "not_configured"]:
    if container.redis is None:
        return "not_configured"
    try:
        async with asyncio.timeout(container.settings.redis_timeout_seconds):
            await container.redis.ping()
    except Exception as exc:
        if container.redis_circuit is not None:
            container.redis_circuit.record_failure()
        log.warning("ready.redis_unavailable", failure_category=type(exc).__name__)
        return "unavailable"
    # A successful probe closes the circuit, so recovery is noticed without waiting for traffic.
    if container.redis_circuit is not None:
        container.redis_circuit.record_success()
    return "ok"


@router.get("/ready", response_model=ReadinessStatus, responses={503: {"model": ReadinessStatus}})
async def ready(container: ContainerDep) -> Response:
    database: Literal["ok", "unavailable"] = "unavailable"
    migrations: SchemaState = "unknown"
    worker: Literal["ok", "lagging", "unknown"] = "unknown"
    try:
        async with asyncio.timeout(_DATABASE_TIMEOUT_SECONDS):
            async with container.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                database = "ok"
                migrations = classify_revision(await _schema_revision(connection))
                if is_safe(migrations):
                    worker = "lagging" if await _escalation_lagging(container, connection) else "ok"
    except Exception as exc:
        log.warning(
            "ready.database_check_failed",
            failure_category=type(exc).__name__,
            database=database,
        )

    redis = await _redis_state(container)
    realtime = getattr(container.realtime, "state", "local_only")
    settings = container.settings
    if settings.voice_provider == "twilio":
        telephony = "twilio_live" if settings.real_telephony_enabled else "twilio_simulated"
    else:
        telephony = settings.voice_provider
    is_ready = database == "ok" and is_safe(migrations)
    body = ReadinessStatus(
        status="ready" if is_ready else "not_ready",
        database=database,
        migrations=migrations,
        expected_schema_revision=EXPECTED_SCHEMA_REVISION,
        redis=redis,
        realtime=realtime,
        escalation_worker=worker,
        telephony=telephony,
        ai_voice=settings.ai_voice_provider,
    )
    return JSONResponse(body.model_dump(), status_code=200 if is_ready else 503)


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    if not request.app.state.container.settings.metrics_enabled:
        return Response(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
