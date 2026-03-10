"""
Result Manager
==============
Thread-safe store for the latest VLM inference result.
Broadcasts updates to all subscribed WebSocket clients via asyncio.Queue.
"""

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ResultManager:
    """Hold the latest result and fan-out updates to WebSocket queues."""

    def __init__(self) -> None:
        self._latest: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._queues: List[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Called from the async (FastAPI) side ──────────────────────────────────

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register the running asyncio event loop for thread-safe dispatch."""
        self._loop = loop

    def subscribe(self) -> "asyncio.Queue[str]":
        """Create and register a queue for a new WebSocket client."""
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        logger.debug("WS client subscribed (%d total)", len(self._queues))
        return q

    def unsubscribe(self, q: "asyncio.Queue[str]") -> None:
        """Remove a client queue when the WebSocket closes."""
        try:
            self._queues.remove(q)
        except ValueError:
            pass
        logger.debug("WS client unsubscribed (%d remaining)", len(self._queues))

    # ── Called from inference thread ──────────────────────────────────────────

    def update_result(self, result: Dict[str, Any]) -> None:
        """Store result and push it to every subscribed WebSocket queue."""
        with self._lock:
            self._latest = result

        if not self._queues or not self._loop or self._loop.is_closed():
            return

        msg = json.dumps(result)
        for q in list(self._queues):
            asyncio.run_coroutine_threadsafe(q.put(msg), self._loop)

    # ── General accessors ─────────────────────────────────────────────────────

    def get_latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._latest
