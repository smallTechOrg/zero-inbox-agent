"""*There should not be one beat without a log being published for the user.*

A tier-3 batch is ONE LLM call covering ~29 threads: between dispatch and return
the graph publishes nothing. On a degraded provider that was minutes of dead air
— which is exactly how a working run became indistinguishable from a hung one.

This test reproduces that exact dead-air window (a simulated 60 s batch) and
asserts the user's feed never goes quiet: max inter-event gap < 5.0 s (the 3.0 s
heartbeat interval plus 2.0 s of thread-scheduling tolerance), and at least 15
heartbeats, each carrying REAL observed state. A spinner is not a heartbeat.
"""

from __future__ import annotations

import time

import pytest
import structlog

from events import heartbeat
from observability.logging import configure_logging, get_logger

USER_ID = "silent-beat-user"
RUN_ID = "silent-beat-run"
MODEL = "nvidia/nemotron-3-nano-30b-a3b"

#: The dead-air window being reproduced, in seconds.
SIMULATED_BATCH_SECONDS = 60.0

MAX_TOLERATED_GAP_SECONDS = 5.0
MIN_EXPECTED_HEARTBEATS = 15



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
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    heartbeat.stop_watchdog()


@pytest.fixture
def published(monkeypatch):
    """Timestamped record of everything published to this user's feed."""
    records: list[tuple[float, dict]] = []
    real_emit = _bus().emit

    def _spy(user_id, event):
        if user_id == USER_ID:
            records.append((time.monotonic(), event))
        return real_emit(user_id, event)

    monkeypatch.setattr(_bus(), "emit", _spy)
    return records


def test_a_sixty_second_batch_never_goes_quiet(published):
    configure_logging(force=True)
    log = get_logger("zero_inbox.graph")

    structlog.contextvars.bind_contextvars(user_id=USER_ID, run_id=RUN_ID)

    # The graph dispatches one batch covering 29 threads...
    log.info(
        "triage.batch_dispatched",
        tier=3,
        batch_n=12,
        batch_total=75,
        batch_size=29,
        model=MODEL,
    )
    started = time.monotonic()

    # ...and then the single LLM call takes a full minute. No graph code runs,
    # nothing is logged, nothing is emitted. This is the real dead-air window.
    time.sleep(SIMULATED_BATCH_SECONDS)

    log.info("triage.batch_returned", tier=3, batch_n=12, decided=29, model=MODEL)
    finished = time.monotonic()

    assert published, "nothing at all was published for the user"

    # 1. No gap longer than the guarantee, anywhere in the window.
    stamps = [ts for ts, _ in published if started <= ts <= finished]
    boundaries = [started, *stamps, finished]
    gaps = [b - a for a, b in zip(boundaries, boundaries[1:])]
    max_gap = max(gaps)
    print(f"\nmax inter-event gap: {max_gap:.2f}s over {finished - started:.1f}s")
    assert max_gap < MAX_TOLERATED_GAP_SECONDS, (
        f"the feed went silent for {max_gap:.2f}s during an active run "
        f"(gaps: {[round(g, 2) for g in gaps]})"
    )

    # 2. Enough heartbeats to be a continuous feed, not an occasional twitch.
    beats = [e for ts, e in published if e.get("type") == "activity_heartbeat"]
    assert len(beats) >= MIN_EXPECTED_HEARTBEATS, (
        f"only {len(beats)} heartbeats in {SIMULATED_BATCH_SECONDS:.0f}s"
    )

    # 3. Each heartbeat carries REAL observed state, not a content-free tick.
    for beat in beats:
        assert beat["run_id"] == RUN_ID
        assert beat["phase"] == "tier3_classify"
        assert beat["batch_n"] == 12
        assert beat["batch_total"] == 75
        assert beat["batch_size"] == 29
        assert beat["model"] == MODEL
        assert beat["detail"] == "batch 12/75 — 29 threads"
        assert isinstance(beat["elapsed_s"], float)
        assert beat["silent_for_s"] >= heartbeat.HEARTBEAT_INTERVAL_SECONDS - 0.5
        # Rule I7 — counts, ids, phases, models, elapsed times ONLY.
        assert not {"subject", "from_email", "body", "snippet", "content"} & set(beat)

    # 4. elapsed_s is a real, monotonically increasing clock on the phase.
    elapsed = [b["elapsed_s"] for b in beats]
    assert elapsed == sorted(elapsed), f"elapsed_s not monotonic: {elapsed}"
    assert elapsed[0] < elapsed[-1]
    assert elapsed[-1] >= SIMULATED_BATCH_SECONDS - 5.0

    # 5. The watchdog disarms when the run ends, and the stream stops.
    log.info("run_completed", items_decided=29)
    assert heartbeat.armed_runs() == []
    before = len(published)
    time.sleep(heartbeat.HEARTBEAT_INTERVAL_SECONDS + 1.0)
    after_beats = [
        e for _ts, e in published[before:] if e.get("type") == "activity_heartbeat"
    ]
    assert after_beats == [], "the watchdog kept beating after the run completed"


def test_no_heartbeats_when_no_run_is_active(published):
    """An idle process publishes zero heartbeats — the watchdog self-arms only."""
    configure_logging(force=True)
    log = get_logger("zero_inbox.api")

    structlog.contextvars.bind_contextvars(user_id=USER_ID)  # no run_id
    log.info("http.request", route="/api/me")

    time.sleep(heartbeat.HEARTBEAT_INTERVAL_SECONDS + 1.0)

    assert [e for _ts, e in published if e.get("type") == "activity_heartbeat"] == []
    assert heartbeat.armed_runs() == []
