"""In-memory per-user SSE event bus.

Thread-safe ring buffer of the last 50 events per user.
Subscribers are asyncio.Queue instances registered per user.

``emit`` is safe to call from background threads (the triage graph runs in a
BackgroundTask). It captures the running event loop at subscribe time and uses
``loop.call_soon_threadsafe`` to deliver events to async queues.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any

_log = logging.getLogger("zero_inbox.events")

# Privacy caps applied INSIDE the typed helpers so no call site can widen them.
_SUBJECT_MAX = 60
_REASONING_MAX = 140

# Sized for "show me every micro action": a large triage run emits hundreds of
# log lines (per-batch tier decisions, per-page fetches, retries), and the ring
# is what a browser replays on connect/reconnect. At 50 a user reconnecting
# mid-run saw only the last few seconds of a multi-minute run.
_RING_SIZE = 1000
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


# ── typed emit helpers (Phase 6) ──────────────────────────────────────────────
#
# Every helper is wrapped in try/except Exception: the triage graph calls these
# from inside its hot path, and an event-bus problem must never kill a run.
# Truncation happens HERE, not at the call site, so no caller can leak more than
# the documented cap. Only headers/subject/redacted metadata ever travel on the
# bus — never message body text.


def _truncate(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def emit_thread_classified(
    user_id: str,
    *,
    run_id: str,
    item_id: str,
    subject: str,
    from_email: str,
    category: str,
    action: str,
    decided_by: str,
    confidence: float,
    reasoning: str,
    review_state: str,
) -> None:
    """One event per decided thread. Never raises.

    Shape: spec/capabilities/triage-transparency.md `thread_classified`.
    """
    try:
        emit(
            user_id,
            {
                "type": "thread_classified",
                "run_id": str(run_id),
                "item_id": str(item_id),
                "subject": _truncate(subject, _SUBJECT_MAX),
                "from_email": str(from_email or ""),
                "category": str(category or ""),
                "action": str(action or ""),
                "decided_by": str(decided_by or ""),
                "confidence": float(confidence or 0.0),
                "reasoning": _truncate(reasoning, _REASONING_MAX),
                "review_state": str(review_state or ""),
            },
        )
    except Exception:  # pragma: no cover — defensive, asserted in unit tests
        _log.warning("events.emit_failed type=thread_classified run_id=%s", run_id, exc_info=True)


def emit_provider_degraded(
    user_id: str,
    *,
    run_id: str,
    provider: str,
    model: str,
    calls: int,
    retries: int,
    consecutive_failures: int,
) -> None:
    """The LLM provider is failing enough to stretch the run. Never raises."""
    try:
        emit(
            user_id,
            {
                "type": "provider_degraded",
                "run_id": str(run_id),
                "provider": str(provider or ""),
                "model": str(model or ""),
                "calls": int(calls),
                "retries": int(retries),
                "consecutive_failures": int(consecutive_failures),
            },
        )
    except Exception:
        _log.warning("events.emit_failed type=provider_degraded run_id=%s", run_id, exc_info=True)


def emit_run_resumable(
    user_id: str,
    *,
    run_id: str,
    items_total: int,
    items_decided: int,
    reason: str,
) -> None:
    """A run stopped with work already durably persisted. Never raises."""
    try:
        emit(
            user_id,
            {
                "type": "run_resumable",
                "run_id": str(run_id),
                "items_total": int(items_total),
                "items_decided": int(items_decided),
                "reason": _truncate(reason, _REASONING_MAX),
            },
        )
    except Exception:
        _log.warning("events.emit_failed type=run_resumable run_id=%s", run_id, exc_info=True)


def emit_model_fallback(
    user_id: str,
    *,
    run_id: str,
    from_model: str,
    to_model: str,
    reason: str,
) -> None:
    """A mid-run model switch — surfaced so it is never an invisible backend
    action. Never raises."""
    try:
        emit(
            user_id,
            {
                "type": "model_fallback",
                "run_id": str(run_id),
                "from_model": str(from_model or ""),
                "to_model": str(to_model or ""),
                "reason": _truncate(reason, _REASONING_MAX),
            },
        )
    except Exception:
        _log.warning("events.emit_failed type=model_fallback run_id=%s", run_id, exc_info=True)
