"""triage-graph slice — routing predicates and the structured error path."""

from __future__ import annotations

import pytest

from graph.edges import (
    has_threads,
    more_batches,
    route_after_apply,
    route_after_load,
)
from graph.nodes import humanize_error
from graph.state import ClassifierView, Decision

from tests.unit.test_graph_nodes import make_fake_deps, outcome_for, run, view


class TestPredicates:
    def test_has_threads_true_on_threads_or_pending_decisions(self):
        v = ClassifierView(thread_id="t1", sender_address="a@b.com")
        d = Decision(thread_id="t1", category_id="c", category_name="Finance",
                     confidence=0.9, reason="r", needs_review=False)
        assert has_threads({"threads": [v]}) is True
        assert has_threads({"threads": [], "decisions": [d]}) is True
        assert has_threads({"threads": [], "decisions": []}) is False

    def test_more_batches(self):
        assert more_batches({"batch_index": 0, "batches": [[1]]}) is True
        assert more_batches({"batch_index": 1, "batches": [[1]]}) is False

    def test_error_routes_to_error_handler(self):
        assert route_after_load({"error": "boom"}) == "error_handler"
        assert route_after_apply({"error": "boom"}) == "error_handler"


class TestHumanizeError:
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (RuntimeError("HTTP 429 Too Many Requests"), "rate-limited"),
            (RuntimeError("invalid_grant: token revoked"), "Reconnect Gmail"),
            (TimeoutError("read timed out"), "timed out"),
            (ValueError("kaboom"), "Something went wrong"),
        ],
    )
    def test_maps_to_actionable_sentence(self, exc, expected):
        message = humanize_error(exc)
        assert expected in message
        assert "Traceback" not in message
        assert type(exc).__name__ not in message


class TestErrorPathThroughGraph:
    def test_llm_failure_interrupts_run_with_human_reason_and_still_finalizes(self):
        def exploding_classify(views, taxonomy):
            raise RuntimeError("HTTP 429 Too Many Requests")

        world = make_fake_deps(threads=[view(1)], classify_fn=exploding_classify)
        summary = run(world)

        assert summary["status"] == "interrupted"
        assert "rate-limited" in summary["error"]
        # status persisted twice: by error_handler and by finalize — both interrupted
        assert all(u["status"] == "interrupted" for u in world.run_updates)
        interrupted = world.events_of("run_interrupted")
        assert len(interrupted) == 1
        assert "Traceback" not in interrupted[0]["sentence"]
        # finalize ALWAYS runs
        assert len(world.events_of("run_finished")) == 1
        # nothing was applied for the failed batch
        assert world.mutations == []

    def test_gmail_failure_mid_apply_keeps_persisted_decisions(self):
        def failing_apply(thread_id, action, label_name):
            raise RuntimeError("401 unauthorized: invalid_grant")

        world = make_fake_deps(
            threads=[view(1)],
            classify_fn=lambda v, t: outcome_for(v, {"t1": ("Finance", 0.9, "ok")}),
            apply_fn=failing_apply,
        )
        summary = run(world)
        assert summary["status"] == "interrupted"
        assert summary["error"] == "Reconnect Gmail to continue."
        # decision row persisted BEFORE the mutation failed — resume re-applies it
        assert len(world.saved) == 1

    def test_fetch_failure_interrupts_before_any_llm_call(self):
        def exploding_fetch(user_id, limit):
            raise TimeoutError("gmail read timed out")

        world = make_fake_deps(threads=[], fetch_fn=exploding_fetch)
        summary = run(world)
        assert summary["status"] == "interrupted"
        assert world.classify_batches == []
        assert world.mutations == []

    def test_error_handler_survives_update_run_failure(self):
        def exploding_classify(views, taxonomy):
            raise RuntimeError("boom")

        def exploding_update(run_id, **fields):
            raise RuntimeError("db locked")

        world = make_fake_deps(threads=[view(1)], classify_fn=exploding_classify,
                               update_run_fn=exploding_update)
        summary = run(world)  # must not raise
        assert summary["status"] == "interrupted"
        assert len(world.events_of("run_interrupted")) == 1
