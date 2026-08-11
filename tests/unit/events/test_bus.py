"""Unit tests for the in-memory SSE event bus (src/events/bus.py).

Tests run against the module-level singleton; each test clears state via the
internal _rings / _subscribers dicts after use so tests don't bleed state.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────


def _fresh_bus():
    """Return a freshly-reset bus module (re-imports to clear module-level state)."""
    if "events.bus" in sys.modules:
        del sys.modules["events.bus"]
    if "events" in sys.modules:
        del sys.modules["events"]
    import events.bus  # noqa: F401
    return sys.modules["events.bus"]


# ── tests ─────────────────────────────────────────────────────────────────────


def test_emit_adds_to_ring_buffer():
    bus = _fresh_bus()
    event = {"type": "run_started", "ts": 1.0}
    bus.emit("user-1", event)
    buf = bus.replay_buffer("user-1")
    assert len(buf) == 1
    assert buf[0] == event


def test_ring_buffer_caps_at_50_drops_oldest():
    bus = _fresh_bus()
    for i in range(55):
        bus.emit("user-cap", {"type": "e", "i": i})
    buf = bus.replay_buffer("user-cap")
    assert len(buf) == 50
    # The first 5 events (i=0..4) should have been dropped
    assert buf[0]["i"] == 5
    assert buf[-1]["i"] == 54


def test_subscriber_receives_emitted_event():
    bus = _fresh_bus()

    async def _run():
        queue = bus.subscribe("user-sub")
        bus.emit("user-sub", {"type": "ping"})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        bus.unsubscribe("user-sub", queue)
        return event

    result = asyncio.run(_run())
    assert result == {"type": "ping"}


def test_unsubscribe_stops_delivery():
    bus = _fresh_bus()

    async def _run():
        queue = bus.subscribe("user-unsub")
        bus.unsubscribe("user-unsub", queue)
        # Emit after unsubscribe — queue should stay empty
        bus.emit("user-unsub", {"type": "should-not-arrive"})
        try:
            await asyncio.wait_for(queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return True  # expected: nothing arrived
        return False  # got something — wrong

    got_nothing = asyncio.run(_run())
    assert got_nothing, "Queue received an event after unsubscribe"


def test_multiple_users_isolated():
    bus = _fresh_bus()

    bus.emit("user-a", {"type": "a-event"})
    bus.emit("user-b", {"type": "b-event"})

    buf_a = bus.replay_buffer("user-a")
    buf_b = bus.replay_buffer("user-b")

    assert len(buf_a) == 1 and buf_a[0]["type"] == "a-event"
    assert len(buf_b) == 1 and buf_b[0]["type"] == "b-event"


def test_emit_from_background_thread():
    """emit() must be safe to call from a non-async thread (BackgroundTask context)."""
    bus = _fresh_bus()

    received = []

    async def _run():
        queue = bus.subscribe("user-thread")
        # emit from a background thread
        t = threading.Thread(target=bus.emit, args=("user-thread", {"type": "from-thread"}))
        t.start()
        t.join()
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        bus.unsubscribe("user-thread", queue)
        received.append(event)

    asyncio.run(_run())
    assert received == [{"type": "from-thread"}]
