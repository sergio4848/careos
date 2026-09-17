"""Liveness, readiness and metrics endpoints."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import text

from careos.api.deps import ContainerDep

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: Literal["ok"]


class ReadinessStatus(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unavailable"]
    redis: Literal["ok", "degraded", "not_configured"]


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Liveness: the process is running. Does not touch dependencies."""
    return HealthStatus(status="ok")


@router.get("/ready", response_model=ReadinessStatus, responses={503: {"model": ReadinessStatus}})
async def ready(container: ContainerDep) -> Response:
    """Readiness: PostgreSQL is required. Redis only degrades realtime fan-out, so it is reported
    but does not fail readiness (incident handling works without it)."""
    database: Literal["ok", "unavailable"] = "ok"
    try:
        async with asyncio.timeout(3):
            async with container.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception:
        database = "unavailable"

    redis_state: Literal["ok", "degraded", "not_configured"] = "not_configured"
    if container.redis is not None:
        try:
            async with asyncio.timeout(2):
                await container.redis.ping()
            redis_state = "ok"
        except Exception:
            redis_state = "degraded"

    body = ReadinessStatus(
        status="ready" if database == "ok" else "not_ready", database=database, redis=redis_state
    )
    return JSONResponse(body.model_dump(), status_code=200 if database == "ok" else 503)


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    if not request.app.state.container.settings.metrics_enabled:
        return Response(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
