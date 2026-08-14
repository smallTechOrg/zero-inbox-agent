"""The three Phase 7 apply events: exact payloads, no content, never fatal.

The privacy invariant is structural, not incidental: these events describe an
apply pass, which never needs a subject or a body to be legible. The payload key
sets below are asserted EXACTLY so a future field cannot be added without a test
failing first.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from events import bus


@contextmanager
def capture_warnings(logger_name: str):
    """Collect WARNING records from *logger_name* directly off its logger.

    ``caplog`` is not used here: the app installs its own structlog/logging
    configuration at import time, and whether the root handler sees these records
    depends on which other test configured logging first. Attaching to the logger
    itself makes the assertion independent of test ordering.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collector(level=logging.WARNING)
    previous_level, previous_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    # The migration tests call alembic's `fileConfig`, which defaults to
    # disable_existing_loggers=True and leaves every already-created application
    # logger disabled for the rest of the process. Undo that here so this
    # assertion does not depend on which tests ran before it.
    logger.disabled = False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled


@pytest.fixture(autouse=True)
def _clean_bus():
    with bus._lock:
        bus._rings.clear()
        bus._subscribers.clear()
    yield
    with bus._lock:
        bus._rings.clear()
        bus._subscribers.clear()


#: Nothing on the bus may carry message content. Checked against every payload.
FORBIDDEN_KEYS = {"body", "content", "text", "snippet", "html", "subject", "from_email", "message"}


class TestApplyProgress:
    def test_the_payload_shape_is_exact(self):
        bus.emit_apply_progress("u1", run_id="r1", applied=25, total_to_apply=544, failed=2)

        event = bus.replay_buffer("u1")[-1]

        assert event == {
            "type": "apply_progress",
            "run_id": "r1",
            "applied": 25,
            "total_to_apply": 544,
            "failed": 2,
        }

    def test_it_carries_no_content_field(self):
        bus.emit_apply_progress("u1", run_id="r1", applied=0, total_to_apply=0, failed=0)

        assert not (set(bus.replay_buffer("u1")[-1]) & FORBIDDEN_KEYS)

    def test_counts_are_coerced_to_ints(self):
        bus.emit_apply_progress("u1", run_id="r1", applied="7", total_to_apply=10.0, failed=0)

        event = bus.replay_buffer("u1")[-1]
        assert event["applied"] == 7 and isinstance(event["applied"], int)
        assert event["total_to_apply"] == 10 and isinstance(event["total_to_apply"], int)


class TestRunApplyFailed:
    def test_the_payload_shape_is_exact(self):
        bus.emit_run_apply_failed("u1", run_id="r1", reason="RefreshError: invalid_grant", distance_to_zero=615)

        assert bus.replay_buffer("u1")[-1] == {
            "type": "run_apply_failed",
            "run_id": "r1",
            "reason": "RefreshError: invalid_grant",
            "distance_to_zero": 615,
        }

    def test_a_long_reason_is_truncated_at_the_bus_not_the_call_site(self):
        bus.emit_run_apply_failed("u1", run_id="r1", reason="x" * 500, distance_to_zero=1)

        assert len(bus.replay_buffer("u1")[-1]["reason"]) == bus._REASONING_MAX

    def test_it_carries_no_content_field(self):
        bus.emit_run_apply_failed("u1", run_id="r1", reason="boom", distance_to_zero=1)

        assert not (set(bus.replay_buffer("u1")[-1]) & FORBIDDEN_KEYS)


class TestInboxZeroReport:
    REMAINDER = {
        "needs_your_call": 213,
        "category_keep": 1314,
        "held_by_never_miss": 34,
        "below_threshold": 71,
        "unclassified": 0,
    }

    def test_the_payload_shape_is_exact(self):
        bus.emit_inbox_zero_report("u1", run_id="r1", applied=544, distance_to_zero=0, remainder=self.REMAINDER)

        assert bus.replay_buffer("u1")[-1] == {
            "type": "inbox_zero_report",
            "run_id": "r1",
            "applied": 544,
            "distance_to_zero": 0,
            "remainder": self.REMAINDER,
        }

    def test_the_remainder_is_coerced_to_counts_so_no_content_can_ride_along(self):
        bus.emit_inbox_zero_report(
            "u1",
            run_id="r1",
            applied=1,
            distance_to_zero=0,
            remainder={"category_keep": "3", "unclassified": None},
        )

        assert bus.replay_buffer("u1")[-1]["remainder"] == {"category_keep": 3, "unclassified": 0}

    def test_an_empty_remainder_is_accepted(self):
        bus.emit_inbox_zero_report("u1", run_id="r1", applied=0, distance_to_zero=0, remainder={})

        assert bus.replay_buffer("u1")[-1]["remainder"] == {}

    def test_it_carries_no_content_field(self):
        bus.emit_inbox_zero_report("u1", run_id="r1", applied=0, distance_to_zero=0, remainder=self.REMAINDER)

        assert not (set(bus.replay_buffer("u1")[-1]) & FORBIDDEN_KEYS)


class TestTheBusNeverFailsARun:
    """A broken subscriber, or a malformed payload, must never kill the apply pass."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda: bus.emit_apply_progress("u1", run_id="r1", applied=1, total_to_apply=1, failed=0),
            lambda: bus.emit_run_apply_failed("u1", run_id="r1", reason="boom", distance_to_zero=1),
            lambda: bus.emit_inbox_zero_report("u1", run_id="r1", applied=1, distance_to_zero=0, remainder={}),
        ],
    )
    def test_a_raising_emit_is_swallowed_and_logged(self, call, monkeypatch):
        def explode(*_args, **_kwargs):
            raise RuntimeError("ring buffer is on fire")

        monkeypatch.setattr(bus, "emit", explode)

        with capture_warnings("zero_inbox.events") as records:
            call()  # must not raise

        assert any("events.emit_failed" in r.getMessage() for r in records)

    def test_a_raising_subscriber_does_not_propagate(self, monkeypatch):
        class ExplodingQueue:
            def put_nowait(self, _event):
                raise RuntimeError("subscriber is broken")

        class ExplodingLoop:
            def call_soon_threadsafe(self, callback, *args):
                callback(*args)

        with bus._lock:
            bus._subscribers["u1"] = [(ExplodingLoop(), ExplodingQueue())]

        bus.emit_apply_progress("u1", run_id="r1", applied=1, total_to_apply=1, failed=0)
        bus.emit_run_apply_failed("u1", run_id="r1", reason="boom", distance_to_zero=1)
        bus.emit_inbox_zero_report("u1", run_id="r1", applied=1, distance_to_zero=0, remainder={})

        # The ring buffer still recorded all three: one bad subscriber loses nothing.
        assert [e["type"] for e in bus.replay_buffer("u1")] == [
            "apply_progress",
            "run_apply_failed",
            "inbox_zero_report",
        ]

    def test_a_non_numeric_count_is_reported_not_raised(self):
        with capture_warnings("zero_inbox.events") as records:
            bus.emit_apply_progress("u1", run_id="r1", applied="not-a-number", total_to_apply=1, failed=0)

        assert bus.replay_buffer("u1") == []
        assert any("events.emit_failed type=apply_progress" in r.getMessage() for r in records)
