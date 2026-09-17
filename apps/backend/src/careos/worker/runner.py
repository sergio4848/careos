"""Worker process: durable escalation timers and device health sweeps.

Stateless and horizontally scalable: every loop coordinates through PostgreSQL row
leases (``FOR UPDATE SKIP LOCKED``), so N workers never execute the same step twice
concurrently. A crashed worker's leases expire and are reclaimed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.bootstrap import Container, build_container
from careos.core.config import Settings
from careos.core.logging import configure_logging, get_logger
from careos.modules.devices.service import mark_stale_devices_offline
from careos.modules.escalation_engine.executor import EscalationExecutor

log = get_logger("careos.worker")

HEARTBEAT_FILE = Path(
    os.environ.get("CAREOS_WORKER_HEARTBEAT_FILE", "/tmp/careos-worker-heartbeat")  # noqa: S108
)


def _touch_heartbeat() -> None:
    with contextlib.suppress(OSError):
        HEARTBEAT_FILE.write_text(str(int(time.time())))


async def _escalation_loop(
    executor: EscalationExecutor, settings: Settings, stop: asyncio.Event
) -> None:
    backoff = settings.worker_poll_interval_seconds
    while not stop.is_set():
        try:
            processed = await executor.run_due()
            backoff = settings.worker_poll_interval_seconds
            _touch_heartbeat()
        except Exception:
            log.exception("worker.escalation_loop_error")
            processed = 0
            backoff = min(backoff * 2, 30.0)
        if processed == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)


async def _device_sweep_loop(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            async with session_factory() as session:
                count = await mark_stale_devices_offline(
                    session, settings.device_offline_after_seconds
                )
            if count:
                log.info("worker.devices_marked_offline", count=count)
        except Exception:
            log.exception("worker.device_sweep_error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.device_sweep_interval_seconds)


async def run_loops(container: Container, stop: asyncio.Event, *, embedded: bool = False) -> None:
    """Run the worker loops against an existing container until ``stop`` is set."""
    settings = container.settings
    worker_id = f"{socket.gethostname()}:{os.getpid()}{':embedded' if embedded else ''}"[:64]
    executor = EscalationExecutor(
        settings=settings,
        session_factory=container.session_factory,
        publisher=container.realtime,
        voice=container.providers.voice,
        notifications=container.providers.notifications,
        ai=container.providers.ai,
        worker_id=worker_id,
    )
    log.info(
        "worker.started",
        worker_id=worker_id,
        embedded=embedded,
        voice_provider=container.providers.voice.name,
        ai_provider=container.providers.ai.provider_name,
    )
    try:
        await asyncio.gather(
            _escalation_loop(executor, settings, stop),
            _device_sweep_loop(settings, container.session_factory, stop),
        )
    finally:
        log.info("worker.stopped", worker_id=worker_id)


async def run_worker(settings: Settings) -> None:
    configure_logging(settings.log_level, settings.log_json)
    container = build_container(settings, with_hub=False)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_loops(container, stop)
    finally:
        await container.aclose()
