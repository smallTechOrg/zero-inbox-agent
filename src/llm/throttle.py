"""Process-wide outbound LLM rate limiter (spec rule G).

Every outbound request passes through one **process-wide** token bucket before it is
issued — first attempts, retries, and every tier. Per-batch limiting cannot work: the
graph runs batches concurrently (``MAX_CONCURRENCY``), so N per-batch limiters allow
N x the intended rate.

The NVIDIA account ceiling is 490 req/min; the default target is 350 for headroom,
configurable via ``AGENT_LLM_MAX_RPM``. Refill is continuous (never a fixed window), so
there is no burst at a minute boundary. A caller that has to wait simply waits: waiting
is never a failure, never a retry, and a request is never dropped.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

__all__ = ["acquire", "acquire_sync", "snapshot", "reset", "max_rpm", "ACCOUNT_CEILING_RPM"]

ACCOUNT_CEILING_RPM = 490
DEFAULT_MAX_RPM = 350
_MIN_RPM = 1

_lock = threading.Lock()
_state: dict[str, float] = {}
_waiting = 0
_configured_rpm: int | None = None


def max_rpm() -> int:
    """The active ceiling: ``AGENT_LLM_MAX_RPM``, clamped to the account ceiling."""
    raw = os.getenv("AGENT_LLM_MAX_RPM", "")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RPM
    if value <= 0:
        value = DEFAULT_MAX_RPM
    return max(_MIN_RPM, min(value, ACCOUNT_CEILING_RPM))


# Bucket capacity is ONE token: strict pacing, no burst. A capacity of `rpm` would
# let `rpm` requests leave instantly and another `rpm` refill inside the same rolling
# 60 s window — i.e. up to 2x the ceiling — which is exactly what the account limit
# punishes. One token means the grant spacing is always >= 60/rpm seconds.
_CAPACITY = 1.0


def _refill_locked(now: float, rpm: int) -> float:
    capacity = _CAPACITY
    tokens = _state.get("tokens", capacity)
    last = _state.get("last", now)
    if _state.get("rpm") != rpm:
        # The ceiling changed (tests / a restart with a new env value): re-base.
        tokens = min(tokens, capacity)
        _state["rpm"] = rpm
    tokens = min(capacity, tokens + (now - last) * rpm / 60.0)
    _state["tokens"] = tokens
    _state["last"] = now
    return tokens


def _try_take() -> float:
    """Take a token, or return the seconds to wait before one is available."""
    rpm = max_rpm()
    with _lock:
        tokens = _refill_locked(time.monotonic(), rpm)
        if tokens >= 1.0:
            _state["tokens"] = tokens - 1.0
            return 0.0
        return max((1.0 - tokens) * 60.0 / rpm, 0.001)


async def acquire() -> None:
    """Block (async) until this request is allowed to go out. Never raises."""
    global _waiting
    waited = False
    while True:
        delay = _try_take()
        if delay == 0.0:
            break
        if not waited:
            with _lock:
                _waiting += 1
            waited = True
        # Bounded sleep: the wait is recomputed each pass against the continuous
        # refill, so this can never block indefinitely.
        await asyncio.sleep(min(delay, 1.0))
    if waited:
        with _lock:
            _waiting -= 1


def acquire_sync() -> None:
    """Blocking variant for synchronous call sites. Never raises."""
    global _waiting
    waited = False
    while True:
        delay = _try_take()
        if delay == 0.0:
            break
        if not waited:
            with _lock:
                _waiting += 1
            waited = True
        time.sleep(min(delay, 1.0))
    if waited:
        with _lock:
            _waiting -= 1


def snapshot() -> dict:
    rpm = max_rpm()
    with _lock:
        tokens = _refill_locked(time.monotonic(), rpm)
        return {"max_rpm": rpm, "available": round(tokens, 3), "waiting": _waiting}


def reset() -> None:
    """Test helper: refill the bucket and clear the waiter count."""
    global _waiting
    with _lock:
        _state.clear()
        _waiting = 0
