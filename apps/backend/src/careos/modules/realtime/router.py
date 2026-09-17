"""Operator WebSocket.

Security:
* authenticated with the same HttpOnly session cookie as the REST API;
* ``Origin`` must be an allowed console origin (prevents cross-site WebSocket hijacking);
* the connection is bound to the principal's organisation; the hub only delivers that
  organisation's messages;
* the session is re-validated periodically so revoked sessions are disconnected.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from careos.api.deps import SESSION_COOKIE
from careos.bootstrap import Container
from careos.core.errors import AuthenticationRequiredError
from careos.core.logging import get_logger
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission

log = get_logger(__name__)
router = APIRouter()

_REVALIDATE_SECONDS = 60
_HEARTBEAT_SECONDS = 25
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403


async def _authenticate(container: Container, websocket: WebSocket) -> Principal | None:
    token = websocket.cookies.get(SESSION_COOKIE)
    try:
        async with container.session_factory() as session:
            return await container.auth.authenticate(session, token)
    except AuthenticationRequiredError:
        return None


@router.websocket("/v1/ws")
async def operator_socket(websocket: WebSocket) -> None:
    container: Container = websocket.app.state.container
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in container.settings.cors_allowed_origins:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    principal = await _authenticate(container, websocket)
    if principal is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return
    if principal.organisation_id is None or not principal.has(Permission.INCIDENTS_READ):
        await websocket.close(code=CLOSE_FORBIDDEN)
        return
    hub = container.hub
    if hub is None:  # pragma: no cover - API containers always have a hub
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await websocket.accept()
    organisation_id = principal.organisation_id
    await hub.register(organisation_id, websocket)
    await websocket.send_json({"type": "connection.ready"})

    async def receive_loop() -> None:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")

    async def heartbeat_and_revalidate() -> None:
        elapsed = 0
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            elapsed += _HEARTBEAT_SECONDS
            await websocket.send_json({"type": "heartbeat"})
            if elapsed >= _REVALIDATE_SECONDS:
                elapsed = 0
                if await _authenticate(container, websocket) is None:
                    await websocket.close(code=CLOSE_UNAUTHENTICATED)
                    return

    tasks = [asyncio.create_task(receive_loop()), asyncio.create_task(heartbeat_and_revalidate())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
        await hub.unregister(organisation_id, websocket)
