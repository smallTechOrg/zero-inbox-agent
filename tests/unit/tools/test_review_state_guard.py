"""The never-miss finality gate: only a `reviewed` decision may ever mutate Gmail.

Phase 6 makes decisions durable the instant a tier makes them, so the database now
contains rows the second-pass reviewer has not seen. Durability must never leak into
finality: ``apply_decision()`` raises :class:`NotReviewedError` for anything whose
``review_state`` is not ``reviewed`` — **before** the mutator is touched, independently
of ``status`` (so flipping a row to ``approved`` cannot force it through), and **not**
bypassable by ``force=True`` (which only ever bypassed the keep-proposed guard).

See spec/capabilities/never-miss-safeguards.md and
spec/capabilities/durable-resumable-runs.md Rule A.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.actions import NotReviewedError, apply_decision

USER_ID = "u-rs"
OTHER_USER = "u-other"


class RecordingMutator:
    """Any call here during a refused apply is a never-miss regression."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_thread_labels(self, thread_id: str) -> list[str]:
        self.calls.append(("get_thread_labels", thread_id))
        return ["INBOX"]

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("archive_and_label", thread_id))
        return {"id": thread_id}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("undo_archive_and_label", thread_id))
        return {"id": thread_id}

    def restore_labels(self, thread_id: str, **kwargs) -> dict:
        self.calls.append(("restore_labels", thread_id))
        return {"id": thread_id}


class Labels:
    def ensure_label(self, name: str) -> dict:
        return {"id": "Label_1", "name": name}


@pytest.fixture
def seeded(_isolated_db):
    """One user, one run, one item and one archive decision per review_state."""
    from db.models import (
        Category,
        ChannelAccount,
        Decision,
        Item,
        TriageRun,
        User,
    )
    from db.session import create_db_session

    ids: dict[str, str] = {}
    with create_db_session() as session:
        session.add(User(id=USER_ID, email="rs@example.com"))
        session.add(
            ChannelAccount(
                id="acct-rs",
                user_id=USER_ID,
                channel="gmail",
                account_email="rs@example.com",
                refresh_token_enc="x",
            )
        )
        session.add(
            Category(
                id="cat-rs",
                user_id=USER_ID,
                key="newsletters",
                name="Newsletters",
                default_action="archive",
                channel_label_name="ZeroInbox/Newsletters",
            )
        )
        session.add(
            TriageRun(
                id="run-rs",
                user_id=USER_ID,
                channel_account_id="acct-rs",
                status="running",
                dry_run=False,
                counts={},
            )
        )
        for n, review_state in enumerate(("provisional", "review_failed", "reviewed")):
            session.add(
                Item(
                    id=f"item-{review_state}",
                    user_id=USER_ID,
                    channel_account_id="acct-rs",
                    external_thread_id=f"thread-{n}",
                    subject="Weekly digest",
                    from_email="news@substack.com",
                    channel_labels=["INBOX"],
                    internal_date=datetime.now(timezone.utc),
                )
            )
            session.add(
                Decision(
                    id=f"dec-{review_state}",
                    user_id=USER_ID,
                    run_id="run-rs",
                    item_id=f"item-{review_state}",
                    category_id="cat-rs",
                    proposed_action="archive",
                    confidence=0.96,
                    reasoning="Substack List-Id.",
                    decided_by="llm",
                    # `approved` on purpose: the review gate is orthogonal to status.
                    status="approved",
                    review_state=review_state,
                )
            )
            ids[review_state] = f"dec-{review_state}"
    return ids


def _session():
    from db.session import create_db_session

    return create_db_session()


# --- error path: the gate refuses, and the mutator is never reached ---------------


@pytest.mark.parametrize("review_state", ["provisional", "review_failed"])
def test_apply_refuses_an_unreviewed_decision_even_when_approved(seeded, review_state):
    mutator = RecordingMutator()
    with _session() as session:
        with pytest.raises(NotReviewedError):
            apply_decision(
                session,
                USER_ID,
                seeded[review_state],
                mutator=mutator,
                label_lookup=Labels(),
                dry_run=False,
            )
    assert mutator.calls == [], "the mutator must never be reached for an unreviewed row"


@pytest.mark.parametrize("review_state", ["provisional", "review_failed"])
def test_force_true_does_not_bypass_the_review_gate(seeded, review_state):
    """`force` only ever bypassed the keep-proposed guard — never this one."""
    mutator = RecordingMutator()
    with _session() as session:
        with pytest.raises(NotReviewedError):
            apply_decision(
                session,
                USER_ID,
                seeded[review_state],
                mutator=mutator,
                label_lookup=Labels(),
                dry_run=False,
                force=True,
            )
    assert mutator.calls == []


def test_a_refused_apply_leaves_the_decision_and_the_labels_untouched(seeded):
    from db.models import Decision, Item

    mutator = RecordingMutator()
    with _session() as session:
        with pytest.raises(NotReviewedError):
            apply_decision(
                session,
                USER_ID,
                seeded["provisional"],
                mutator=mutator,
                label_lookup=Labels(),
                dry_run=False,
            )
    with _session() as session:
        decision = session.get(Decision, seeded["provisional"])
        item = session.get(Item, "item-provisional")
        assert decision.status == "approved"
        assert decision.review_state == "provisional"
        assert item.channel_labels == ["INBOX"]


def test_the_error_message_names_the_review_state(seeded):
    with _session() as session:
        with pytest.raises(NotReviewedError) as excinfo:
            apply_decision(
                session,
                USER_ID,
                seeded["review_failed"],
                mutator=RecordingMutator(),
                label_lookup=Labels(),
                dry_run=False,
            )
    assert "review_failed" in str(excinfo.value)


# --- happy path: a reviewed decision still applies --------------------------------


def test_a_reviewed_decision_applies_normally(seeded):
    from db.models import Decision

    mutator = RecordingMutator()
    with _session() as session:
        action_log = apply_decision(
            session,
            USER_ID,
            seeded["reviewed"],
            mutator=mutator,
            label_lookup=Labels(),
            dry_run=False,
        )
        assert action_log.undo_token["thread_id"] == "thread-2"
        session.commit()

    assert ("archive_and_label", "thread-2") in mutator.calls
    with _session() as session:
        assert session.get(Decision, seeded["reviewed"]).status == "applied"


# --- edge case: the gate runs before every other guard ---------------------------


def test_dry_run_is_still_refused_first(seeded):
    """`dry_run` remains the outermost guard — nothing is attempted at all."""
    from channels.base import DryRunViolation

    mutator = RecordingMutator()
    with _session() as session:
        with pytest.raises(DryRunViolation):
            apply_decision(
                session,
                USER_ID,
                seeded["provisional"],
                mutator=mutator,
                label_lookup=Labels(),
                dry_run=True,
            )
    assert mutator.calls == []


def test_a_missing_decision_is_still_a_plain_actions_error(seeded):
    from tools.actions import ActionsError

    with _session() as session:
        with pytest.raises(ActionsError) as excinfo:
            apply_decision(
                session,
                USER_ID,
                "does-not-exist",
                mutator=RecordingMutator(),
                label_lookup=Labels(),
                dry_run=False,
            )
    assert not isinstance(excinfo.value, NotReviewedError)
