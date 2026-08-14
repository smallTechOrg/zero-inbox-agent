"""Unit tests for the no-silent-beat watchdog.

Threading discipline: every test stops the daemon thread and clears state, and
no test waits unboundedly — the sweeper is driven directly via ``sweep_once``
wherever real time is not the thing under test.
"""

from __future__ import annotations

import time

import pytest

from events import heartbeat



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
def captured(monkeypatch):
    """Record every event published to the bus, in order, with timestamps."""
    events: list[tuple[float, str, dict]] = []
    real_emit = _bus().emit

    def _spy(user_id, event):
        events.append((time.monotonic(), user_id, event))
        return real_emit(user_id, event)

    monkeypatch.setattr(_bus(), "emit", _spy)
    return events


def _heartbeats(events, user_id=None):
    return [
        e
        for _ts, uid, e in events
        if e.get("type") == "activity_heartbeat" and (user_id is None or uid == user_id)
    ]


def test_note_activity_arms_and_starts_one_thread():
    heartbeat.note_activity("u1", run_id="r1", phase="triage.tier_started", fields={"tier": 3})

    assert heartbeat.armed_runs() == [("u1", "r1")]
    assert heartbeat._thread is not None and heartbeat._thread.is_alive()

    # Idempotent: a second event does not spawn a second thread.
    first = heartbeat._thread
    heartbeat.note_activity("u1", run_id="r1", phase="triage.batch_dispatched", fields={"tier": 3})
    assert heartbeat._thread is first


def test_event_without_run_id_or_user_does_not_arm():
    heartbeat.note_activity("u1", run_id=None, phase="operation.start", fields={})
    heartbeat.note_activity("", run_id="r1", phase="operation.start", fields={})

    assert heartbeat.armed_runs() == []


@pytest.mark.parametrize(
    "event", ["run_completed", "triage.run_completed", "run_resumable", "run_failed", "error"]
)
def test_terminal_events_disarm(event):
    heartbeat.note_activity("u1", run_id="r1", phase="triage.tier_started", fields={"tier": 3})
    assert heartbeat.armed_runs() == [("u1", "r1")]

    heartbeat.note_activity("u1", run_id="r1", phase=event, fields={})
    assert heartbeat.armed_runs() == []


def test_no_heartbeat_when_publications_are_frequent(captured):
    heartbeat.note_activity("u1", run_id="r1", phase="triage.batch_dispatched", fields={"tier": 3})

    # Six publications inside two intervals — the graph is publishing faster
    # than the watchdog's bar, so the watchdog must stay silent.
    deadline = time.monotonic() + 2 * heartbeat.HEARTBEAT_INTERVAL_SECONDS
    while time.monotonic() < deadline:
        heartbeat.note_activity(
            "u1", run_id="r1", phase="triage.batch_returned", fields={"tier": 3}
        )
        assert heartbeat.sweep_once() == 0
        time.sleep(0.25)

    assert _heartbeats(captured) == []


def test_heartbeat_fires_after_the_interval_with_real_state(captured):
    heartbeat.note_activity(
        "u1",
        run_id="r1",
        phase="triage.batch_dispatched",
        fields={
            "tier": 3,
            "batch_n": 12,
            "batch_total": 75,
            "batch_size": 29,
            "model": "nvidia/nemotron-3-nano-30b-a3b",
        },
    )

    now = time.monotonic()
    assert heartbeat.sweep_once(now=now) == 0  # nothing silent yet
    assert heartbeat.sweep_once(now=now + heartbeat.HEARTBEAT_INTERVAL_SECONDS + 0.1) == 1

    beats = _heartbeats(captured)
    assert len(beats) == 1
    beat = beats[0]
    assert beat["type"] == "activity_heartbeat"
    assert beat["run_id"] == "r1"
    assert beat["phase"] == "tier3_classify"
    assert beat["detail"] == "batch 12/75 — 29 threads"
    assert beat["batch_n"] == 12
    assert beat["batch_total"] == 75
    assert beat["batch_size"] == 29
    assert beat["model"] == "nvidia/nemotron-3-nano-30b-a3b"
    assert beat["elapsed_s"] >= 3.0
    assert beat["silent_for_s"] >= 3.0
    # Rule I7: counts, ids, phases, models and elapsed times only.
    assert not {"subject", "from_email", "body", "snippet", "content"} & set(beat)


