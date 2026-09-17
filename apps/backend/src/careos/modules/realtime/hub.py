"""In-process registry of operator WebSocket connections, partitioned by organisation."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict

from starlette.websockets import WebSocket

from careos.contracts.realtime import RealtimeMessage
from careos.core.logging import get_logger
from careos.core.metrics import WEBSOCKET_CONNECTIONS

log = get_logger(__name__)

_SEND_TIMEOUT_SECONDS = 5.0


class ConnectionHub:
    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def register(self, organisation_id: uuid.UUID, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections[organisation_id].add(websocket)
        WEBSOCKET_CONNECTIONS.inc()

    async def unregister(self, organisation_id: uuid.UUID, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(organisation_id)
            if connections is None or websocket not in connections:
                return
            connections.discard(websocket)
            if not connections:
                self._connections.pop(organisation_id, None)
        WEBSOCKET_CONNECTIONS.dec()

    def connection_count(self, organisation_id: uuid.UUID | None = None) -> int:
        if organisation_id is not None:
            return len(self._connections.get(organisation_id, ()))
        return sum(len(c) for c in self._connections.values())

    async def deliver(self, message: RealtimeMessage) -> None:
        """Fan out to sockets of the message's organisation only (tenant isolation)."""
        targets = list(self._connections.get(message.organisation_id, ()))
        if not targets:
            return
        data = message.model_dump_json()
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(data), _SEND_TIMEOUT_SECONDS) for ws in targets),
            return_exceptions=True,
        )
        for websocket, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                log.info("realtime.drop_slow_or_closed_socket", error=type(result).__name__)
                await self.unregister(message.organisation_id, websocket)
