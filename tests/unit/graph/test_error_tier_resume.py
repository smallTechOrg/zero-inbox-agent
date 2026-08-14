"""Error-tail convergence — Rules F1 + F2 (Phase 7, slice 2).

Run ``fbeed060`` ended with 179 rows ``decided_by="error"``: threads the tier could
not decide, permanently counted as decided and therefore never picked up again.

F1 (``already_decided_item_ids`` excludes them) and F2
(``insert_provisional_decisions`` overwrites them in place) are a **pair**. The trap
this module exists to close: F1 alone re-classifies the tail and then throws the new
answer away, because the insert path skips any existing ``(run_id, item_id)``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

RUN_ID = "runF"
USER_ID = "uF"


def _item_payload(index: int) -> dict:
    return {
        "id": f"item{index}",
        "external_thread_id": f"thread{index}",
        "subject": f"Subject {index}",
        "from_name": "Sender",
        "from_email": "s@example.com",
        "from_domain": "example.com",
        "message_count": 1,
        "snippet_redacted": "x",
        "internal_date": datetime.now(timezone.utc),
        "is_unread": False,
    }


@pytest.fixture
def seeded(_isolated_db):
    """A run with one healthy row, one error row and one review_failed row."""
    from db.models import (
        Category,
        ChannelAccount,
        Decision,
        Item,
        TriageRun,
        User,
        UserSettings,
    )
    from db.session import create_db_session

    specs = [
        {"decided_by": "llm", "review_state": "reviewed"},
        {"decided_by": "error", "review_state": "provisional"},
        {"decided_by": "llm", "review_state": "review_failed"},
    ]
    with create_db_session() as session:
        session.add(User(id=USER_ID, email="uf@example.com", display_name="U"))
        session.add(UserSettings(user_id=USER_ID))
        session.add(
            ChannelAccount(
                id="acctF",
                user_id=USER_ID,
                channel="gmail",
                account_email="uf@gmail.com",
                refresh_token_enc="ENC",
                scopes=[],
                status="connected",
            )
        )
        session.add(
            Category(
                id="catF",
                user_id=USER_ID,
                key="newsletters",
                name="Newsletters",
                description="",
                channel_label_name="ZeroInbox/Newsletters",
                default_action="archive",
                is_default=True,
                sort_order=1,
            )
        )
        session.add(
            TriageRun(
                id=RUN_ID,
                user_id=USER_ID,
                channel_account_id="acctF",
                status="resumable",
                dry_run=False,
                items_total=3,
                items_decided=3,
            )
        )
        for index, spec in enumerate(specs):
            payload = _item_payload(index)
            session.add(
                Item(
                    id=payload["id"],
                    user_id=USER_ID,
                    channel_account_id="acctF",
                    external_thread_id=payload["external_thread_id"],
                    external_message_ids=[f"msg{index}"],
                    subject=payload["subject"],
                    from_name="Sender",
                    from_email="s@example.com",
                    from_domain="example.com",
                    message_count=1,
                    snippet_redacted="x",
                    internal_date=payload["internal_date"],
                    is_unread=False,
                )
            )
            session.add(
                Decision(
                    id=f"decF{index}",
                    user_id=USER_ID,
                    item_id=payload["id"],
                    run_id=RUN_ID,
                    category_id="catF",
                    proposed_action="keep",
                    confidence=0.0 if spec["decided_by"] == "error" else 0.9,
                    reasoning="",
                    decided_by=spec["decided_by"],
                    time_sensitive=False,
                    status="proposed",
                    review_state=spec["review_state"],
                )
            )
        session.commit()
    return specs


# ------------------------------------------------------------------------ F1


def test_error_and_review_failed_rows_are_not_reported_as_decided(seeded):
    from db.session import create_db_session
    from graph.persistence import already_decided_item_ids

    with create_db_session() as session:
        decided = already_decided_item_ids(session, RUN_ID)

    # item0 (a real verdict) is decided; item1 (error) and item2 (review_failed)
    # were never really decided and must be re-queued on resume.
    assert "item0" in decided and "thread0" in decided
    assert "item1" not in decided and "thread1" not in decided
    assert "item2" not in decided and "thread2" not in decided


# ------------------------------------------------------------------------ F2


def test_error_row_is_overwritten_in_place_on_reclassification(seeded):
    from db.models import Decision
    from db.session import create_db_session
    from graph.persistence import insert_provisional_decisions

    with create_db_session() as session:
        written = insert_provisional_decisions(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            channel_account_id="acctF",
            items=[_item_payload(1)],
            decisions=[
                {
                    "item_id": "item1",
                    "category": "newsletters",
                    "proposed_action": "archive",
                    "confidence": 0.88,
                    "reasoning": "re-classified on resume",
                    "decided_by": "llm",
                    "time_sensitive": False,
                    "status": "proposed",
                }
            ],
        )
        session.commit()

    # The overwrite is NOT counted as a new row: it was already counted in
    # triage_runs.items_decided when the error row was first inserted.
    assert written == 0

    with create_db_session() as session:
        rows = session.query(Decision).filter(Decision.run_id == RUN_ID).all()
        by_item = {row.item_id: row for row in rows}
        # Zero duplicate (run_id, item_id) pairs.
        assert len(rows) == 3
        row = by_item["item1"]
        # The trap: F1 without F2 leaves this row exactly as it was.
        assert row.decided_by == "llm"
        assert row.proposed_action == "archive"
        assert row.confidence == pytest.approx(0.88)
        assert row.reasoning == "re-classified on resume"
        assert row.category_id == "catF"
        # The new verdict has not been reviewed yet, so it re-enters the
        # never-miss gate rather than inheriting a stale finality.
        assert row.review_state == "provisional"


def test_a_healthy_existing_row_is_never_overwritten(seeded):
    from db.models import Decision
    from db.session import create_db_session
    from graph.persistence import insert_provisional_decisions

    with create_db_session() as session:
        written = insert_provisional_decisions(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            channel_account_id="acctF",
            items=[_item_payload(0)],
            decisions=[
                {
                    "item_id": "item0",
                    "category": "newsletters",
                    "proposed_action": "archive",
                    "confidence": 0.99,
                    "reasoning": "should never land",
                    "decided_by": "llm",
                    "status": "proposed",
                }
            ],
        )
        session.commit()

    assert written == 0
    with create_db_session() as session:
        row = (
            session.query(Decision)
            .filter(Decision.run_id == RUN_ID, Decision.item_id == "item0")
            .one()
        )
        assert row.proposed_action == "keep"
        assert row.reasoning == ""
        assert row.review_state == "reviewed"


def test_new_rows_are_still_inserted_and_counted(seeded):
    from db.models import Decision
    from db.session import create_db_session
    from graph.persistence import insert_provisional_decisions

    with create_db_session() as session:
        written = insert_provisional_decisions(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            channel_account_id="acctF",
            items=[_item_payload(9)],
            decisions=[
                {
                    "item_id": "item9",
                    "category": "newsletters",
                    "proposed_action": "archive",
                    "confidence": 0.81,
                    "reasoning": "fresh",
                    "decided_by": "llm",
                    "status": "proposed",
                }
            ],
        )
        session.commit()

    assert written == 1
    with create_db_session() as session:
        assert session.query(Decision).filter(Decision.run_id == RUN_ID).count() == 4


def test_f1_and_f2_together_converge_the_tail(seeded):
    """The pair, end to end: the tail is re-queued AND its answer is kept."""
    from db.models import Decision
    from db.session import create_db_session
    from graph.persistence import already_decided_item_ids, insert_provisional_decisions

    with create_db_session() as session:
        pending = already_decided_item_ids(session, RUN_ID)

    requeued = [i for i in ("item1", "item2") if i not in pending]
    assert requeued == ["item1", "item2"]

    with create_db_session() as session:
        insert_provisional_decisions(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            channel_account_id="acctF",
            items=[_item_payload(1), _item_payload(2)],
            decisions=[
                {
                    "item_id": item_id,
                    "category": "newsletters",
                    "proposed_action": "archive",
                    "confidence": 0.86,
                    "reasoning": "resume verdict",
                    "decided_by": "llm",
                    "status": "proposed",
                }
                for item_id in requeued
            ],
        )
        session.commit()

    with create_db_session() as session:
        rows = session.query(Decision).filter(Decision.run_id == RUN_ID).all()
        assert len(rows) == 3  # no duplicates
        assert not [r for r in rows if r.decided_by == "error"]
        # BOTH unresolved kinds converge. `review_failed` used to be excluded
        # here, which made it a permanent self-renewing leak rather than a
        # one-off discard: F1 re-queued the row (costing a real LLM call every
        # resume, forever), F2 refused the write so it stayed `review_failed`,
        # and `load_provisional_for_review` selects only `provisional` so the
        # reviewer could never clear it. The row stayed un-appliable for good —
        # counted in `distance_to_zero` permanently, pinning `apply_ok` false on
        # every future run so the red "Retry archiving" bar could never clear.
        by_item = {r.item_id: r for r in rows}
        assert by_item["item2"].review_state == "provisional", (
            "a review_failed row must be overwritten by its fresh verdict and "
            "returned to provisional, or the tail never converges"
        )
        assert by_item["item2"].reasoning == "resume verdict"
        # Landing state matters: provisional means it re-enters the never-miss
        # gate like any other decision. Nothing skips review.
        assert by_item["item2"].status == "proposed"

        # And nothing is left unresolved at all — the whole point of the tail.
        unresolved = [
            r for r in rows
            if r.decided_by == "error" or r.review_state == "review_failed"
        ]
        assert unresolved == []