def test_heartbeat_repeats_once_per_interval_while_silent(captured):
    heartbeat.note_activity("u1", run_id="r1", phase="triage.batch_dispatched", fields={"tier": 3})
    base = time.monotonic()

    for i in range(1, 5):
        assert heartbeat.sweep_once(now=base + i * heartbeat.HEARTBEAT_INTERVAL_SECONDS) == 1

    beats = _heartbeats(captured)
    assert len(beats) == 4
    elapsed = [b["elapsed_s"] for b in beats]
    assert elapsed == sorted(elapsed) and elapsed[0] < elapsed[-1]


def test_runs_are_isolated_per_user_and_per_run(captured):
    heartbeat.note_activity("u1", run_id="r1", phase="triage.batch_dispatched", fields={"tier": 3})
    heartbeat.note_activity("u2", run_id="r2", phase="triage.page_fetched", fields={})
    heartbeat.note_activity("u1", run_id="r3", phase="triage.reviewer_started", fields={})

    assert heartbeat.armed_runs() == [("u1", "r1"), ("u1", "r3"), ("u2", "r2")]

    # Disarming one run leaves the others watched.
    heartbeat.note_activity("u1", run_id="r1", phase="run_completed", fields={})
    assert heartbeat.armed_runs() == [("u1", "r3"), ("u2", "r2")]

    assert heartbeat.sweep_once(now=time.monotonic() + 4.0) == 2
    beats_u1 = _heartbeats(captured, "u1")
    beats_u2 = _heartbeats(captured, "u2")
    assert [b["run_id"] for b in beats_u1] == ["r3"]
    assert [b["run_id"] for b in beats_u2] == ["r2"]
    assert beats_u1[0]["phase"] == "second_pass_review"
    assert beats_u2[0]["phase"] == "fetching_inbox"


def test_idle_ceiling_drops_an_abandoned_run(captured):
    heartbeat.note_activity("u1", run_id="r1", phase="triage.batch_dispatched", fields={"tier": 3})

    assert heartbeat.sweep_once(now=time.monotonic() + heartbeat.IDLE_CEILING_SECONDS + 1) == 0
    assert heartbeat.armed_runs() == []
    assert _heartbeats(captured) == []


def test_llm_chatter_does_not_overwrite_the_graph_phase(captured):
    heartbeat.note_activity(
        "u1",
        run_id="r1",
        phase="triage.batch_dispatched",
        fields={"tier": 3, "batch_n": 4, "batch_total": 9, "batch_size": 29},
    )
    heartbeat.note_activity(
        "u1", run_id="r1", phase="llm.call_started", fields={"model": "nemotron"}
    )

    heartbeat.sweep_once(now=time.monotonic() + 4.0)
    beat = _heartbeats(captured)[0]
    assert beat["phase"] == "tier3_classify"
    assert beat["batch_n"] == 4
    assert beat["model"] == "nemotron"


def test_stop_watchdog_is_safe_when_never_started():
    heartbeat.stop_watchdog()
    heartbeat.stop_watchdog()
    assert heartbeat.armed_runs() == []


def test_watchdog_thread_actually_emits_without_manual_sweeps(captured):
    """End-to-end through the real daemon thread — bounded wait, no busy loop."""
    heartbeat.note_activity("u1", run_id="r1", phase="triage.batch_dispatched", fields={"tier": 3})

    deadline = time.monotonic() + heartbeat.HEARTBEAT_INTERVAL_SECONDS + 2.0
    while time.monotonic() < deadline and not _heartbeats(captured):
        time.sleep(0.1)

    assert _heartbeats(captured), "the daemon thread published no heartbeat within 5s"
