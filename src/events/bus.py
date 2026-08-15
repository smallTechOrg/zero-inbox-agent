"""In-memory per-user SSE event bus (spec/architecture.md `src/events/`).

A thread-safe ring buffer of recent events per user plus live delivery to
asyncio subscriber queues. ``emit`` is safe to call from background threads
(the triage runner executes as a FastAPI background task): the subscriber's
event loop is captured at subscribe time and delivery goes through
``loop.call_soon_threadsafe``.

Durability is NOT this module's job — feed events are persisted as
``run_events`` rows by :mod:`events.store` *before* they are emitted here, so a
reconnecting browser replays from the database (``?after_seq``) and the bus
only has to cover the live tail.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Any

_log = logging.getLogger("zero_inbox.events.bus")

#: Live-tail safety net only; replay-on-reconnect is served from run_events.
_RING_SIZE = 1000

_lock = threading.Lock()

# user_id -> deque of event dicts (ring buffer)
_rings: dict[str, deque] = {}

# user_id -> list of (loop, queue) pairs
_subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}


def emit(user_id: str, event: dict[str, Any]) -> None:
    """Push *event* to the user's ring buffer and every active SSE queue.

    Safe from any thread. Never raises — an event-bus problem must never kill
    a triage run or an undo.
    """
    try:
        with _lock:
            ring = _rings.setdefault(user_id, deque(maxlen=_RING_SIZE))
            ring.append(event)
            for loop, queue in list(_subscribers.get(user_id, [])):
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except Exception:  # pragma: no cover — dead loop / closed queue
                    pass
    except Exception:  # pragma: no cover — defensive
        _log.warning("bus.emit_failed user_id=%s", user_id, exc_info=True)


def subscribe(user_id: str) -> asyncio.Queue:
    """Register a new SSE subscriber and return its queue.

    Must be called from inside a running asyncio event loop (the SSE route).
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
    """A copy of the user's current ring buffer, oldest first."""
    with _lock:
        ring = _rings.get(user_id)
        return list(ring) if ring else []
