"""In-memory per-user SSE event bus.

Thread-safe ring buffer of the last 50 events per user.
Subscribers are asyncio.Queue instances registered per user.

``emit`` is safe to call from background threads (the triage graph runs in a
BackgroundTask). It captures the running event loop at subscribe time and uses
``loop.call_soon_threadsafe`` to deliver events to async queues.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any

_RING_SIZE = 50
_HEARTBEAT_EVENT = {"type": "heartbeat"}

# ── module-level singleton state ──────────────────────────────────────────────

_lock = threading.Lock()

# user_id -> deque of dicts (ring buffer)
_rings: dict[str, deque] = {}

# user_id -> list of (loop, queue) pairs
_subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}


# ── public API ────────────────────────────────────────────────────────────────


def emit(user_id: str, event: dict[str, Any]) -> None:
    """Push *event* to the user's ring buffer and all active SSE queues.

    Safe to call from any thread, including non-async background threads.
    """
    with _lock:
        # ring buffer
        ring = _rings.setdefault(user_id, deque(maxlen=_RING_SIZE))
        ring.append(event)

        # deliver to active subscribers
        for loop, queue in list(_subscribers.get(user_id, [])):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception:  # pragma: no cover — dead loop / closed queue
                pass


def subscribe(user_id: str) -> asyncio.Queue:
    """Register a new SSE subscriber and return its queue.

    Must be called from inside a running asyncio event loop.
    The current running loop is captured here so ``emit`` can use
    ``call_soon_threadsafe`` from non-async threads.
    """
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subscribers.setdefault(user_id, []).append((loop, queue))
    return queue


def unsubscribe(user_id: str, queue: asyncio.Queue) -> None:
    """Remove *queue* from the subscriber list for *user_id*."""
    with _lock:
        subs = _subscribers.get(user_id, [])
        _subscribers[user_id] = [(lp, q) for lp, q in subs if q is not queue]


def replay_buffer(user_id: str) -> list[dict]:
    """Return a copy of the current ring buffer for *user_id* (oldest first)."""
    with _lock:
        ring = _rings.get(user_id)
        return list(ring) if ring else []
