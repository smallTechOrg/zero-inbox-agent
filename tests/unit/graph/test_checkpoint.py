"""Incremental checkpointing — durable the instant a tier decides (Phase 6, Rule A).

Regression for the motivating defect: run ``fbeed060`` decided 2,003 of 2,176 threads
over ~23 minutes and left **0 rows in `decisions`**, because everything was persisted
once, at the very end of the graph. These tests pin that

- a tier's rows exist in the database *while the run is still in flight*, as
  ``review_state="provisional"`` (durable, not yet final),
- the ``(run_id, item_id)`` unique constraint makes a re-checkpoint a no-op — never a
  duplicate row and never a double-counted ``items_decided`` or cost,
- a checkpoint that raises logs a warning and **never** kills the run.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from graph import checkpoint, nodes

RUN_ID = "run-cp"
USER_ID = "user-cp"
ACCOUNT_ID = "acct-cp"


def _item(index: int, **overrides) -> dict:
    base = {
        "id": f"i{index}",
        "external_thread_id": f"t{index}",
        "subject": f"Subject {index}",
        "from_name": "Sender",
        "from_email": "sender@example.com",
        "from_domain": "example.com",
        "snippet_redacted": "snippet",
        "message_count": 1,
        "has_attachments": False,
        "is_unread": True,
        "internal_date": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


def _decision(index: int, **overrides) -> dict:
    base = {
        "item_id": f"i{index}",
        "category": "newsletters",
        "proposed_action": "archive",
        "confidence": 0.91,
        "reasoning": "Substack List-Id and an unsubscribe header.",
        "decided_by": "llm",
        "rule_id": None,
        "time_sensitive": False,
        "status": "proposed",
    }
    base.update(overrides)
    return base


def _state(items: list[dict]) -> dict:
    return {
        "run_id": RUN_ID,
        "user_id": USER_ID,
        "channel_account_id": ACCOUNT_ID,
        "items": items,
    }


@pytest.fixture
def run_row(_isolated_db):
    from db.models import TriageRun, User
    from db.session import create_db_session

    with create_db_session() as session:
        session.add(User(id=USER_ID, email="cp@example.com"))
        session.add(
            TriageRun(
                id=RUN_ID,
                user_id=USER_ID,
                channel_account_id=ACCOUNT_ID,
                status="running",
                dry_run=True,
                items_total=3,
                items_decided=0,
                counts={},
            )
        )

    def _reload():
        with create_db_session() as session:
            row = session.get(TriageRun, RUN_ID)
            session.expunge(row)
            return row

    return _reload


def _decisions_in_db():
    from db.models import Decision
    from db.session import create_db_session

    with create_db_session() as session:
        rows = list(session.execute(select(Decision)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


# --- happy path: durable mid-run -------------------------------------------------


class TestRowsExistMidRun:
    def test_a_tier_batch_lands_in_the_database_immediately_as_provisional(self, run_row):
        items = [_item(1), _item(2)]
        written = checkpoint.record_batch(
            _state(items),
            [_decision(1), _decision(2)],
            tier="llm",
            llm_calls=[
                {
                    "purpose": "classify",
                    "model": "nvidia/nemotron-3-nano-30b-a3b",
                    "items_in_batch": 2,
                    "tokens_in": 100,
                    "tokens_out": 40,
                    "cost_usd": 0.001,
                    "latency_ms": 500,
                }
            ],
        )

        rows = _decisions_in_db()
        assert written == 2
        assert len(rows) == 2
        # Durable, but NOT final: nothing here may be applied to Gmail yet.
        assert {r.review_state for r in rows} == {"provisional"}
        assert {r.run_id for r in rows} == {RUN_ID}
        # The progress numerator moved while the run is still in flight.
        assert run_row().items_decided == 2

    def test_the_batchs_llm_calls_are_appended_so_cost_accrues_incrementally(self, run_row):
        from db.models import LLMCall
        from db.session import create_db_session

        calls = [
            {
                "purpose": "classify",
                "model": "nvidia/nemotron-3-nano-30b-a3b",
                "items_in_batch": 1,
                "tokens_in": 10,
                "tokens_out": 5,
                "cost_usd": 0.0002,
                "latency_ms": 120,
            }
        ]
        checkpoint.record_batch(_state([_item(1)]), [_decision(1)], tier="llm", llm_calls=calls)

        with create_db_session() as session:
            tokens = list(session.execute(select(LLMCall.tokens_in)).scalars())
        assert tokens == [10]
        # Marked so the end-of-graph finalisation cannot write them a second time.
        assert calls[0]["_checkpointed"] is True

    def test_one_thread_classified_event_is_emitted_per_decision(self, run_row, monkeypatch):
        import events.bus as bus

        seen: list[dict] = []
        monkeypatch.setattr(
            bus,
            "emit_thread_classified",
            lambda user_id, **payload: seen.append({"user_id": user_id, **payload}),
            raising=False,
        )

        checkpoint.record_batch(
            _state([_item(1), _item(2)]), [_decision(1), _decision(2)], tier="llm"
        )

        assert [e["item_id"] for e in seen] == ["i1", "i2"]
        assert {e["review_state"] for e in seen} == {"provisional"}
        assert seen[0]["user_id"] == USER_ID
        assert seen[0]["subject"] == "Subject 1"
        assert seen[0]["decided_by"] == "llm"


# --- edge case: idempotency ------------------------------------------------------


class TestIdempotency:
    def test_re_checkpointing_the_same_thread_never_duplicates_or_double_counts(
        self, run_row
    ):
        state = _state([_item(1)])
        first = checkpoint.record_batch(state, [_decision(1)], tier="llm")
        second = checkpoint.record_batch(state, [_decision(1)], tier="llm")

        assert (first, second) == (1, 0)
        assert len(_decisions_in_db()) == 1
        assert run_row().items_decided == 1

    def test_an_empty_batch_is_a_no_op(self, run_row):
        assert checkpoint.record_batch(_state([]), [], tier="rule") == 0
        assert _decisions_in_db() == []
        assert run_row().items_decided == 0


# --- error path: a checkpoint failure must never kill the run --------------------


class TestFailureIsSurvivable:
    def test_a_raising_checkpoint_does_not_propagate(self, run_row, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(
            "graph.persistence.insert_provisional_decisions", _boom
        )

        # No exception, no rows — the run simply carries on in memory and the
        # un-checkpointed threads are re-decided on resume.
        assert checkpoint.record_batch(_state([_item(1)]), [_decision(1)], tier="llm") == 0
        assert _decisions_in_db() == []
        assert run_row().items_decided == 0

    def test_a_raising_event_subscriber_does_not_propagate(self, run_row, monkeypatch):
        import events.bus as bus

        def _boom(*args, **kwargs):
            raise RuntimeError("subscriber exploded")

        monkeypatch.setattr(bus, "emit_thread_classified", _boom, raising=False)

        assert checkpoint.record_batch(_state([_item(1)]), [_decision(1)], tier="llm") == 1
        assert len(_decisions_in_db()) == 1

    def test_a_failing_checkpoint_does_not_fail_the_tier_node(self, run_row, monkeypatch):
        """The node still returns its decisions — durability is best-effort, the run is not."""

        def _boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("graph.persistence.insert_provisional_decisions", _boom)

        out = nodes.apply_deterministic_rules(
            {
                "run_id": RUN_ID,
                "user_id": USER_ID,
                "channel_account_id": ACCOUNT_ID,
                "items": [_item(1, list_id="<news.substack.com>")],
                "rules": [],
            }
        )
        assert "llm_queue" in out
