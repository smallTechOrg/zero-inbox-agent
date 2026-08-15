"""Phase 9, item zero: `reviewed` is a claim about a DECISION, not about a run.

`spec/roadmap.md` § Phase 9 "Item zero" / "Slice 1 — review-gate-integrity".

Before Phase 9, `finalise_review` upgraded every row of the run to `reviewed`
regardless of what the reviewer looked at, so `NotReviewedError` — the gate that
stands between an unaudited decision and the Gmail mutator — passed vacuously.

These tests pin the two halves of the fix:

1. `finalise_review` upgrades **exactly** the item ids it was told were audited,
   leaves the rest `provisional`, and reports `not_audited`.
2. `upgrade_review_state` can no longer express a run-wide promotion to
   `reviewed` at all — it raises.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

USER_ID = "test-user-finalise"
RUN_ID = "run-finalise-scope"
OTHER_RUN_ID = "run-finalise-other"


def _decision_payload(item_id: str, action: str) -> dict:
    return {
        "item_id": item_id,
        "proposed_action": action,
        "confidence": 0.9,
        "reasoning": "bulk",
        "decided_by": "llm",
        "status": "proposed",
    }


@pytest.fixture
def seeded(_isolated_db):
    """One run of 10 provisional decisions: 5 archive, 5 digest."""
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

    ids = [f"it-{n}" for n in range(10)]
    with create_db_session() as session:
        session.add(User(id=USER_ID, email="finalise@example.com", display_name="F"))
        session.add(UserSettings(user_id=USER_ID))
        session.add(
            ChannelAccount(
                id="acct-fin",
                user_id=USER_ID,
                channel="gmail",
                account_email="finalise@example.com",
                refresh_token_enc="ENC",
                scopes=[],
                status="connected",
            )
        )
        session.add(
            Category(
                id="cat-fin",
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
                channel_account_id="acct-fin",
                status="running",
                dry_run=False,
                items_total=len(ids),
                items_decided=len(ids),
            )
        )
        for n, item_id in enumerate(ids):
            session.add(
                Item(
                    id=item_id,
                    user_id=USER_ID,
                    channel_account_id="acct-fin",
                    external_thread_id=f"thread-{n}",
                    external_message_ids=[f"m{n}"],
                    subject=f"Subject {n}",
                    from_name="Sender",
                    from_email="s@example.com",
                    from_domain="example.com",
                    message_count=1,
                    snippet_redacted="x",
                    internal_date=datetime.now(timezone.utc),
                    is_unread=False,
                )
            )
            session.add(
                Decision(
                    id=f"dec-{n}",
                    user_id=USER_ID,
                    item_id=item_id,
                    run_id=RUN_ID,
                    category_id="cat-fin",
                    proposed_action="archive" if n < 5 else "digest",
                    confidence=0.9,
                    reasoning="bulk",
                    decided_by="llm",
                    time_sensitive=False,
                    status="proposed",
                    review_state="provisional",
                )
            )
        session.commit()
    return ids


def _items_payload(ids: list[str]) -> list[dict]:
    """The same 10 threads, in graph-state shape, so persist_run_results can
    resolve them. Without this the decision loop skips every row and any
    assertion about what it wrote would pass vacuously."""
    return [
        {
            "id": item_id,
            "external_thread_id": f"thread-{n}",
            "external_message_ids": [f"m{n}"],
            "subject": f"Subject {n}",
            "from_name": "Sender",
            "from_email": "s@example.com",
            "from_domain": "example.com",
            "message_count": 1,
            "snippet_redacted": "x",
            "internal_date": datetime.now(timezone.utc),
            "is_unread": False,
        }
        for n, item_id in enumerate(ids)
    ]


def _states(ids: list[str]) -> dict[str, str]:
    from db.models import Decision
    from db.session import create_db_session
    from sqlalchemy import select

    with create_db_session() as session:
        rows = session.execute(
            select(Decision).where(Decision.run_id == RUN_ID)
        ).scalars()
        return {r.item_id: r.review_state for r in rows}


# --------------------------------------------------------------------------
# 1. Only audited rows become reviewed
# --------------------------------------------------------------------------


def test_finalise_review_marks_only_the_audited_rows(seeded):
    from db.session import create_db_session
    from graph.persistence import finalise_review

    audited = ["it-0", "it-1", "it-2"]
    with create_db_session() as session:
        outcome = finalise_review(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            decisions=[_decision_payload(i, "archive") for i in seeded[:5]]
            + [_decision_payload(i, "digest") for i in seeded[5:]],
            audited_item_ids=audited,
        )
        session.commit()

    assert outcome["reviewed"] == 3
    assert outcome["review_failed"] == 0
    assert outcome["not_audited"] == 7

    states = _states(seeded)
    assert [i for i, s in states.items() if s == "reviewed"] == audited
    assert sorted(i for i, s in states.items() if s == "provisional") == sorted(
        seeded[3:]
    ), "every row the reviewer never audited must stay provisional"


def test_finalise_review_never_vouches_for_a_digest_it_did_not_audit(seeded):
    """The exact live defect: 171 digest rows read `reviewed`, unaudited."""
    from db.session import create_db_session
    from graph.persistence import finalise_review

    archives = seeded[:5]
    digests = seeded[5:]
    with create_db_session() as session:
        outcome = finalise_review(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            decisions=[_decision_payload(i, "archive") for i in archives]
            + [_decision_payload(i, "digest") for i in digests],
            audited_item_ids=archives,  # pre-Phase-9 reviewer scope
        )
        session.commit()

    assert outcome["reviewed"] == 5
    assert outcome["not_audited"] == 5
    states = _states(seeded)
    for item_id in digests:
        assert states[item_id] == "provisional", (
            f"{item_id} is a digest the reviewer never audited — marking it "
            "'reviewed' is exactly the vacuous gate Phase 9 closes"
        )


def test_empty_audited_set_upgrades_nothing(seeded):
    """Edge case: reviewer audited nothing. Nothing may become reviewed."""
    from db.session import create_db_session
    from graph.persistence import finalise_review

    with create_db_session() as session:
        outcome = finalise_review(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            decisions=[_decision_payload(i, "archive") for i in seeded],
            audited_item_ids=[],
        )
        session.commit()

    assert outcome["reviewed"] == 0
    assert outcome["not_audited"] == 10
    assert set(_states(seeded).values()) == {"provisional"}


def test_review_failed_wins_over_a_conflicting_audited_claim(seeded):
    """Error path: an id in both sets is `review_failed` — the safe reading."""
    from db.session import create_db_session
    from graph.persistence import finalise_review

    with create_db_session() as session:
        outcome = finalise_review(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            decisions=[_decision_payload(i, "archive") for i in seeded],
            audited_item_ids=["it-0", "it-1"],
            review_failed_item_ids=["it-1", "it-2"],
        )
        session.commit()

    assert outcome["review_failed"] == 2
    assert outcome["reviewed"] == 1
    states = _states(seeded)
    assert states["it-0"] == "reviewed"
    assert states["it-1"] == "review_failed"
    assert states["it-2"] == "review_failed"


def test_audited_item_ids_is_required():
    """Signature guard — slices 3 and 5 code against this exact contract."""
    import inspect

    from graph.persistence import finalise_review

    sig = inspect.signature(finalise_review)
    param = sig.parameters["audited_item_ids"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        "audited_item_ids must have no default: a caller that forgets it must "
        "fail loudly, never fall back to 'the whole run'"
    )

    with pytest.raises(TypeError):
        finalise_review(None, run_id="r", user_id="u", decisions=[])


def test_unknown_audited_ids_are_ignored_not_expanded(seeded):
    """Edge case: ids for another user's/run's rows upgrade nothing."""
    from db.session import create_db_session
    from graph.persistence import finalise_review

    with create_db_session() as session:
        outcome = finalise_review(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            decisions=[_decision_payload(i, "archive") for i in seeded],
            audited_item_ids=["it-does-not-exist", "it-0"],
        )
        session.commit()

    assert outcome["reviewed"] == 1
    assert _states(seeded)["it-0"] == "reviewed"


# --------------------------------------------------------------------------
# 2. The run-wide upgrade is no longer expressible
# --------------------------------------------------------------------------


def test_run_wide_upgrade_to_reviewed_raises(seeded):
    from db.session import create_db_session
    from graph.persistence import ReviewScopeError, upgrade_review_state

    with create_db_session() as session:
        with pytest.raises(ReviewScopeError) as excinfo:
            upgrade_review_state(session, run_id=RUN_ID, state="reviewed")
        session.rollback()

    assert "item_ids" in str(excinfo.value)
    assert set(_states(seeded).values()) == {"provisional"}


def test_run_wide_upgrade_raises_even_with_explicit_none(seeded):
    from db.session import create_db_session
    from graph.persistence import ReviewScopeError, upgrade_review_state

    with create_db_session() as session:
        with pytest.raises(ReviewScopeError):
            upgrade_review_state(
                session, run_id=RUN_ID, state="reviewed", item_ids=None
            )
        session.rollback()


def test_scoped_upgrade_still_works(seeded):
    from db.session import create_db_session
    from graph.persistence import upgrade_review_state

    with create_db_session() as session:
        changed = upgrade_review_state(
            session, run_id=RUN_ID, state="reviewed", item_ids=["it-0", "it-1"]
        )
        session.commit()

    assert changed == 2
    states = _states(seeded)
    assert states["it-0"] == "reviewed"
    assert states["it-9"] == "provisional"


def test_run_wide_downgrade_is_still_allowed(seeded):
    """Only the *unsafe* direction is blocked. review_failed stays expressible."""
    from db.session import create_db_session
    from graph.persistence import upgrade_review_state

    with create_db_session() as session:
        changed = upgrade_review_state(session, run_id=RUN_ID, state="review_failed")
        session.commit()

    assert changed == 10
    assert set(_states(seeded).values()) == {"review_failed"}


# --------------------------------------------------------------------------
# 3. The other run-wide path: persist_run_results must not launder `reviewed`
# --------------------------------------------------------------------------


def test_persist_run_results_cannot_mark_a_row_reviewed(seeded):
    """A blanket `review_state="reviewed"` on the decision payload is ignored.

    `graph.nodes.persist_decisions` stamps every decision dict `reviewed` before
    persisting. That is the second run-wide upgrade path, and it must not be able
    to vouch for a row either.
    """
    from db.session import create_db_session
    from graph.persistence import persist_run_results

    payload = []
    for n, item_id in enumerate(seeded):
        d = _decision_payload(item_id, "archive" if n < 5 else "digest")
        d["category"] = "newsletters"
        d["review_state"] = "reviewed"
        d["status"] = "approved"  # a field that IS persisted — proves the write ran
        payload.append(d)

    with create_db_session() as session:
        persist_run_results(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            channel_account_id="acct-fin",
            items=_items_payload(seeded),
            decisions=payload,
            clusters=[],
            counts={"total": len(payload)},
            cost={},
            llm_calls=[],
            status="running",
        )
        session.commit()

    from db.models import Decision
    from sqlalchemy import select

    with create_db_session() as session:
        rows = [
            (r.status, r.review_state)
            for r in session.execute(
                select(Decision).where(Decision.run_id == RUN_ID)
            ).scalars()
        ]
    assert {status for status, _ in rows} == {"approved"}, (
        "persist_run_results never reached these rows — the review_state "
        "assertion below would pass vacuously"
    )
    assert {state for _, state in rows} == {"provisional"}, (
        "persist_run_results accepted a blanket 'reviewed' — the run-wide "
        "upgrade is back in through the side door"
    )


def test_persist_run_results_still_honours_review_failed(seeded):
    from db.session import create_db_session
    from graph.persistence import persist_run_results

    payload = []
    for n, item_id in enumerate(seeded):
        d = _decision_payload(item_id, "archive")
        d["category"] = "newsletters"
        d["review_state"] = "review_failed" if n == 0 else "provisional"
        payload.append(d)

    with create_db_session() as session:
        persist_run_results(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            channel_account_id="acct-fin",
            items=_items_payload(seeded),
            decisions=payload,
            clusters=[],
            counts={"total": len(payload)},
            cost={},
            llm_calls=[],
            status="running",
        )
        session.commit()

    states = _states(seeded)
    assert states["it-0"] == "review_failed"
    assert states["it-1"] == "provisional"
