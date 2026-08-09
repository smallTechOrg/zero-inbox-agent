"""Un-archive correction detection (spec/capabilities/user-memory.md).

Fast, mocked unit tests only — no real LLM/API calls. Covers the reconciliation
hook in ``graph.nodes.fetch_items`` that detects a thread the agent archived
(an `applied` archive Decision) being found back in INBOX on a later fetch, and
records it as a `corrections` row via `tools.memory.record_correction` — while
never double-counting the app's own undo flow (`Decision.status == "undone"`).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from db.models import Correction, Decision, Item, SenderProfile, User
from graph.nodes import _detect_and_record_mailbox_corrections
from tools.memory import CORRECTION_IMPORTANCE_BUMP


@pytest.fixture
def db_session(_isolated_db):
    from db.session import create_db_session

    with create_db_session() as session:
        yield session


def _make_user(session, email="u@example.com") -> str:
    user = User(email=email, display_name="Test User")
    session.add(user)
    session.flush()
    return user.id


def _make_item(session, user_id, thread_id="thread-1", from_email="sender@corp.com") -> Item:
    item = Item(
        user_id=user_id,
        channel_account_id="chacc-1",
        external_thread_id=thread_id,
        subject="Re: quarterly update",
        from_name="Sender",
        from_email=from_email,
        from_domain=from_email.split("@")[-1],
        snippet_redacted="hello there",
    )
    session.add(item)
    session.flush()
    return item


def _make_decision(session, user_id, item_id, *, status="applied", proposed_action="archive", run_id="run-1") -> Decision:
    from db.models import TriageRun

    run = session.get(TriageRun, run_id)
    if run is None:
        run = TriageRun(id=run_id, user_id=user_id, channel_account_id="chacc-1", status="completed")
        session.add(run)
        session.flush()
    decision = Decision(
        user_id=user_id,
        item_id=item_id,
        run_id=run_id,
        proposed_action=proposed_action,
        confidence=0.9,
        reasoning="looked like a newsletter",
        decided_by="llm",
        status=status,
    )
    session.add(decision)
    session.flush()
    return decision


# --- happy path -------------------------------------------------------------------


def test_restored_inbox_thread_records_correction_and_bumps_importance(db_session):
    user_id = _make_user(db_session)
    item = _make_item(db_session, user_id)
    decision = _make_decision(db_session, user_id, item.id)
    db_session.commit()

    items = [{"external_thread_id": "thread-1", "channel_labels": ["INBOX", "IMPORTANT"]}]
    _detect_and_record_mailbox_corrections(user_id, items)

    corrections = db_session.execute(select(Correction).where(Correction.user_id == user_id)).scalars().all()
    assert len(corrections) == 1
    correction = corrections[0]
    assert correction.decision_id == decision.id
    assert correction.from_action == "archive"
    assert correction.to_action == "keep"
    assert correction.source == "mailbox_reconciliation"

    profile = db_session.execute(
        select(SenderProfile).where(
            SenderProfile.user_id == user_id, SenderProfile.sender_email == "sender@corp.com"
        )
    ).scalar_one()
    assert profile.importance_score == pytest.approx(CORRECTION_IMPORTANCE_BUMP)


# --- edge case: thread still archived (no INBOX) -----------------------------------


def test_thread_still_archived_records_nothing(db_session):
    user_id = _make_user(db_session)
    item = _make_item(db_session, user_id)
    _make_decision(db_session, user_id, item.id)
    db_session.commit()

    items = [{"external_thread_id": "thread-1", "channel_labels": ["Category/Newsletters"]}]
    _detect_and_record_mailbox_corrections(user_id, items)

    corrections = db_session.execute(select(Correction).where(Correction.user_id == user_id)).scalars().all()
    assert corrections == []


# --- error path: the app's own undo flow must never double-count as a correction ---


def test_apps_own_undo_is_not_double_counted_as_correction(db_session):
    user_id = _make_user(db_session)
    item = _make_item(db_session, user_id)
    _make_decision(db_session, user_id, item.id, status="undone")
    db_session.commit()

    items = [{"external_thread_id": "thread-1", "channel_labels": ["INBOX"]}]
    _detect_and_record_mailbox_corrections(user_id, items)

    corrections = db_session.execute(select(Correction).where(Correction.user_id == user_id)).scalars().all()
    assert corrections == []


# --- idempotency: re-fetching the same restored thread must not double-record -----


def test_repeated_fetch_does_not_duplicate_correction(db_session):
    user_id = _make_user(db_session)
    item = _make_item(db_session, user_id)
    _make_decision(db_session, user_id, item.id)
    db_session.commit()

    items = [{"external_thread_id": "thread-1", "channel_labels": ["INBOX"]}]
    _detect_and_record_mailbox_corrections(user_id, items)
    _detect_and_record_mailbox_corrections(user_id, items)

    corrections = db_session.execute(select(Correction).where(Correction.user_id == user_id)).scalars().all()
    assert len(corrections) == 1

    profile = db_session.execute(
        select(SenderProfile).where(
            SenderProfile.user_id == user_id, SenderProfile.sender_email == "sender@corp.com"
        )
    ).scalar_one()
    assert profile.importance_score == pytest.approx(CORRECTION_IMPORTANCE_BUMP)


# --- multi-thread: only threads with an applied archive decision are considered ---


def test_no_decision_for_thread_is_ignored(db_session):
    user_id = _make_user(db_session)
    _make_item(db_session, user_id, thread_id="thread-no-decision")
    db_session.commit()

    items = [{"external_thread_id": "thread-no-decision", "channel_labels": ["INBOX"]}]
    _detect_and_record_mailbox_corrections(user_id, items)

    corrections = db_session.execute(select(Correction).where(Correction.user_id == user_id)).scalars().all()
    assert corrections == []
