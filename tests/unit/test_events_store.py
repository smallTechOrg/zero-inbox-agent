"""Unit tests — runs-undo-api slice: persist-then-emit event store (src/events/)."""

from __future__ import annotations

import pytest

from tests.conftest import seed_user

ALICE = "test-user-alice"
BOB = "test-user-bob"


@pytest.fixture
def run_for(db_session):
    def _make(user_id: str):
        seed_user(db_session, user_id=user_id, email=f"{user_id}@example.com")
        from db.models import Run

        run = Run(user_id=user_id, status="running")
        db_session.add(run)
        db_session.commit()
        return run

    return _make


def test_record_run_event_persists_with_monotonic_seq_and_emits(db_session, run_for):
    from db.models import RunEvent
    from events import bus
    from events.store import record_run_event

    run = run_for(ALICE)
    first = record_run_event(
        db_session,
        user_id=ALICE,
        run_id=run.id,
        type="decision",
        sentence="Filed 'ACME invoice' → Finance.",
        detail={"confidence": 0.93, "reasoning": "invoice from ACME"},
    )
    second = record_run_event(
        db_session, user_id=ALICE, run_id=run.id, type="action", sentence="Archived it."
    )
    db_session.commit()

    assert (first["seq"], second["seq"]) == (1, 2)
    rows = db_session.query(RunEvent).filter_by(run_id=run.id).order_by(RunEvent.seq).all()
    assert [(r.seq, r.type) for r in rows] == [(1, "decision"), (2, "action")]
    assert rows[0].detail_json["confidence"] == 0.93

    # Persisted BEFORE emitted, and emitted with the identical payload.
    live = [e for e in bus.replay_buffer(ALICE) if e.get("run_id") == run.id]
    assert [e["seq"] for e in live] == [1, 2]
    assert live[0]["sentence"] == "Filed 'ACME invoice' → Finance."


def test_replay_filters_by_after_seq_and_user(db_session, run_for):
    from events.store import record_run_event, replay_run_events

    run = run_for(ALICE)
    for i in range(3):
        record_run_event(
            db_session, user_id=ALICE, run_id=run.id, type="action", sentence=f"step {i}"
        )
    db_session.commit()

    tail = replay_run_events(db_session, user_id=ALICE, run_id=run.id, after_seq=1)
    assert [e["seq"] for e in tail] == [2, 3]
    # A run id is not a capability: another user replays nothing.
    assert replay_run_events(db_session, user_id=BOB, run_id=run.id) == []


def test_record_event_owns_its_session_and_commits(db_session, run_for):
    """The session-owning helper (used by API-side producers) is durable."""
    from db.models import RunEvent
    from events.store import record_event

    run = run_for(ALICE)
    payload = record_event(
        user_id=ALICE, run_id=run.id, type="undo_started", sentence="Undoing this run."
    )
    assert payload["seq"] == 1
    db_session.expire_all()
    row = db_session.query(RunEvent).filter_by(run_id=run.id).one()
    assert row.type == "undo_started"


def test_empty_run_replays_empty(db_session, run_for):
    from events.store import replay_run_events

    run = run_for(ALICE)
    assert replay_run_events(db_session, user_id=ALICE, run_id=run.id) == []
