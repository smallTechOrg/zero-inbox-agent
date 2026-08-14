"""The structlog -> SSE bus bridge, and the watchdog arming that rides on it."""

from __future__ import annotations

import pytest

from events import heartbeat
from observability.logging import activity_bus_processor



def _bus():
    """Resolve ``events.bus`` at call time.

    ``tests/unit/events/test_bus.py`` deliberately deletes the module from
    ``sys.modules`` to test a fresh ring, so a module object captured at import
    time can be stale by the time these tests run — patching it would then patch
    a module nothing uses. Always look it up live.
    """
    import importlib

    return importlib.import_module("events.bus")


@pytest.fixture(autouse=True)
def _clean_watchdog():
    heartbeat.stop_watchdog()
    yield
    heartbeat.stop_watchdog()


@pytest.fixture
def emitted(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(_bus(), "emit", lambda user_id, event: events.append((user_id, event)))
    return events


def test_bridge_forwards_a_log_line_for_a_bound_user(emitted):
    event_dict = {
        "event": "triage.batch_dispatched",
        "level": "info",
        "logger": "zero_inbox.graph",
        "timestamp": "2026-08-15T00:00:00Z",
        "user_id": "u1",
        "run_id": "r1",
        "tier": 3,
        "batch_n": 12,
        "batch_total": 75,
    }

    returned = activity_bus_processor(None, "info", dict(event_dict))

    assert returned == event_dict  # the processor never mutates the log line
    assert len(emitted) == 1
    user_id, event = emitted[0]
    assert user_id == "u1"
    assert event["type"] == "log"
    assert event["event"] == "triage.batch_dispatched"
    assert event["run_id"] == "r1"
    assert event["fields"] == {"tier": 3, "batch_n": 12, "batch_total": 75}
    # Envelope keys are not duplicated into fields.
    assert not {"event", "level", "logger", "timestamp", "run_id", "user_id"} & set(event["fields"])


def test_bridge_drops_events_without_a_bound_user(emitted):
    activity_bus_processor(None, "info", {"event": "triage.batch_dispatched", "run_id": "r1"})
    activity_bus_processor(None, "info", {"event": "boot", "user_id": ""})
    activity_bus_processor(None, "info", {"event": "boot", "user_id": None})

    assert emitted == []
    assert heartbeat.armed_runs() == []


def test_bridge_arms_the_watchdog_from_the_log_stream(emitted):
    activity_bus_processor(
        None,
        "info",
        {
            "event": "triage.batch_dispatched",
            "user_id": "u1",
            "run_id": "r1",
            "tier": 3,
            "batch_n": 12,
            "batch_total": 75,
            "batch_size": 29,
            "model": "nvidia/nemotron-3-nano-30b-a3b",
        },
    )

    assert heartbeat.armed_runs() == [("u1", "r1")]

    emitted.clear()
    assert heartbeat.sweep_once(now=_future()) == 1
    beat = emitted[0][1]
    assert beat["type"] == "activity_heartbeat"
    assert beat["phase"] == "tier3_classify"
    assert beat["batch_size"] == 29
    assert beat["model"] == "nvidia/nemotron-3-nano-30b-a3b"


def test_bridge_disarms_the_watchdog_on_a_terminal_event(emitted):
    activity_bus_processor(
        None, "info", {"event": "triage.tier_started", "user_id": "u1", "run_id": "r1", "tier": 3}
    )
    assert heartbeat.armed_runs() == [("u1", "r1")]

    activity_bus_processor(None, "info", {"event": "run_completed", "user_id": "u1", "run_id": "r1"})

    assert heartbeat.armed_runs() == []
    emitted.clear()
    assert heartbeat.sweep_once(now=_future()) == 0
    assert emitted == []


def test_a_log_line_with_a_user_but_no_run_id_is_forwarded_but_does_not_arm(emitted):
    activity_bus_processor(None, "info", {"event": "http.request", "user_id": "u1"})

    assert len(emitted) == 1
    assert heartbeat.armed_runs() == []


def test_bus_failure_never_breaks_logging(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(_bus(), "emit", _boom)

    event_dict = {"event": "triage.checkpoint", "user_id": "u1", "run_id": "r1"}
    assert activity_bus_processor(None, "info", dict(event_dict)) == event_dict
    # The watchdog still armed even though the bus emit failed.
    assert heartbeat.armed_runs() == [("u1", "r1")]


def _future() -> float:
    import time

    return time.monotonic() + heartbeat.HEARTBEAT_INTERVAL_SECONDS + 0.5
