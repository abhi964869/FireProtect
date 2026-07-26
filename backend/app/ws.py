"""WebSocket connection hub.

The MQTT subscriber runs on a paho thread with no event loop, while the
WebSocket sends must happen on the FastAPI event loop. The hub bridges the two:
``broadcast_threadsafe`` can be called from any thread and schedules the send on
the captured loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionHub:
    """Tracks live WebSocket clients and fans messages out to them."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the event loop so background threads can schedule sends."""
        self._loop = loop

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        logger.info("websocket connected (%d total)", len(self._clients))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        logger.info("websocket disconnected (%d remain)", len(self._clients))

    async def broadcast(self, message_type: str, payload: dict[str, Any]) -> None:
        """Send to every client, dropping any that fail."""
        if not self._clients:
            return
        message = json.dumps({"type": message_type, "payload": payload})
        async with self._lock:
            targets = list(self._clients)

        dead: list[WebSocket] = []
        for client in targets:
            try:
                await client.send_text(message)
            except Exception:
                # A client that vanished mid-send is normal (tab closed);
                # collect and prune rather than letting one failure stop the fan-out.
                dead.append(client)

        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)
            logger.info("pruned %d dead websocket client(s)", len(dead))

    def broadcast_threadsafe(self, message_type: str, payload: dict[str, Any]) -> None:
        """Schedule a broadcast from a non-async thread (the MQTT callback)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast(message_type, payload), loop
            )
        except RuntimeError:
            # Loop shutting down; dropping a broadcast during teardown is fine.
            logger.debug("event loop unavailable; dropped %s broadcast", message_type)


hub = ConnectionHub()
